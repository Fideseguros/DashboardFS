"""Authentication routes: login/logout with rate limiting and secure cookies."""
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
import bcrypt as _bcrypt
import secrets
from datetime import datetime, timedelta
from app.database import get_db
from app.config import SESSION_EXPIRY_HOURS, LOGIN_MAX_ATTEMPTS, LOGIN_LOCKOUT_MINUTES, COOKIE_SECURE
from app.audit import log_audit, get_client_ip

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Pre-computed bcrypt hash used for constant-time checks when the username does not exist.
# The plaintext "unreachable-dummy-password" never matches any real user.
_DUMMY_HASH = _bcrypt.hashpw(b"unreachable-dummy-password", _bcrypt.gensalt(rounds=12))

_USERNAME_MAX = 100


class LoginRequest(BaseModel):
    username: str
    password: str


def _is_ip_locked(conn, ip: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM login_attempts "
        "WHERE ip = ? AND success = 0 AND attempted_at > datetime('now', ?)",
        (ip, f'-{int(LOGIN_LOCKOUT_MINUTES)} minutes')
    ).fetchone()
    return (row["cnt"] if row else 0) >= LOGIN_MAX_ATTEMPTS


def _is_user_locked(conn, username: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM login_attempts "
        "WHERE username = ? AND success = 0 AND attempted_at > datetime('now', ?)",
        (username[:_USERNAME_MAX], f'-{int(LOGIN_LOCKOUT_MINUTES)} minutes')
    ).fetchone()
    # Slightly higher limit per username to allow users across multiple offices.
    return (row["cnt"] if row else 0) >= (LOGIN_MAX_ATTEMPTS * 2)


def _record_attempt(conn, ip: str, username: str, success: bool):
    conn.execute(
        "INSERT INTO login_attempts (ip, username, success) VALUES (?, ?, ?)",
        (ip[:80], username[:_USERNAME_MAX], 1 if success else 0)
    )


def _cleanup_stale(conn):
    conn.execute("DELETE FROM sessions WHERE expires_at < datetime('now')")
    conn.execute("DELETE FROM login_attempts WHERE attempted_at < datetime('now', '-24 hours')")


@router.post("/login")
def login(req: LoginRequest, request: Request, response: Response):
    ip = get_client_ip(request) or "unknown"
    username = (req.username or "").strip()[:_USERNAME_MAX]

    with get_db() as conn:
        _cleanup_stale(conn)

        if _is_ip_locked(conn, ip) or _is_user_locked(conn, username):
            log_audit(None, username, "login_blocked_rate_limit",
                      f"bloqueo por intentos repetidos", ip)
            raise HTTPException(status_code=429,
                                detail=f"Demasiados intentos fallidos. Intenta en {LOGIN_LOCKOUT_MINUTES} minutos.")

        user = conn.execute(
            "SELECT * FROM users WHERE username = ? AND is_active = 1",
            (username,)
        ).fetchone()

        # Constant-time credential check: if user is None, compare against dummy hash
        # so response timing does not reveal whether the username exists.
        hash_to_check = user["password_hash"].encode("utf-8") if user else _DUMMY_HASH
        password_ok = _bcrypt.checkpw(req.password.encode("utf-8"), hash_to_check)
        valid = bool(user and password_ok)

        _record_attempt(conn, ip, username, valid)

        if not valid:
            log_audit(user["id"] if user else None, username, "login_failed", "credenciales inválidas", ip)
            raise HTTPException(status_code=401, detail="Credenciales invalidas")

        token = secrets.token_urlsafe(32)
        expires = datetime.utcnow() + timedelta(hours=SESSION_EXPIRY_HOURS)

        conn.execute(
            "INSERT INTO sessions (token, user_id, ip, expires_at) VALUES (?, ?, ?, ?)",
            (token, user["id"], ip, expires.isoformat())
        )
        conn.execute(
            "UPDATE users SET last_login = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), user["id"])
        )
        log_audit(user["id"], user["username"], "login_success", "", ip)

    response.set_cookie(
        key="fide_token",
        value=token,
        max_age=SESSION_EXPIRY_HOURS * 3600,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        path="/"
    )

    return {
        "expires": expires.isoformat(),
        "name": user["display_name"] or user["username"],
        "role": user["role"]
    }


@router.post("/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get("fide_token", "") or request.headers.get("Authorization", "").replace("Bearer ", "")
    ip = get_client_ip(request) or "unknown"
    if token:
        with get_db() as conn:
            row = conn.execute(
                "SELECT s.user_id, u.username FROM sessions s "
                "JOIN users u ON u.id = s.user_id WHERE s.token = ?", (token,)
            ).fetchone()
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            if row:
                log_audit(row["user_id"], row["username"], "logout", "", ip)
    response.delete_cookie("fide_token", path="/")
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    from app.auth.middleware import require_auth
    session = require_auth(request)
    return {
        "username": session["username"],
        "name": session["display_name"] or session["username"],
        "role": session["role"]
    }
