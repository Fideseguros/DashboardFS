"""Tests de RBAC: superadmin vs viewer vs consulta vs anónimo."""
import pytest


_VALID_USER_PAYLOAD = {
    "username": "valid_user",
    "password": "validpassword123",
    "display_name": "Valid User",
    "role": "viewer",
}

# Endpoints solo-superadmin
SUPERADMIN_ONLY = [
    ("POST", "/api/users", {"json": _VALID_USER_PAYLOAD}),
    ("GET", "/api/juridico", {}),
    ("GET", "/api/juridico/summary", {}),
    ("GET", "/api/juridico/cartera-summary", {}),
]

# Endpoints accesibles a cualquier usuario autenticado
AUTH_ANY = [
    ("GET", "/api/auth/me", {}),
    ("GET", "/api/credits", {}),
    ("GET", "/api/recaudo", {}),
    ("GET", "/api/recaudo/summary", {}),
    ("GET", "/api/solicitudes", {}),
    ("GET", "/api/solicitudes/summary", {}),
    ("GET", "/api/sync/status", {}),
]


@pytest.mark.parametrize("method,path,kwargs", SUPERADMIN_ONLY)
def test_superadmin_only_blocks_anon(client, method, path, kwargs):
    """Endpoints superadmin sin sesión → 401."""
    res = client.request(method, path, **kwargs)
    assert res.status_code == 401, f"{method} {path} debería ser 401 sin auth"


@pytest.mark.parametrize("method,path,kwargs", SUPERADMIN_ONLY)
def test_superadmin_only_blocks_viewer(viewer_client, method, path, kwargs):
    """Endpoints superadmin con sesión viewer → 403."""
    res = viewer_client.request(method, path, **kwargs)
    assert res.status_code == 403, f"{method} {path} debería ser 403 para viewer (got {res.status_code})"


@pytest.mark.parametrize("method,path,kwargs", AUTH_ANY)
def test_auth_endpoints_block_anon(client, method, path, kwargs):
    """Endpoints autenticados sin sesión → 401."""
    res = client.request(method, path, **kwargs)
    assert res.status_code == 401, f"{method} {path} debería ser 401 sin auth"


@pytest.mark.parametrize("method,path,kwargs", AUTH_ANY)
def test_auth_endpoints_allow_viewer(viewer_client, method, path, kwargs):
    """Endpoints autenticados con sesión viewer → 200."""
    res = viewer_client.request(method, path, **kwargs)
    assert res.status_code == 200, f"{method} {path} debería ser 200 para viewer (got {res.status_code})"


def test_viewer_cannot_create_user(client, viewer):
    """viewer NO puede crear usuarios."""
    client.post("/api/auth/login", json=viewer)
    res = client.post("/api/users", json=_VALID_USER_PAYLOAD)
    assert res.status_code == 403


def test_admin_can_create_user(admin_client):
    """superadmin SÍ puede crear usuarios."""
    res = admin_client.post("/api/users", json={
        "username": "nuevo_user",
        "password": "newpass123",
        "role": "viewer",
        "display_name": "Nuevo",
    })
    assert res.status_code in (200, 201), f"creación falló: {res.text}"


def test_invalid_token_rejected(client):
    """Cookie token inválido → 401."""
    client.cookies.set("fide_token", "token-falsificado-no-existe")
    res = client.get("/api/auth/me")
    assert res.status_code == 401


def test_expired_token_rejected(client, db):
    """Sesión expirada → 401."""
    # Insertar sesión expirada manualmente
    db.execute("INSERT INTO users (username, password_hash, role, is_active) "
               "VALUES ('exp', 'x', 'viewer', 1)")
    user_id = db.execute("SELECT id FROM users WHERE username='exp'").fetchone()["id"]
    db.execute(
        "INSERT INTO sessions (token, user_id, expires_at) "
        "VALUES ('expired-token', ?, datetime('now', '-1 hour'))",
        (user_id,)
    )
    db.commit()
    client.cookies.set("fide_token", "expired-token")
    res = client.get("/api/auth/me")
    assert res.status_code == 401
