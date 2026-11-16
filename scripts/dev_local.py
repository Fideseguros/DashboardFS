"""Entorno local de un solo comando: BD vacía + servidor + Excel de muestra cargados.

Uso:
    python scripts/dev_local.py --dir <carpeta> [--port 8765] [--no-server]

Qué hace:
  1. Crea una BD vacía en <carpeta>/fide.db (si ya había una, la aparta como
     fide.db.bak-<fecha>; nunca la borra).
  2. Arranca uvicorn con el entorno de desarrollo (sin HTTPS, PII en texto
     plano, usuario auditor / Auditoria2026!).
  3. Espera /api/health, inicia sesión y sube por la carga única
     (/api/cargas/aplicar, confirmar=1) los Excel de muestra de la raíz del
     repo, imprimiendo el tipo detectado y las filas de cada uno.
  4. Limpia login_attempts y deja el servidor corriendo (o lo apaga con
     --no-server).

SOLO para desarrollo: la contraseña es pública y la PII queda sin cifrar. No
apuntes --dir a la carpeta de datos de producción.
Solo biblioteca estándar (urllib); el servidor usa lo de requirements.txt.
"""
import argparse
import http.cookiejar
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
USUARIO = "auditor"
CLAVE = "Auditoria2026!"

# Orden de carga: la cartera base va antes que la incremental (que se apoya en
# ella). El resto es independiente.
ORDEN = ["cartera_vieja", "cartera_nueva", "saldo_cartera", "recaudo", "recaudo_legacy",
         "solicitudes", "solicitudes_legacy", "juridico", "financieros"]
# Tipos donde cada archivo REEMPLAZA al anterior: si hay varios Excel del mismo
# tipo en la raíz se carga uno solo (el de esta lista si está; si no, el más
# reciente). Los estados de resultados sí se cargan todos (uno por año).
PREFERIDOS = {
    "cartera_vieja": "Informe cartera plataforma vieja.xlsx",
    "cartera_nueva": "Informe cartera nueva plataforma.xlsx",
    "recaudo": "Informe Pagos Nueva Plataforma.xlsx",
    "recaudo_legacy": "Informe de Pagos plataforma Anti.xlsx",
    "solicitudes": "Informe solicitudes nueva plataforma.xlsx",
    "solicitudes_legacy": "Informe de Solicitudes plataforma Anti.xlsx",
    "juridico": "Informe Procesos Judiciales.xlsx",
}
ACUMULABLES = {"financieros", "saldo_cartera"}


def _puerto_ocupado(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _preparar_bd(carpeta: Path) -> Path:
    carpeta.mkdir(parents=True, exist_ok=True)
    bd = carpeta / "fide.db"
    if bd.exists():
        aparte = carpeta / f"fide.db.bak-{datetime.now():%Y%m%d-%H%M%S}"
        bd.rename(aparte)
        print(f"  Ya había una BD: la aparté como {aparte.name}")
    for sufijo in ("-wal", "-shm"):
        resto = Path(str(bd) + sufijo)
        if resto.exists():
            resto.unlink()
    return bd


def _arrancar(bd: Path, port: int, log: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({
        "DATABASE_PATH": str(bd),
        "COOKIE_SECURE": "0",
        "ALLOW_PLAINTEXT_PII": "1",
        "APP_ENV": "development",
        "BOOTSTRAP_ADMIN_USER": USUARIO,
        "BOOTSTRAP_ADMIN_PASSWORD": CLAVE,
        "PYTHONUNBUFFERED": "1",
    })
    flags = 0
    if os.name == "nt":
        # Grupo propio: el servidor sobrevive al cierre de esta consola / Ctrl+C.
        flags = subprocess.CREATE_NEW_PROCESS_GROUP
    salida = open(log, "ab")
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(RAIZ), env=env, stdout=salida, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, creationflags=flags)


def _esperar_salud(base: str, proc: subprocess.Popen, log: Path, segundos: int = 90) -> None:
    limite = time.time() + segundos
    while time.time() < limite:
        if proc.poll() is not None:
            cola = log.read_text(encoding="utf-8", errors="replace")[-2000:]
            raise SystemExit(f"El servidor terminó al arrancar (código {proc.returncode}).\n{cola}")
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=3) as r:
                if json.load(r).get("status") == "ok":
                    return
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.5)
    raise SystemExit(f"El servidor no respondió /api/health en {segundos} s. Revisa {log}")


def _json(opener, req, timeout=900):
    """(status, cuerpo JSON). Los 4xx/5xx no lanzan: se devuelven."""
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        cuerpo = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(cuerpo)
        except ValueError:
            return e.code, {"detail": cuerpo[:300]}


