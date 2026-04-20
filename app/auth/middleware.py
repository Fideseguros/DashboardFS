"""Authentication middleware for FastAPI."""
from fastapi import Request, HTTPException
from app.database import get_connection


def _load_session(request: Request) -> dict:
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        token = request.cookies.get("fide_token", "")
    if not token:
        raise HTTPException(status_code=401, detail="Token requerido")

    conn = get_connection()
    try:
        session = conn.execute(
            "SELECT s.*, u.username, u.display_name, u.role "
            "FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token = ? AND s.expires_at > datetime('now') AND u.is_active = 1",
            (token,)
        ).fetchone()
    finally:
        conn.close()

    if not session:
        raise HTTPException(status_code=401, detail="Sesion expirada o invalida")
    return dict(session)


def require_auth(request: Request):
    """Dependency that validates the session token."""
    return _load_session(request)


def require_superadmin(request: Request):
    """Dependency that requires role=superadmin."""
    session = _load_session(request)
    if session.get("role") != "superadmin":
        raise HTTPException(status_code=403, detail="Requiere permisos de superadmin")
    return session
