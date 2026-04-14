"""Authentication routes: login/logout."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import bcrypt as _bcrypt
import secrets
from datetime import datetime, timedelta
from app.database import get_db
from app.config import SESSION_EXPIRY_DAYS

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(req: LoginRequest):
    with get_db() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE username = ? AND is_active = 1",
            (req.username,)
        ).fetchone()

        if not user or not _bcrypt.checkpw(req.password.encode('utf-8'), user["password_hash"].encode('utf-8')):
            raise HTTPException(status_code=401, detail="Credenciales invalidas")

        token = secrets.token_urlsafe(32)
        expires = datetime.utcnow() + timedelta(days=SESSION_EXPIRY_DAYS)

        conn.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
            (token, user["id"], expires.isoformat())
        )
        conn.execute(
            "UPDATE users SET last_login = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), user["id"])
        )

        return {
            "token": token,
            "expires": expires.isoformat(),
            "name": user["display_name"] or user["username"]
        }


@router.post("/logout")
def logout(token: str = ""):
    if not token:
        return {"ok": True}
    with get_db() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    return {"ok": True}
