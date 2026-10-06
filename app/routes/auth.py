"""Authentication routes: login/logout with rate limiting and secure cookies.

Oct-2026:
  - sessions.token guarda sha256(token) (ver app.auth.middleware.hash_token).
  - Verificación en dos pasos (TOTP, pyotp) OPT-IN por usuario. Si el usuario
    tiene totp_enabled, /login NO crea sesión: devuelve un pending_token y la
    sesión se crea en /login/2fa tras validar el código (o uno de recuperación).
  - Sin 2FA activado, el login sigue exactamente como antes.
"""
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
import bcrypt as _bcrypt
import hashlib
import json
import secrets
from datetime import datetime, timedelta

import pyotp

from app.database import get_db
from app.config import (SESSION_EXPIRY_HOURS, LOGIN_MAX_ATTEMPTS, LOGIN_LOCKOUT_MINUTES,
                        COOKIE_SECURE, TOTP_ISSUER, TOTP_PENDING_MINUTES, TOTP_MAX_FAILED)
from app.audit import log_audit, get_client_ip
from app.auth.middleware import hash_token, require_auth
from app.crypto import encrypt, decrypt

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Pre-computed bcrypt hash used for constant-time checks when the username does not exist.
# The plaintext "unreachable-dummy-password" never matches any real user.
_DUMMY_HASH = _bcrypt.hashpw(b"unreachable-dummy-password", _bcrypt.gensalt(rounds=12))

_USERNAME_MAX = 100
_RECOVERY_CODES = 8
_RECOVERY_LEN = 10
# Sin 0/O ni 1/I para que el usuario no se confunda al copiarlos a mano.
_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class LoginRequest(BaseModel):
    username: str
    password: str


class Login2FARequest(BaseModel):
    pending_token: str
    code: str


class TotpConfirmRequest(BaseModel):
    code: str


class TotpDisableRequest(BaseModel):
    password: str
    code: str


# ---------------------------------------------------------------- helpers ---

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
    conn.execute("DELETE FROM login_pending WHERE expires_at < datetime('now')")


def _hash_recovery(code: str) -> str:
    """sha256 del código de recuperación normalizado (mayúsculas, sin guiones ni espacios)."""
    norm = (code or "").strip().upper().replace("-", "").replace(" ", "")
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _generate_recovery_codes() -> tuple[list[str], str]:
    """Devuelve (códigos en claro, JSON de hashes para guardar)."""
    codes = ["".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_LEN))
             for _ in range(_RECOVERY_CODES)]
    return codes, json.dumps([_hash_recovery(c) for c in codes])


def _totp_ok(secret_enc: str | None, code: str) -> bool:
    """Valida un código TOTP con ventana ±1 paso (30 s)."""
    secret = decrypt(secret_enc) if secret_enc else None
    if not secret:
        return False
    digits = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(digits) != 6:
        return False
    return pyotp.TOTP(secret).verify(digits, valid_window=1)


def _recovery_ok(conn, user_row, code: str) -> bool:
    """Valida y CONSUME un código de recuperación. Devuelve True si coincidió."""
    raw = user_row["totp_recovery"]
    if not raw:
        return False
    try:
        hashes: list[str] = json.loads(raw)
    except (TypeError, ValueError):
        return False
    h = _hash_recovery(code)
    if h not in hashes:
        return False
    hashes = [x for x in hashes if x != h]
    conn.execute("UPDATE users SET totp_recovery = ? WHERE id = ?",
                 (json.dumps(hashes), user_row["id"]))
    return True


def _verify_second_factor(conn, user_row, code: str) -> str | None:
    """Devuelve 'totp', 'recovery' o None. El código de recuperación se consume."""
    if _totp_ok(user_row["totp_secret"], code):
        return "totp"
    if _recovery_ok(conn, user_row, code):
        return "recovery"
    return None


def _create_session(user, ip: str, response: Response) -> dict:
    """Crea la sesión (token hasheado en BD), pone la cookie y devuelve el payload de login."""
    token = secrets.token_urlsafe(32)
    expires = datetime.utcnow() + timedelta(hours=SESSION_EXPIRY_HOURS)
    invalidated = 0
    with get_db() as conn:
        # Auditoría A5: invalidar sesiones previas del mismo usuario.
        # Si un laptop quedó comprometido con sesión viva, un re-login
        # desde el equipo legítimo cierra la sesión hostil al instante.
        # Política: single-session por usuario.
        old_sessions = conn.execute(
            "SELECT COUNT(*) as cnt FROM sessions WHERE user_id = ?",
            (user["id"],)
        ).fetchone()
        invalidated = old_sessions["cnt"] if old_sessions else 0
        if invalidated:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))

        conn.execute(
            "INSERT INTO sessions (token, user_id, ip, expires_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (hash_token(token), user["id"], ip, expires.isoformat())
        )
        conn.execute(
            "UPDATE users SET last_login = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), user["id"])
        )

    if invalidated:
        log_audit(user["id"], user["username"], "session_invalidated",
                  f"sesiones previas cerradas: {invalidated}", ip)
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


