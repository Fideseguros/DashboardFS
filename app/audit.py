"""Audit logging helper for security-relevant actions.

Política: las acciones críticas (reveal, export, delete, upload, login_*) no
deben perder rastros. Si la BD falla, se escribe a un archivo fallback en el
volumen (que persiste entre redeploys).

Habeas Data (M1/M2): los `details` pueden contener identificaciones reales
(ej. credit_reveal con la cédula). Para trazabilidad sin acumular PII en
plaintext indefinidamente en audit_logs:
  - Las identificaciones se hashean SHA-256 truncado a 12 chars (suficiente
    para correlación, no reversible).
  - El hash usa una clave secreta (AUDIT_PII_PEPPER env var, fallback a
    FIELD_ENCRYPTION_KEY) para que un atacante con read-only al audit_logs
    no pueda construir rainbow table.
"""
import hashlib
import hmac
import logging
import os
import re
from datetime import datetime
from fastapi import Request
from app.database import get_db
from app.config import DATABASE_PATH

_log = logging.getLogger("fide.audit")


def _pii_pepper() -> bytes:
    """Pepper para hashear PII en audit logs. Fallback a key de cifrado de
    PII (siempre presente en producción)."""
    p = os.getenv("AUDIT_PII_PEPPER") or os.getenv("FIELD_ENCRYPTION_KEY") or "fide-audit-fallback"
    return p.encode("utf-8")


def _hash_id(s: str) -> str:
    """HMAC-SHA256 truncado a 12 hex chars para identificaciones en audit."""
    return hmac.new(_pii_pepper(), s.encode("utf-8"), hashlib.sha256).hexdigest()[:12]


# Patrón: secuencias de 5-12 dígitos (típicas cédulas/NITs) en `details`.
# Las hasheamos antes de persistir para no dejar PII en plaintext.
_ID_PATTERN = re.compile(r'\b\d{5,12}\b')


def _scrub_details(details: str) -> str:
    """Reemplaza identificaciones numéricas en `details` por su hash HMAC.

    Mantiene contexto (action, motivo, counts) pero impide que un dump de
    audit_logs revele cédulas en plaintext. Idempotente.
    """
    if not details:
        return details
    return _ID_PATTERN.sub(lambda m: f"id={_hash_id(m.group(0))}", details)

# Acciones que NUNCA deben perder rastro — si la BD falla, escribimos a archivo
CRITICAL_ACTIONS = {
    "credit_reveal", "credit_reveal_denied", "csv_export",
    "excel_upload", "recaudo_legacy_upload", "solicitudes_legacy_upload",
    "juridico_upload", "financieros_upload",
    "user_create", "user_update", "user_deactivate",
    "login_blocked_rate_limit",
}


def _fallback_log_path() -> str:
    """Path del archivo fallback: junto a la BD, en el volumen persistente."""
    db_dir = os.path.dirname(DATABASE_PATH) or "."
    return os.path.join(db_dir, "audit_fallback.log")


def _write_fallback(user_id, username, action, details, ip, error):
    # `details` ya viene scrubbed desde log_audit; este es el camino fallback
    # cuando la BD falla, así que también queda sin PII en plaintext.
    try:
        with open(_fallback_log_path(), "a", encoding="utf-8") as f:
            f.write(
                f"{datetime.utcnow().isoformat()}|user_id={user_id}|username={username}|"
                f"action={action}|ip={ip}|details={details[:500]}|"
                f"db_error={type(error).__name__}:{str(error)[:200]}\n"
            )
    except Exception:
        # Si ni el archivo se puede escribir, al menos al logger del servidor
        _log.critical(
            "AUDIT_FAIL action=%s user=%s ip=%s details=%s",
            action, username, ip, details[:200]
        )


def get_client_ip(request: Request) -> str:
    """Return the client IP, honoring X-Forwarded-For when behind a proxy."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip", "")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else ""


def log_audit(user_id: int | None, username: str | None, action: str,
              details: str = "", ip: str = "") -> None:
    """Insert an audit entry. Never raises — audit failures must not block user actions.

    Para acciones críticas, si la inserción a la BD falla, escribimos a un
    archivo fallback en el volumen (audit_fallback.log) y emitimos un log
    de severidad CRITICAL al logger del servidor.

    Habeas Data: los `details` se pasan por _scrub_details() que hashea
    identificaciones numéricas (HMAC con pepper). Mantiene trazabilidad
    sin acumular PII en plaintext indefinidamente.
    """
    safe_details = _scrub_details(details or "")[:1000]
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO audit_logs (user_id, username, action, details, ip) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, username, action, safe_details, ip)
            )
    except Exception as e:
        if action in CRITICAL_ACTIONS:
            _write_fallback(user_id, username, action, safe_details, ip, e)
        else:
            _log.warning(
                "audit_logs insert failed for action=%s (non-critical): %s",
                action, type(e).__name__
            )
