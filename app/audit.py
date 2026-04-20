"""Audit logging helper for security-relevant actions."""
from fastapi import Request
from app.database import get_db


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
    """Insert an audit entry. Never raises — audit failures must not block user actions."""
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO audit_logs (user_id, username, action, details, ip) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, username, action, details[:1000], ip)
            )
    except Exception:
        pass