# ------------------------------------------------------------------ login ---

@router.post("/login")
def login(req: LoginRequest, request: Request, response: Response):
    ip = get_client_ip(request) or "unknown"
    username = (req.username or "").strip()[:_USERNAME_MAX]

    # Fase 1: validar y REGISTRAR el intento en su propia transacción.
    # Antes, el `raise HTTPException` ocurría dentro del `with get_db()`,
    # lo que disparaba rollback y borraba el intento fallido: el límite de
    # intentos (429) nunca se alcanzaba. Ahora el intento queda guardado
    # pase lo que pase, y los HTTPException se lanzan fuera del bloque.
    error: HTTPException | None = None
    user = None
    with get_db() as conn:
        _cleanup_stale(conn)

        if _is_ip_locked(conn, ip) or _is_user_locked(conn, username):
            error = HTTPException(
                status_code=429,
                detail=f"Demasiados intentos fallidos. Intenta en {LOGIN_LOCKOUT_MINUTES} minutos.")
        else:
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
                error = HTTPException(status_code=401, detail="Credenciales invalidas")

    # log_audit abre su propia conexión: llamarlo FUERA del `with` evita que
    # espere el busy_timeout (5 s) por el lock de escritura de la conexión anterior.
    if error is not None:
        if error.status_code == 429:
            log_audit(None, username, "login_blocked_rate_limit",
                      "bloqueo por intentos repetidos", ip)
        else:
            log_audit(user["id"] if user else None, username, "login_failed",
                      "credenciales inválidas", ip)
        raise error

    # Fase 1b: si el usuario tiene 2FA, NO hay sesión todavía. Entregamos un
    # pending_token opaco (solo su hash va a la BD) que vence en 5 minutos.
    if user["totp_enabled"]:
        pending = secrets.token_urlsafe(32)
        pending_exp = datetime.utcnow() + timedelta(minutes=TOTP_PENDING_MINUTES)
        with get_db() as conn:
            conn.execute(
                "INSERT INTO login_pending (token_hash, user_id, ip, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (hash_token(pending), user["id"], ip, pending_exp.strftime("%Y-%m-%d %H:%M:%S"))
            )
        log_audit(user["id"], user["username"], "login_totp_pending",
                  "credenciales válidas, esperando código", ip)
        return {"requires_totp": True, "pending_token": pending}

    # Fase 2: crear la sesión.
    return _create_session(user, ip, response)


@router.post("/login/2fa")
def login_2fa(req: Login2FARequest, request: Request, response: Response):
    """Segundo paso del login: pending_token + código TOTP (o de recuperación)."""
    ip = get_client_ip(request) or "unknown"
    token_hash = hash_token((req.pending_token or "").strip())

    error: HTTPException | None = None
    audit: tuple | None = None
    user = None
    method = None
    with get_db() as conn:
        _cleanup_stale(conn)
        pending = conn.execute(
            "SELECT * FROM login_pending WHERE token_hash = ? AND expires_at > datetime('now')",
            (token_hash,)
        ).fetchone()
        if not pending:
            error = HTTPException(status_code=401,
                                  detail="Verificación expirada o inválida. Inicia sesión de nuevo.")
        else:
            user = conn.execute(
                "SELECT * FROM users WHERE id = ? AND is_active = 1 AND totp_enabled = 1",
                (pending["user_id"],)
            ).fetchone()
            method = _verify_second_factor(conn, user, req.code) if user else None
            if method:
                conn.execute("DELETE FROM login_pending WHERE token_hash = ?", (token_hash,))
            else:
                failed = (pending["failed"] or 0) + 1
                if failed >= TOTP_MAX_FAILED or not user:
                    # Límite de códigos fallidos: el pending se invalida y toca
                    # volver a empezar con usuario + contraseña.
                    conn.execute("DELETE FROM login_pending WHERE token_hash = ?", (token_hash,))
                    audit = (pending["user_id"], user["username"] if user else None, "2fa_failed",
                             f"verificación invalidada tras {failed} códigos fallidos", ip)
                    error = HTTPException(status_code=401,
                                          detail="Demasiados códigos incorrectos. Inicia sesión de nuevo.")
                else:
                    conn.execute("UPDATE login_pending SET failed = ? WHERE token_hash = ?",
                                 (failed, token_hash))
                    error = HTTPException(status_code=401, detail="Código incorrecto")

    if audit:
        log_audit(*audit)
    if error is not None:
        raise error

    if method == "recovery":
        remaining = len(json.loads(user["totp_recovery"] or "[]")) - 1
        log_audit(user["id"], user["username"], "2fa_recovery_used",
                  f"códigos de recuperación restantes: {max(remaining, 0)}", ip)
    return _create_session(user, ip, response)