def _login(opener, base: str) -> None:
    req = urllib.request.Request(
        base + "/api/auth/login", method="POST",
        data=json.dumps({"username": USUARIO, "password": CLAVE}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    status, cuerpo = _json(opener, req, timeout=30)
    if status != 200:
        raise SystemExit(f"No pude iniciar sesión como {USUARIO}: {status} {cuerpo.get('detail')}")


def _multipart(archivo: Path, campos: dict) -> tuple[bytes, str]:
    limite = "----fide" + uuid.uuid4().hex
    partes = []
    for k, v in campos.items():
        partes.append(f'--{limite}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode("utf-8"))
    nombre = archivo.name.replace('"', "")
    partes.append((f'--{limite}\r\nContent-Disposition: form-data; name="file"; filename="{nombre}"\r\n'
                   "Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n"
                   ).encode("utf-8"))
    partes.append(archivo.read_bytes())
    partes.append(f"\r\n--{limite}--\r\n".encode("utf-8"))
    return b"".join(partes), f"multipart/form-data; boundary={limite}"


def _subir(opener, base: str, ruta: str, archivo: Path, **campos):
    cuerpo, tipo = _multipart(archivo, campos)
    req = urllib.request.Request(base + ruta, data=cuerpo, method="POST",
                                 headers={"Content-Type": tipo})
    return _json(opener, req)


def _cargar_muestras(opener, base: str) -> None:
    archivos = sorted(p for p in RAIZ.glob("*.xlsx") if not p.name.startswith("~$"))
    if not archivos:
        print("  No hay Excel de muestra en la raíz del repo: la BD queda vacía.")
        return

    print(f"\nAnalizando {len(archivos)} Excel de la raíz del repo...")
    por_tipo: dict[str, list[tuple[Path, dict]]] = {}
    for a in archivos:
        status, an = _subir(opener, base, "/api/cargas/analizar", a)
        tipo = an.get("tipo") if status == 200 else None
        if not tipo:
            print(f"  ?  {a.name}: no reconocido ({status})")
            continue
        por_tipo.setdefault(tipo, []).append((a, an))

    print("\nCargando...")
    ok = fallos = 0
    for tipo in ORDEN:
        candidatos = por_tipo.get(tipo, [])
        if not candidatos:
            print(f"  -  {tipo:19s} (sin archivo de muestra)")
            continue
        if tipo not in ACUMULABLES and len(candidatos) > 1:
            elegido = next((c for c in candidatos if c[0].name == PREFERIDOS.get(tipo)), None) \
                or max(candidatos, key=lambda c: c[0].stat().st_mtime)
            for a, _ in candidatos:
                if a != elegido[0]:
                    print(f"  ~  {tipo:19s} {a.name}: omitido (mismo tipo que {elegido[0].name})")
            candidatos = [elegido]
        for a, an in candidatos:
            t0 = time.time()
            status, res = _subir(opener, base, "/api/cargas/aplicar", a, tipo=tipo, confirmar="1")
            filas = (an.get("validacion") or {}).get("registros_nuevo")
            if status == 200:
                ok += 1
                print(f"  OK {tipo:19s} {a.name}: {filas} registros "
                      f"(confianza {an.get('confianza')}, {time.time() - t0:.1f} s)")
            else:
                fallos += 1
                print(f"  X  {tipo:19s} {a.name}: {status} {str(res.get('detail'))[:160]}")
    print(f"\n{ok} archivos cargados, {fallos} con error.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Entorno local de FIDES Seguro con datos de muestra.")
    ap.add_argument("--dir", required=True, help="Carpeta donde se crea fide.db (y server.log)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-server", action="store_true",
                    help="Apaga el servidor al terminar (deja solo la BD cargada)")
    args = ap.parse_args()

    carpeta = Path(args.dir).resolve()
    base = f"http://127.0.0.1:{args.port}"
    if _puerto_ocupado(args.port):
        print(f"El puerto {args.port} ya está en uso: cierra ese servidor o usa --port.")
        return 1

    print(f"Preparando BD en {carpeta}")
    bd = _preparar_bd(carpeta)
    log = carpeta / "server.log"
    proc = _arrancar(bd, args.port, log)
    try:
        _esperar_salud(base, proc, log)
        print(f"Servidor arriba en {base} (PID {proc.pid}, log en {log})")
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        _login(opener, base)
        _cargar_muestras(opener, base)
        # Los intentos de login de este script no deben contar para el bloqueo por IP.
        with sqlite3.connect(str(bd), timeout=10) as conn:
            conn.execute("DELETE FROM login_attempts")
    except BaseException:
        proc.terminate()
        raise

    if args.no_server:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        print(f"Servidor apagado. BD lista en {bd}")
    else:
        print(f"\nListo: {base}  ·  usuario {USUARIO}  ·  contraseña {CLAVE}")
        detener = f"taskkill /PID {proc.pid} /F" if os.name == "nt" else f"kill {proc.pid}"
        print(f"El servidor queda corriendo. Para detenerlo: {detener}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
