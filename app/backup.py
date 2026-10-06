"""Respaldo automático de la BD SQLite.

Railway monta un solo volumen: si el archivo fide.db se corrompe o alguien
borra una cartera por error, no hay de dónde recuperar. Este módulo:

  - run_backup(): copia consistente con sqlite3 Connection.backup() (segura
    aunque haya escrituras en curso, a diferencia de copiar el archivo), la
    comprime a .gz junto a la BD en <dir>/backups/ y conserva las 14 más
    recientes (≈ dos semanas si corre a diario).
  - start_backup_scheduler(): hilo daemon que al arrancar revisa la marca
    'last_backup_at' en kv_store; si pasaron más de 24 h ejecuta el respaldo
    y luego vuelve a revisar cada hora. Cualquier error se registra en el log
    y NUNCA tumba la app. No corre bajo pytest (APP_ENV == "test").
"""
import gzip
import logging
import os
import re
import shutil
import sqlite3
import threading
from datetime import datetime, timedelta

from app.config import DATABASE_PATH
from app.database import get_db

_log = logging.getLogger("fide.backup")

KEEP_LAST = 14
BACKUP_EVERY = timedelta(hours=24)
CHECK_EVERY_SECONDS = 3600
KV_KEY = "last_backup_at"

# Nombre estricto: fide_YYYYMMDD_HHMM.db.gz (opcionalmente con sufijo -N si
# hubo dos en el mismo minuto). Sirve también para validar descargas y evitar
# path traversal.
BACKUP_NAME_RE = re.compile(r"^fide_\d{8}_\d{4}(?:-\d+)?\.db\.gz$")

_scheduler_started = False
_lock = threading.Lock()


def backup_dir() -> str:
    """Carpeta de respaldos junto a la BD (en el volumen persistente)."""
    return os.path.join(os.path.dirname(os.path.abspath(DATABASE_PATH)) or ".", "backups")


def list_backups() -> list[dict]:
    """Lista los respaldos existentes, más reciente primero."""
    d = backup_dir()
    if not os.path.isdir(d):
        return []
    out = []
    for nombre in os.listdir(d):
        if not BACKUP_NAME_RE.match(nombre):
            continue
        ruta = os.path.join(d, nombre)
        try:
            st = os.stat(ruta)
        except OSError:
            continue
        out.append({
            "nombre": nombre,
            "tamano": st.st_size,
            "fecha": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "_mtime": st.st_mtime,
        })
    # Más reciente primero. Por mtime (no por nombre): dos respaldos en el
    # mismo minuto llevan sufijo -N y el orden lexicográfico los desordenaría.
    out.sort(key=lambda b: (b["_mtime"], b["nombre"]), reverse=True)
    for b in out:
        b.pop("_mtime", None)
    return out


def _rotate(keep: int = KEEP_LAST) -> int:
    """Borra los respaldos más viejos dejando `keep`. Devuelve cuántos borró."""
    borrados = 0
    for b in list_backups()[keep:]:
        try:
            os.remove(os.path.join(backup_dir(), b["nombre"]))
            borrados += 1
        except OSError:
            _log.warning("no se pudo borrar respaldo viejo %s", b["nombre"])
    return borrados


def run_backup(keep: int = KEEP_LAST) -> str:
    """Ejecuta un respaldo ahora. Devuelve el nombre del archivo .gz creado.

    Pasos: backup() de SQLite → fide_<fecha>.db → gzip → borrar el .db
    intermedio → rotar a `keep` → marcar last_backup_at en kv_store."""
    with _lock:
        d = backup_dir()
        os.makedirs(d, exist_ok=True)
        base = "fide_" + datetime.now().strftime("%Y%m%d_%H%M")
        nombre_gz = base + ".db.gz"
        n = 1
        while os.path.exists(os.path.join(d, nombre_gz)):
            nombre_gz = f"{base}-{n}.db.gz"
            n += 1
        ruta_db = os.path.join(d, nombre_gz[:-3])  # quitar ".gz"
        ruta_gz = os.path.join(d, nombre_gz)

        src = sqlite3.connect(DATABASE_PATH, timeout=10.0)
        try:
            dst = sqlite3.connect(ruta_db)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        try:
            with open(ruta_db, "rb") as f_in, gzip.open(ruta_gz, "wb", compresslevel=6) as f_out:
                shutil.copyfileobj(f_in, f_out)
        finally:
            try:
                os.remove(ruta_db)
            except OSError:
                pass

        _rotate(keep)
        _mark_done()
        _log.info("respaldo creado: %s (%d bytes)", nombre_gz, os.path.getsize(ruta_gz))
        return nombre_gz


def _mark_done():
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO kv_store (key, value, updated_at) "
                "VALUES (?, ?, datetime('now'))",
                (KV_KEY, datetime.utcnow().isoformat(timespec="seconds")),
            )
    except Exception:
        _log.exception("no se pudo marcar %s en kv_store", KV_KEY)


def _last_backup_at() -> datetime | None:
    try:
        with get_db() as conn:
            row = conn.execute("SELECT value FROM kv_store WHERE key = ?", (KV_KEY,)).fetchone()
        if row and row["value"]:
            return datetime.fromisoformat(row["value"])
    except Exception:
        _log.exception("no se pudo leer %s de kv_store", KV_KEY)
    return None


def backup_due(now: datetime | None = None) -> bool:
    """True si nunca se ha respaldado o el último respaldo tiene > 24 h."""
    now = now or datetime.utcnow()
    last = _last_backup_at()
    return last is None or (now - last) >= BACKUP_EVERY


def _loop(stop_event: threading.Event):
    while not stop_event.is_set():
        try:
            if backup_due():
                run_backup()
        except Exception:
            _log.exception("respaldo automático falló (se reintenta en 1 h)")
        stop_event.wait(CHECK_EVERY_SECONDS)


def start_backup_scheduler() -> threading.Thread | None:
    """Arranca el hilo daemon del respaldo. Idempotente; no corre en pytest."""
    global _scheduler_started
    if os.getenv("APP_ENV") == "test":
        return None
    if _scheduler_started:
        return None
    try:
        stop_event = threading.Event()
        t = threading.Thread(target=_loop, args=(stop_event,), name="fide-backup", daemon=True)
        t.start()
        _scheduler_started = True
        return t
    except Exception:
        _log.exception("no se pudo arrancar el hilo de respaldo (no fatal)")
        return None