@router.post("/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get("fide_token", "") or request.headers.get("Authorization", "").replace("Bearer ", "")
    ip = get_client_ip(request) or "unknown"
    if token:
        token_hash = hash_token(token)
        with get_db() as conn:
            row = conn.execute(
                "SELECT s.user_id, u.username FROM sessions s "
                "JOIN users u ON u.id = s.user_id WHERE s.token = ?", (token_hash,)
            ).fetchone()
            conn.execute("DELETE FROM sessions WHERE token = ?", (token_hash,))
        if row:
            log_audit(row["user_id"], row["username"], "logout", "", ip)
    response.delete_cookie("fide_token", path="/")
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    session = require_auth(request)
    return {
        "username": session["username"],
        "name": session["display_name"] or session["username"],
        "role": session["role"],
        "totp_enabled": bool(session.get("totp_enabled"))
    }


# -------------------------------------------------------------- 2FA (TOTP) ---

@router.post("/2fa/setup")
def totp_setup(request: Request):
    """Genera un secreto TOTP pendiente de confirmar. El frontend dibuja el QR del otpauth_url."""
    session = require_auth(request)
    ip = get_client_ip(request) or "unknown"
    secret = pyotp.random_base32()
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        if user["totp_enabled"]:
            raise HTTPException(status_code=400,
                                detail="La verificación en dos pasos ya está activa. Desactívala primero.")
        conn.execute(
            "UPDATE users SET totp_secret = ?, totp_enabled = 0, totp_recovery = NULL WHERE id = ?",
            (encrypt(secret), user["id"])
        )
    log_audit(user["id"], user["username"], "2fa_setup", "secreto generado, pendiente de confirmar", ip)
    uri = pyotp.TOTP(secret).provisioning_uri(name=user["username"], issuer_name=TOTP_ISSUER)
    return {"secret": secret, "otpauth_url": uri}


@router.post("/2fa/confirm")
def totp_confirm(req: TotpConfirmRequest, request: Request):
    """Confirma el secreto pendiente con un código válido y activa el 2FA.

    Devuelve los 8 códigos de recuperación UNA sola vez en claro.
    """
    session = require_auth(request)
    ip = get_client_ip(request) or "unknown"
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        if user["totp_enabled"]:
            raise HTTPException(status_code=400, detail="La verificación en dos pasos ya está activa.")
        if not user["totp_secret"]:
            raise HTTPException(status_code=400, detail="Primero genera el código QR (setup).")
        if not _totp_ok(user["totp_secret"], req.code):
            raise HTTPException(status_code=400,
                                detail="Código incorrecto. Revisa la hora del teléfono e inténtalo de nuevo.")
        codes, hashes_json = _generate_recovery_codes()
        conn.execute(
            "UPDATE users SET totp_enabled = 1, totp_recovery = ? WHERE id = ?",
            (hashes_json, user["id"])
        )
    log_audit(user["id"], user["username"], "2fa_enabled", "", ip)
    return {"ok": True, "recovery_codes": codes}


@router.post("/2fa/disable")
def totp_disable(req: TotpDisableRequest, request: Request):
    """Desactiva el 2FA: exige contraseña actual + código TOTP válido o de recuperación."""
    session = require_auth(request)
    ip = get_client_ip(request) or "unknown"
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        if not user["totp_enabled"]:
            raise HTTPException(status_code=400, detail="La verificación en dos pasos no está activa.")
        if not _bcrypt.checkpw((req.password or "").encode("utf-8"),
                               user["password_hash"].encode("utf-8")):
            raise HTTPException(status_code=401, detail="Contraseña incorrecta")
        if not _verify_second_factor(conn, user, req.code):
            raise HTTPException(status_code=400, detail="Código incorrecto")
        conn.execute(
            "UPDATE users SET totp_secret = NULL, totp_enabled = 0, totp_recovery = NULL WHERE id = ?",
            (user["id"],)
        )
    log_audit(user["id"], user["username"], "2fa_disabled", "", ip)
    return {"ok": True}
