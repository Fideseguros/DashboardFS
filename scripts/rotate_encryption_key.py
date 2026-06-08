"""Rotación de FIELD_ENCRYPTION_KEY — descifra con clave vieja, cifra con nueva.

Cuándo usar
-----------
- La clave actual se filtró (Railway panel comprometido, dev se llevó copia).
- Política periódica de rotación (anual, semestral).
- Antes de ofrecer acceso a un consultor externo.

Procedimiento seguro
--------------------
1) Hacer BACKUP de la BD antes de correr este script:
       cp data/fide.db data/fide.db.backup-$(date +%Y%m%d)

2) En Railway o local, configurar AMBAS variables temporalmente:
       FIELD_ENCRYPTION_KEY_OLD = "<clave anterior>"
       FIELD_ENCRYPTION_KEY     = "<clave nueva>"

3) Detener el servicio web (o ponerlo en mantenimiento). La BD no debe
   recibir writes durante la rotación.

4) Correr este script:
       python -m scripts.rotate_encryption_key

5) Verificar que el conteo de filas re-cifradas coincida con el total
   esperado por tabla. Si hay rows con InvalidToken (cifradas con una
   tercera clave desconocida), quedan marcadas en el reporte final.

6) Quitar FIELD_ENCRYPTION_KEY_OLD del entorno una vez confirmado el
   reinicio del servicio.

Limitaciones
------------
- El script asume que SOLO HAY DOS claves activas (vieja y nueva). Si hay
  un tercera generación previa, hay que correr en cadena.
- Si la BD recibe writes mientras corre, rows nuevos quedan cifrados con
  la nueva pero no se re-procesan; el script es idempotente, basta volver
  a correrlo.
- mask_identificacion/mask_cliente cacheadas en credits NO se tocan: ya
  son strings públicos (no cifrados); se re-generan al próximo upload.
"""
import os
import sys
import sqlite3
import base64
import logging
from contextlib import contextmanager
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
_log = logging.getLogger("rotate")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import DATABASE_PATH

_KDF_SALT = b"fide-seguros-pii-salt-v1"
_KDF_ITERS = 600_000

# Mismas tablas que app/routes/habeas_data.py — mantener sincronizado.
PII_COLUMNS = [
    ("credits",            ["identificacion", "cliente"]),
    ("pagos",              ["identificacion", "cliente"]),
    ("solicitudes",        ["identificacion", "solicitante"]),
    ("pagos_legacy",       ["identificacion", "nombre"]),
    ("solicitudes_legacy", ["identificacion", "nombre_completo"]),
    ("procesos_juridicos", ["identificacion", "nombre"]),
]


def _make_fernet(key: str) -> Fernet:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=_KDF_SALT, iterations=_KDF_ITERS)
    derived = kdf.derive(key.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(derived))


def main():
    old_key = os.getenv("FIELD_ENCRYPTION_KEY_OLD", "").strip()
    new_key = os.getenv("FIELD_ENCRYPTION_KEY", "").strip()
    if not old_key or not new_key:
        _log.error("FALTA: FIELD_ENCRYPTION_KEY_OLD y FIELD_ENCRYPTION_KEY deben estar seteadas.")
        sys.exit(1)
    if old_key == new_key:
        _log.error("FIELD_ENCRYPTION_KEY_OLD == FIELD_ENCRYPTION_KEY — nada que rotar.")
        sys.exit(1)

    f_old = _make_fernet(old_key)
    f_new = _make_fernet(new_key)
    _log.info("Backup recomendado: cp data/fide.db data/fide.db.backup-...")
    _log.info("DB: %s", DATABASE_PATH)
    _log.info("Tablas a procesar: %s", [t for t, _ in PII_COLUMNS])

    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    total_ok = total_skip = total_invalid = 0
    report = {}
    try:
        for table, cols in PII_COLUMNS:
            t_ok = t_skip = t_invalid = 0
            for col in cols:
                rows = conn.execute(
                    f"SELECT id, {col} FROM {table} WHERE {col} IS NOT NULL AND {col} != ''"
                ).fetchall()
                _log.info("  %s.%s: %d filas a evaluar", table, col, len(rows))
                for r in rows:
                    ct = r[col]
                    try:
                        plain = f_old.decrypt(ct.encode("utf-8")).decode("utf-8")
                    except InvalidToken:
                        # Probablemente ya cifrada con la nueva (script ya corrió antes)
                        try:
                            f_new.decrypt(ct.encode("utf-8"))
                            t_skip += 1
                            continue
                        except InvalidToken:
                            t_invalid += 1
                            _log.warning("    %s.%s id=%s: InvalidToken con ambas claves",
                                         table, col, r["id"])
                            continue
                    new_ct = f_new.encrypt(plain.encode("utf-8")).decode("utf-8")
                    conn.execute(
                        f"UPDATE {table} SET {col} = ? WHERE id = ?",
                        (new_ct, r["id"])
                    )
                    t_ok += 1
                conn.commit()
            report[table] = {"re_cifradas": t_ok, "ya_nuevas": t_skip, "invalid": t_invalid}
            total_ok += t_ok; total_skip += t_skip; total_invalid += t_invalid
    finally:
        conn.close()

    _log.info("=" * 60)
    _log.info("REPORTE DE ROTACIÓN")
    for t, r in report.items():
        _log.info("  %-20s re=%d skip=%d invalid=%d", t, r["re_cifradas"], r["ya_nuevas"], r["invalid"])
    _log.info("  TOTAL  re_cifradas=%d  ya_nuevas=%d  invalid=%d",
              total_ok, total_skip, total_invalid)
    if total_invalid > 0:
        _log.warning("HAY %d filas con InvalidToken — revisa manualmente antes de "
                     "retirar FIELD_ENCRYPTION_KEY_OLD del entorno.", total_invalid)
        sys.exit(2)
    _log.info("OK. Ya puedes retirar FIELD_ENCRYPTION_KEY_OLD del entorno.")


if __name__ == "__main__":
    main()
