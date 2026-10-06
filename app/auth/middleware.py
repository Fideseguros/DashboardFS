"""Authentication middleware for FastAPI.

Seguridad de sesiones (oct-2026):
  - sessions.token guarda sha256(token). Un volcado de la BD no sirve para
    secuestrar sesiones vivas. Usar hash_token() en TODO acceso a la tabla.
  - Expiración deslizante: una sesión muere si pasa SESSION_IDLE_MINUTES sin
    actividad, además del máximo absoluto SESSION_EXPIRY_HOURS. last_seen_at
    se actualiza como máximo cada SESSION_TOUCH_SECONDS para no escribir en
    cada request.
"""
import hashlib
from datetime import datetime, timedelta
from fastapi import Request, HTTPException
from app.database import get_connection
from app.config import SESSION_IDLE_MINUTES, SESSION_TOUCH_SECONDS

_SQLITE_FMT = "%Y-%m-%d %H:%M:%S"


def hash_token(token: str) -> str:
    """sha256 hex del token de sesión. Es lo ÚNICO que se guarda en sessions.token."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _parse_sqlite_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:19], _SQLITE_FMT)
    except ValueError:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None


def _extract_token(request: Request) -> str:
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        token = request.cookies.get("fide_token", "")
    return token


def _load_session(request: Request) -> dict:
    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Token requerido")

    token_hash = hash_token(token)
    conn = get_connection()
    try:
        session = conn.execute(
            "SELECT s.*, u.username, u.display_name, u.role, u.totp_enabled "
            "FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token = ? AND s.expires_at > datetime('now') AND u.is_active = 1 "
            # Inactividad: COALESCE cubre filas migradas sin last_seen_at.
            "AND COALESCE(s.last_seen_at, s.created_at) > datetime('now', ?)",
            (token_hash, f"-{int(SESSION_IDLE_MINUTES)} minutes")
        ).fetchone()

        if session:
            # Refrescar last_seen_at, pero no en cada request: solo si el último
            # toque tiene más de SESSION_TOUCH_SECONDS.
            last_seen = _parse_sqlite_dt(session["last_seen_at"] or session["created_at"])
            if last_seen is None or (datetime.utcnow() - last_seen) >= timedelta(seconds=SESSION_TOUCH_SECONDS):
                conn.execute(
                    "UPDATE sessions SET last_seen_at = datetime('now') WHERE token = ?",
                    (token_hash,)
                )
                conn.commit()
    finally:
        conn.close()

    if not session:
        raise HTTPException(status_code=401, detail="Sesion expirada o invalida")
    out = dict(session)
    out["totp_enabled"] = bool(out.get("totp_enabled"))
    return out


def require_auth(request: Request):
    """Dependency that validates the session token."""
    return _load_session(request)


def require_superadmin(request: Request):
    """Dependency that requires role=superadmin."""
    session = _load_session(request)
    if session.get("role") != "superadmin":
        raise HTTPException(status_code=403, detail="Requiere permisos de superadmin")
    return session
