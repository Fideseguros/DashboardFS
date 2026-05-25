"""Field-level encryption for sensitive PII (identificacion, cliente).

Uses Fernet (AES-128-CBC + HMAC-SHA256). Key is derived from FIELD_ENCRYPTION_KEY env var
via PBKDF2-HMAC-SHA256 with a fixed salt. In production (APP_ENV=production), a missing
FIELD_ENCRYPTION_KEY raises at startup (fail-closed).
"""
import base64
import os
import logging
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

_FERNET: Fernet | None = None
_INIT_DONE = False
_log = logging.getLogger("fide.crypto")

# Fixed salt is acceptable because the purpose is key-stretching, not per-user hashing.
_KDF_SALT = b"fide-seguros-pii-salt-v1"
_KDF_ITERS = 600_000


def _init_fernet() -> Fernet | None:
    global _FERNET, _INIT_DONE
    if _INIT_DONE:
        return _FERNET
    _INIT_DONE = True
    key_env = os.getenv("FIELD_ENCRYPTION_KEY", "").strip()
    app_env = os.getenv("APP_ENV", "development").lower()
    if not key_env:
        if app_env == "production":
            raise RuntimeError(
                "FIELD_ENCRYPTION_KEY no está configurada en producción. "
                "Los datos PII no pueden almacenarse en texto plano."
            )
        _log.warning("FIELD_ENCRYPTION_KEY vacía — PII se guardará en texto plano (modo dev).")
        return None
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=_KDF_SALT, iterations=_KDF_ITERS)
    derived = kdf.derive(key_env.encode("utf-8"))
    _FERNET = Fernet(base64.urlsafe_b64encode(derived))
    return _FERNET


def encrypt(value: str | None) -> str | None:
    if value is None or value == "":
        return value
    f = _init_fernet()
    if f is None:
        return value
    return f.encrypt(str(value).encode("utf-8")).decode("utf-8")


def decrypt(value: str | None) -> str | None:
    """Decrypt a string. Returns None on InvalidToken (never the raw ciphertext)."""
    if value is None or value == "":
        return value
    f = _init_fernet()
    if f is None:
        return value
    try:
        return f.decrypt(str(value).encode("utf-8")).decode("utf-8")
    except InvalidToken:
        _log.warning("decrypt: InvalidToken — valor probablemente cifrado con otra clave")
        return None
    except Exception:
        _log.exception("decrypt: error inesperado")
        return None


def mask_identificacion(value: str | None) -> str:
    """Enmascarar identificación. Uso `*` (ASCII) en lugar de `•` para
    compatibilidad universal con Excel y CSV."""
    if not value:
        return ""
    s = str(value)
    if len(s) <= 5:
        return "*" * len(s)
    return f"{s[:2]}{'*' * (len(s) - 5)}{s[-3:]}"


def mask_cliente(value: str | None) -> str:
    if not value:
        return ""
    parts = [p for p in str(value).split() if p]
    if len(parts) <= 1:
        return parts[0] if parts else ""
    return f"{parts[0]} " + " ".join(f"{p[0]}." for p in parts[1:])
