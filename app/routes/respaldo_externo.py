"""Respaldo cifrado FUERA de Railway.

El respaldo diario de app/backup.py vive en el mismo volumen que la base: si el
volumen o el proyecto se pierden, se van los dos. Por eso un equipo externo
descarga a diario una copia por esta ruta y la guarda fuera del servidor.

  GET /api/respaldo-externo   (cabecera  Authorization: Bearer <ficha>)

Cómo se protege:
  - La copia sale CIFRADA con la llave PÚBLICA de abajo (X25519 + HKDF-SHA256 +
    AES-256-GCM). La llave privada que la abre solo existe fuera del servidor:
    ni Railway ni este repositorio pueden descifrar los respaldos.
  - Para descargar hace falta una ficha de 256 bits; aquí solo se guarda su
    huella SHA-256 (no sirve para reconstruirla).
  - Cualquier fallo de ficha responde 404, igual que una ruta inexistente.
  - Cada descarga queda en la auditoría como acción crítica.

Formato del archivo: MAGIA | pública efímera (32) | nonce (12) | cifrado+etiqueta.
AAD = MAGIA. Llave AES = HKDF(compartido, sal = efímera || destinataria).
"""
import base64
import gzip
import hashlib
import hmac
import os
import secrets
import sqlite3
import tempfile
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from app.audit import CRITICAL_ACTIONS, get_client_ip, log_audit
from app.config import DATABASE_PATH

router = APIRouter(tags=["respaldo-externo"])

MAGIA = b"FIDES-X25519-AESGCM-1\n"
# Llave pública del destinatario (la privada no está en el servidor).
LLAVE_PUBLICA = base64.b64decode("/pKrjAoI6uPT/8AiebuGTV1Dzsx6PBEmxVtEZO30gU8=")
# SHA-256 de la ficha de descarga.
HUELLA_FICHA = "3789054303df861200adbafacb9f8470cb543106daa99a9bacdb8884ce2bfe22"

CRITICAL_ACTIONS.add("backup_external_download")


def _copia_comprimida() -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        ruta = os.path.join(tmp, "fide.db")
        src = sqlite3.connect(DATABASE_PATH, timeout=10.0)
        try:
            dst = sqlite3.connect(ruta)
            try:
                src.backup(dst)  # consistente aunque haya escrituras en curso
            finally:
                dst.close()
        finally:
            src.close()
        with open(ruta, "rb") as f:
            return gzip.compress(f.read(), compresslevel=6)


def cifrar(datos: bytes, publica: bytes = LLAVE_PUBLICA) -> bytes:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    efimera = X25519PrivateKey.generate()
    efimera_pub = efimera.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    compartido = efimera.exchange(X25519PublicKey.from_public_bytes(publica))
    llave = HKDF(algorithm=hashes.SHA256(), length=32, salt=efimera_pub + publica,
                 info=b"fides-respaldo").derive(compartido)
    nonce = secrets.token_bytes(12)
    return MAGIA + efimera_pub + nonce + AESGCM(llave).encrypt(nonce, datos, MAGIA)


@router.get("/api/respaldo-externo")
def respaldo_externo(request: Request):
    dada = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    huella = hashlib.sha256(dada.encode()).hexdigest()
    if not dada or not hmac.compare_digest(huella, HUELLA_FICHA):
        raise HTTPException(status_code=404, detail="Not Found")
    cuerpo = cifrar(_copia_comprimida())
    log_audit(None, "respaldo-externo", "backup_external_download",
              f"bytes={len(cuerpo):,}".replace(",", "."), get_client_ip(request) or "unknown")
    nombre = "fide_" + datetime.now().strftime("%Y%m%d_%H%M") + ".db.gz.x25519"
    return Response(cuerpo, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{nombre}"'})
