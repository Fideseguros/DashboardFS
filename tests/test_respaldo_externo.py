"""Respaldo cifrado fuera de Railway (/api/respaldo-externo)."""
import gzip
import hashlib
import sqlite3
import tempfile
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.routes import respaldo_externo as rx


def _descifrar(blob: bytes, privada: X25519PrivateKey) -> bytes:
    assert blob.startswith(rx.MAGIA)
    resto = blob[len(rx.MAGIA):]
    efimera_pub, nonce, ct = resto[:32], resto[32:44], resto[44:]
    publica = privada.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    compartido = privada.exchange(X25519PublicKey.from_public_bytes(efimera_pub))
    llave = HKDF(algorithm=hashes.SHA256(), length=32, salt=efimera_pub + publica,
                 info=b"fides-respaldo").derive(compartido)
    return AESGCM(llave).decrypt(nonce, ct, rx.MAGIA)


def test_sin_ficha_responde_404(client):
    assert client.get("/api/respaldo-externo").status_code == 404
    r = client.get("/api/respaldo-externo", headers={"Authorization": "Bearer otra-cosa"})
    assert r.status_code == 404


def test_descarga_cifrada_y_restaurable(client, monkeypatch):
    privada = X25519PrivateKey.generate()
    publica = privada.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ficha = "ficha-de-prueba-123"
    monkeypatch.setattr(rx, "LLAVE_PUBLICA", publica)
    monkeypatch.setattr(rx, "HUELLA_FICHA", hashlib.sha256(ficha.encode()).hexdigest())
    monkeypatch.setattr(rx.cifrar, "__defaults__", (publica,))

    r = client.get("/api/respaldo-externo", headers={"Authorization": f"Bearer {ficha}"})
    assert r.status_code == 200, r.text
    assert b"SQLite format 3" not in r.content  # nunca en claro
    db = gzip.decompress(_descifrar(r.content, privada))
    assert db.startswith(b"SQLite format 3")
    with tempfile.TemporaryDirectory() as tmp:
        ruta = os.path.join(tmp, "r.db")
        open(ruta, "wb").write(db)
        con = sqlite3.connect(ruta)
        try:
            assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='users'").fetchone()[0] == 1
        finally:
            con.close()
