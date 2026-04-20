"""Field-level encryption for sensitive PII (identificacion, cliente).

Uses Fernet (AES-128-CBC + HMAC-SHA256). Key is derived from FIELD_ENCRYPTION_KEY env var.
If the key is not set, fields are stored in plaintext (dev-only fallback).
"""
import base64
import hashlib
import os
from cryptography.fernet import Fernet, InvalidToken

_FERNET: Fernet | None = None


def _get_fernet() -> Fernet | None:
    global _FERNET
    if _FERNET is not None:
        return _FERNET
    key_env = os.getenv("FIELD_ENCRYPTION_KEY", "")
    if not key_env:
        return None
    # Derive a stable 32-byte key from whatever the user provides, base64-encode for Fernet.
    digest = hashlib.sha256(key_env.encode("utf-8")).digest()
    fernet_key = base64.urlsafe_b64encode(digest)
    _FERNET = Fernet(fernet_key)
    return _FERNET


def encrypt(value: str | None) -> str | None:
    """Encrypt a string. Returns plaintext if no key is configured."""
    if value is None or value == "":
        return value
    f = _get_fernet()
    if f is None:
        return value
    return f.encrypt(str(value).encode("utf-8")).decode("utf-8")


def decrypt(value: str | None) -> str | None:
    """Decrypt a string. Passes through if no key or value is plaintext."""
    if value is None or value == "":
        return value
    f = _get_fernet()
    if f is None:
        return value
    try:
        return f.decrypt(str(value).encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        return value


def mask_identificacion(value: str | None) -> str:
    """Show only last 3 digits, e.g. 43739279 -> 43•••••279."""
    if not value:
        return ""
    s = str(value)
    if len(s) <= 5:
        return "•" * len(s)
    return f"{s[:2]}{'•' * (len(s) - 5)}{s[-3:]}"


def mask_cliente(value: str | None) -> str:
    """Show first name + initial of last name, e.g. 'SANDRA EUGENIA VILLEGAS' -> 'SANDRA E. V.'."""
    if not value:
        return ""
    parts = [p for p in str(value).split() if p]
    if len(parts) <= 1:
        return parts[0] if parts else ""
    return f"{parts[0]} " + " ".join(f"{p[0]}." for p in parts[1:])
