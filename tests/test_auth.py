"""Tests de autenticación: login, logout, rate limiting, dummy bcrypt anti-timing."""
import time
import pytest


def test_login_ok_superadmin(client, superadmin):
    res = client.post("/api/auth/login", json=superadmin)
    assert res.status_code == 200
    data = res.json()
    assert data["role"] == "superadmin"
    assert "expires" in data
    # cookie httponly secure samesite=strict
    cookie_header = res.headers.get("set-cookie", "")
    assert "HttpOnly" in cookie_header
    assert "SameSite=strict" in cookie_header.lower() or "samesite=strict" in cookie_header.lower()


def test_login_wrong_password(client, superadmin):
    res = client.post("/api/auth/login", json={
        "username": superadmin["username"],
        "password": "wrong-password"
    })
    assert res.status_code == 401


def test_login_unknown_user(client):
    res = client.post("/api/auth/login", json={
        "username": "no-existe",
        "password": "lo-que-sea"
    })
    assert res.status_code == 401


def test_login_inactive_user(client, db):
    """Usuario inactivo no debe poder loguearse."""
    import bcrypt as _bcrypt
    pwd_hash = _bcrypt.hashpw(b"x", _bcrypt.gensalt(rounds=4)).decode()
    db.execute(
        "INSERT INTO users (username, password_hash, role, is_active) "
        "VALUES ('inactivo', ?, 'viewer', 0)", (pwd_hash,)
    )
    db.commit()
    res = client.post("/api/auth/login", json={"username": "inactivo", "password": "x"})
    assert res.status_code == 401


def test_dummy_bcrypt_no_timing_disclosure(client, superadmin):
    """El tiempo de respuesta para usuario inexistente debe ser similar al de password incorrecto.

    Esto valida que el dummy bcrypt anti-timing funciona. Tolerancia amplia
    porque CI es ruidoso; lo importante es que el usuario inexistente NO
    responda instantáneamente.
    """
    # Calentar bcrypt
    client.post("/api/auth/login", json={"username": "a", "password": "b"})

    t_unknown = []
    t_wrong = []
    for _ in range(3):
        t0 = time.perf_counter()
        client.post("/api/auth/login", json={"username": f"no-existe-{_}", "password": "x"})
        t_unknown.append(time.perf_counter() - t0)

        t0 = time.perf_counter()
        client.post("/api/auth/login", json={
            "username": superadmin["username"], "password": "wrong"
        })
        t_wrong.append(time.perf_counter() - t0)

    avg_unknown = sum(t_unknown) / len(t_unknown)
    avg_wrong = sum(t_wrong) / len(t_wrong)
    # El tiempo para usuario inexistente NO debe ser <50% del de password incorrecto
    # (esto indicaría que bcrypt no se ejecuta para usuarios desconocidos)
    assert avg_unknown >= avg_wrong * 0.4, (
        f"timing leak: avg_unknown={avg_unknown*1000:.1f}ms "
        f"vs avg_wrong={avg_wrong*1000:.1f}ms"
    )


def test_rate_limit_ip(client, db):
    """Después de LOGIN_MAX_ATTEMPTS intentos fallidos, el IP queda bloqueado."""
    # Simular 5 intentos fallidos
    for i in range(5):
        client.post("/api/auth/login", json={
            "username": f"fake-{i}", "password": "wrong"
        })
    # El 6to debe ser 429
    res = client.post("/api/auth/login", json={
        "username": "fake-6", "password": "wrong"
    })
    assert res.status_code == 429
    assert "intentos" in res.json()["detail"].lower()


def test_login_creates_audit_log(client, superadmin, db):
    """Login exitoso debe registrar audit_log."""
    client.post("/api/auth/login", json=superadmin)
    rows = db.execute(
        "SELECT action FROM audit_logs WHERE username = ? ORDER BY id DESC",
        (superadmin["username"],)
    ).fetchall()
    actions = [r["action"] for r in rows]
    assert "login_success" in actions


def test_login_failed_creates_audit_log(client, superadmin, db):
    """Login fallido debe registrar audit_log con login_failed."""
    client.post("/api/auth/login", json={
        "username": superadmin["username"], "password": "wrong"
    })
    rows = db.execute(
        "SELECT action FROM audit_logs WHERE username = ?",
        (superadmin["username"],)
    ).fetchall()
    actions = [r["action"] for r in rows]
    assert "login_failed" in actions


def test_logout_deletes_session(admin_client, db):
    """Logout debe borrar la sesión de la BD."""
    sessions_before = db.execute("SELECT COUNT(*) as c FROM sessions").fetchone()["c"]
    assert sessions_before >= 1
    res = admin_client.post("/api/auth/logout")
    assert res.status_code == 200
    sessions_after = db.execute("SELECT COUNT(*) as c FROM sessions").fetchone()["c"]
    assert sessions_after < sessions_before


def test_me_requires_auth(client):
    """/api/auth/me sin sesión debe devolver 401."""
    res = client.get("/api/auth/me")
    assert res.status_code == 401


def test_me_returns_role(admin_client):
    res = admin_client.get("/api/auth/me")
    assert res.status_code == 200
    assert res.json()["role"] == "superadmin"
