"""User management routes (superadmin only)."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
import bcrypt as _bcrypt
from app.database import get_db, get_connection
from app.auth.middleware import require_superadmin
from app.audit import log_audit, get_client_ip

router = APIRouter(prefix="/api/users", tags=["users"])

VALID_ROLES = {"superadmin", "viewer", "consulta"}


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=8, max_length=200)
    display_name: str = Field(max_length=100)
    role: str


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=100)
    role: str | None = None
    is_active: int | None = None
    password: str | None = Field(default=None, min_length=8, max_length=200)


def _hash(pwd: str) -> str:
    return _bcrypt.hashpw(pwd.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")


def _to_dict(row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "is_active": row["is_active"],
        "created_at": row["created_at"],
        "last_login": row["last_login"],
    }


@router.get("")
def list_users(_user=Depends(require_superadmin)):
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, username, display_name, role, is_active, created_at, last_login "
            "FROM users ORDER BY id"
        ).fetchall()
        return [_to_dict(r) for r in rows]
    finally:
        conn.close()


@router.post("")
def create_user(body: UserCreate, request: Request, user=Depends(require_superadmin)):
    if body.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Rol inválido. Valores: {', '.join(sorted(VALID_ROLES))}")
    username = body.username.strip().lower()
    ip = get_client_ip(request) or "unknown"
    with get_db() as conn:
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="El usuario ya existe")
        conn.execute(
            "INSERT INTO users (username, password_hash, display_name, role, is_active) "
            "VALUES (?, ?, ?, ?, 1)",
            (username, _hash(body.password), body.display_name.strip(), body.role)
        )
        new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        log_audit(user["user_id"], user["username"], "user_create",
                  f"id={new_id} username={username} role={body.role}", ip)
    return {"id": new_id, "ok": True}


@router.patch("/{user_id}")
def update_user(user_id: int, body: UserUpdate, request: Request, user=Depends(require_superadmin)):
    if body.role is not None and body.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail="Rol inválido")

    ip = get_client_ip(request) or "unknown"
    with get_db() as conn:
        target = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")

        # Prevent self-deactivation or self-demotion from superadmin
        if target["id"] == user["user_id"]:
            if body.is_active == 0:
                raise HTTPException(status_code=400, detail="No puedes desactivar tu propia cuenta")
            if body.role and body.role != "superadmin":
                raise HTTPException(status_code=400, detail="No puedes cambiar tu propio rol")

        updates, params = [], []
        if body.display_name is not None:
            updates.append("display_name = ?"); params.append(body.display_name.strip())
        if body.role is not None:
            updates.append("role = ?"); params.append(body.role)
        if body.is_active is not None:
            updates.append("is_active = ?"); params.append(1 if body.is_active else 0)
        if body.password is not None and body.password.strip():
            updates.append("password_hash = ?"); params.append(_hash(body.password))
            # Invalidate all existing sessions when password changes
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

        if not updates:
            return {"ok": True, "changes": 0}

        params.append(user_id)
        conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
        log_audit(user["user_id"], user["username"], "user_update",
                  f"id={user_id} fields={','.join(u.split(' ')[0] for u in updates)}", ip)
    return {"ok": True}


@router.delete("/{user_id}")
def delete_user(user_id: int, request: Request, user=Depends(require_superadmin)):
    ip = get_client_ip(request) or "unknown"
    if user_id == user["user_id"]:
        raise HTTPException(status_code=400, detail="No puedes eliminar tu propia cuenta")
    with get_db() as conn:
        target = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")
        # Preserve audit/history by soft-deleting: deactivate + remove sessions
        conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        log_audit(user["user_id"], user["username"], "user_deactivate",
                  f"id={user_id} username={target['username']}", ip)
    return {"ok": True}
