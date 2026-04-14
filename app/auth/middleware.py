"""Authentication middleware for FastAPI."""
from fastapi import Request, HTTPException
from app.database import get_connection


def require_auth(request: Request):
    """Dependency that validates the session token."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        # Also check cookie for browser-based access
        token = request.cookies.get("fide_token", "")
    if not token:
        raise HTTPException(status_code=401, detail="Token requerido")

    conn = get_connection()
    try:
        session = conn.execute(
            "SELECT s.*, u.username, u.display_name FROM sessions s "
            "JOIN users u ON u.id = s.user_id "
            "WHERE s.token = ? AND s.expires_at > datetime('now') AND u.is_active = 1",
            (token,)
        ).fetchone()
    finally:
        conn.close()

    if not session:
        raise HTTPException(status_code=401, detail="Sesion expirada o invalida")

    return dict(session)
