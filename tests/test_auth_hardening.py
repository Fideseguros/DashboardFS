"""Hardening de autenticación (oct-2026).

Cubre: tokens de sesión hasheados en BD, expiración deslizante por inactividad,
política de contraseñas, flujo 2FA (TOTP) completo y reset de 2FA por superadmin.
"""
import json
import pyotp
import pytest

from app.auth.middleware import hash_token


# ------------------------------------------------------------ helpers ---

def _sessions(db):
    return db.execute("SELECT * FROM sessions").fetchall()


def _audit_actions(db, action):
    return db.execute("SELECT * FROM audit_logs WHERE action = ?", (action,)).fetchall()


def _enable_2fa(client):
    """setup + confirm sobre el cliente ya autenticado. Devuelve (secret, recovery_codes)."""
    res = client.post("/api/auth/2fa/setup")
    assert res.status_code == 200, res.text
    secret = res.json()["secret"]
    assert "otpauth_url" in res.json() and res.json()["otpauth_url"].startswith("otpauth://totp/")
    code = pyotp.TOTP(secret).now()
    res = client.post("/api/auth/2fa/confirm", json={"code": code})
    assert res.status_code == 200, res.text
    codes = res.json()["recovery_codes"]
    assert len(codes) == 8 and all(len(c) == 10 for c in codes)
    return secret, codes


# ------------------------------------------------- 1. token hasheado ---

def test_session_token_stored_hashed(admin_client, db):
    token = admin_client.cookies.get("fide_token")
    rows = _sessions(db)
    assert len(rows) == 1
    stored = rows[0]["token"]
    assert stored != token, "el token en claro no debe estar en la BD"
    assert stored == hash_token(token)
    assert len(stored) == 64  # sha256 hex
    assert rows[0]["last_seen_at"] is not None


def test_hash_token_is_deterministic_sha256():
    import hashlib
    assert hash_token("abc") == hashlib.sha256(b"abc").hexdigest()
    assert hash_token("abc") == hash_token("abc")
    assert hash_token("abc") != hash_token("abd")


def test_logout_uses_hashed_token(admin_client, db):
    assert len(_sessions(db)) == 1
    res = admin_client.post("/api/auth/logout")
    assert res.status_code == 200
    assert len(_sessions(db)) == 0


def test_plaintext_token_in_db_does_not_authenticate(client, superadmin, db):
    """Si alguien inserta un token en claro en sessions, la cookie con ese valor no sirve."""
    uid = db.execute("SELECT id FROM users WHERE username = ?", (superadmin["username"],)).fetchone()["id"]
    db.execute(
        "INSERT INTO sessions (token, user_id, expires_at, last_seen_at) "
        "VALUES ('token-en-claro', ?, datetime('now', '+1 hour'), datetime('now'))", (uid,))
    db.commit()
    client.cookies.set("fide_token", "token-en-claro")
    assert client.get("/api/auth/me").status_code == 401


# ------------------------------------------------- 2. idle expiry ---

def test_idle_session_rejected(admin_client, db):
    assert admin_client.get("/api/auth/me").status_code == 200
    db.execute("UPDATE sessions SET last_seen_at = datetime('now', '-90 minutes')")
    db.commit()
    res = admin_client.get("/api/auth/me")
    assert res.status_code == 401


def test_idle_session_within_limit_ok_and_touches_last_seen(admin_client, db):
    db.execute("UPDATE sessions SET last_seen_at = datetime('now', '-30 minutes')")
    db.commit()
    before = _sessions(db)[0]["last_seen_at"]
    assert admin_client.get("/api/auth/me").status_code == 200
    after = _sessions(db)[0]["last_seen_at"]
    assert after > before, "last_seen_at debe refrescarse si el último toque tiene más de 60 s"


def test_last_seen_not_touched_on_every_request(admin_client, db):
    """Dos requests seguidos: el segundo NO reescribe last_seen_at (throttle de 60 s)."""
    assert admin_client.get("/api/auth/me").status_code == 200
    first = _sessions(db)[0]["last_seen_at"]
    assert admin_client.get("/api/auth/me").status_code == 200
    second = _sessions(db)[0]["last_seen_at"]
    assert first == second


def test_dashboard_redirects_when_idle(admin_client, db):
    db.execute("UPDATE sessions SET last_seen_at = datetime('now', '-90 minutes')")
    db.commit()
    res = admin_client.get("/", follow_redirects=False)
    assert res.status_code == 302
    assert "/login" in res.headers["location"]


def test_dashboard_ok_with_valid_session(admin_client):
    res = admin_client.get("/", follow_redirects=False)
    assert res.status_code == 200


# ------------------------------------------- 3. política de contraseñas ---

def _create(client, pwd, username="nuevo_usuario"):
    return client.post("/api/users", json={
        "username": username, "password": pwd, "display_name": "Nuevo", "role": "viewer"})


def test_password_too_short_rejected(admin_client):
    res = _create(admin_client, "corta-2026")
    assert res.status_code == 400
    assert "12 caracteres" in res.json()["detail"]


def test_password_common_rejected(admin_client):
    for pwd in ("password1234", "123456789012", "fideseguros2026", "FidesSeguros2026"):
        res = _create(admin_client, pwd)
        assert res.status_code == 400, pwd
        assert "común" in res.json()["detail"]


def test_password_containing_username_rejected(admin_client):
    res = _create(admin_client, "xx-Nuevo_Usuario-2026", username="nuevo_usuario")
    assert res.status_code == 400
    assert "nombre de usuario" in res.json()["detail"]


def test_password_strong_accepted(admin_client):
    res = _create(admin_client, "Cartera.Segura#2026")
    assert res.status_code == 200, res.text


def test_password_policy_applies_on_update(admin_client, viewer, db):
    uid = db.execute("SELECT id FROM users WHERE username = ?", (viewer["username"],)).fetchone()["id"]
    res = admin_client.patch(f"/api/users/{uid}", json={"password": "corta"})
    assert res.status_code == 400
    res = admin_client.patch(f"/api/users/{uid}", json={"password": f"abc-{viewer['username']}-xyz"})
    assert res.status_code == 400
    res = admin_client.patch(f"/api/users/{uid}", json={"password": "Otra.Clave.Larga#2026"})
    assert res.status_code == 200


# ------------------------------------------------------ 4. flujo 2FA ---

def test_me_reports_totp_disabled_by_default(admin_client):
    data = admin_client.get("/api/auth/me").json()
    assert data["totp_enabled"] is False


def test_2fa_endpoints_require_auth(client):
    assert client.post("/api/auth/2fa/setup").status_code == 401
    assert client.post("/api/auth/2fa/confirm", json={"code": "000000"}).status_code == 401
    assert client.post("/api/auth/2fa/disable", json={"password": "x", "code": "000000"}).status_code == 401


def test_2fa_setup_stores_encrypted_secret_not_enabled(admin_client, db, superadmin):
    res = admin_client.post("/api/auth/2fa/setup")
    assert res.status_code == 200
    secret = res.json()["secret"]
    row = db.execute("SELECT * FROM users WHERE username = ?", (superadmin["username"],)).fetchone()
    assert row["totp_enabled"] == 0
    assert row["totp_secret"] and row["totp_secret"] != secret, "el secreto debe guardarse cifrado"
    from app.crypto import decrypt
    assert decrypt(row["totp_secret"]) == secret
    # Sin confirmar, el login sigue siendo de un solo paso
    assert admin_client.get("/api/auth/me").json()["totp_enabled"] is False


def test_2fa_confirm_wrong_code_rejected(admin_client):
    admin_client.post("/api/auth/2fa/setup")
    res = admin_client.post("/api/auth/2fa/confirm", json={"code": "000000"})
    assert res.status_code == 400


def test_2fa_confirm_without_setup_rejected(admin_client):
    res = admin_client.post("/api/auth/2fa/confirm", json={"code": "123456"})
    assert res.status_code == 400


def test_2fa_full_flow(client, superadmin, db):
    # --- activar ---
    client.post("/api/auth/login", json=superadmin)
    secret, codes = _enable_2fa(client)
    row = db.execute("SELECT * FROM users WHERE username = ?", (superadmin["username"],)).fetchone()
    assert row["totp_enabled"] == 1
    stored = json.loads(row["totp_recovery"])
    assert len(stored) == 8
    assert not any(c in stored for c in codes), "solo se guardan hashes de los códigos"
    assert len(_audit_actions(db, "2fa_enabled")) == 1
    assert client.get("/api/auth/me").json()["totp_enabled"] is True

    # --- login en dos pasos ---
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401
    res = client.post("/api/auth/login", json=superadmin)
    assert res.status_code == 200
    body = res.json()
    assert body["requires_totp"] is True
    pending = body["pending_token"]
    assert pending and "expires" not in body
    assert client.cookies.get("fide_token") in (None, ""), "sin código no hay cookie de sesión"
    assert len(_sessions(db)) == 0
    pend_rows = db.execute("SELECT * FROM login_pending").fetchall()
    assert len(pend_rows) == 1 and pend_rows[0]["token_hash"] == hash_token(pending)

    # código malo → 401 y el pending sigue vivo
    res = client.post("/api/auth/login/2fa", json={"pending_token": pending, "code": "000000"})
    assert res.status_code == 401
    assert db.execute("SELECT failed FROM login_pending").fetchone()["failed"] == 1

    # código bueno → sesión + cookie
    res = client.post("/api/auth/login/2fa",
                      json={"pending_token": pending, "code": pyotp.TOTP(secret).now()})
    assert res.status_code == 200, res.text
    assert "expires" in res.json() and res.json()["role"] == "superadmin"
    assert client.cookies.get("fide_token")
    assert client.get("/api/auth/me").status_code == 200
    assert len(_sessions(db)) == 1
    assert db.execute("SELECT COUNT(*) c FROM login_pending").fetchone()["c"] == 0

    # el pending ya no se puede reutilizar
    res = client.post("/api/auth/login/2fa",
                      json={"pending_token": pending, "code": pyotp.TOTP(secret).now()})
    assert res.status_code == 401

    # --- código de recuperación: se consume ---
    client.post("/api/auth/logout")
    pending = client.post("/api/auth/login", json=superadmin).json()["pending_token"]
    res = client.post("/api/auth/login/2fa", json={"pending_token": pending, "code": codes[0]})
    assert res.status_code == 200, res.text
    remaining = json.loads(db.execute(
        "SELECT totp_recovery FROM users WHERE username = ?", (superadmin["username"],)
    ).fetchone()["totp_recovery"])
    assert len(remaining) == 7
    client.post("/api/auth/logout")
    pending = client.post("/api/auth/login", json=superadmin).json()["pending_token"]
    res = client.post("/api/auth/login/2fa", json={"pending_token": pending, "code": codes[0]})
    assert res.status_code == 401, "un código de recuperación usado no vale dos veces"
    # entramos con otro código válido para seguir
    res = client.post("/api/auth/login/2fa", json={"pending_token": pending, "code": codes[1]})
    assert res.status_code == 200

    # --- disable ---
    res = client.post("/api/auth/2fa/disable",
                      json={"password": "mala", "code": pyotp.TOTP(secret).now()})
    assert res.status_code == 401
    res = client.post("/api/auth/2fa/disable",
                      json={"password": superadmin["password"], "code": "000000"})
    assert res.status_code == 400
    res = client.post("/api/auth/2fa/disable",
                      json={"password": superadmin["password"], "code": pyotp.TOTP(secret).now()})
    assert res.status_code == 200, res.text
    row = db.execute("SELECT * FROM users WHERE username = ?", (superadmin["username"],)).fetchone()
    assert row["totp_enabled"] == 0 and row["totp_secret"] is None and row["totp_recovery"] is None
    assert len(_audit_actions(db, "2fa_disabled")) == 1

    # vuelve a ser login de un paso
    client.post("/api/auth/logout")
    res = client.post("/api/auth/login", json=superadmin)
    assert res.status_code == 200 and "expires" in res.json()


def test_2fa_pending_invalidated_after_5_failures(client, superadmin, db):
    client.post("/api/auth/login", json=superadmin)
    _enable_2fa(client)
    client.post("/api/auth/logout")
    pending = client.post("/api/auth/login", json=superadmin).json()["pending_token"]
    for _ in range(5):
        res = client.post("/api/auth/login/2fa", json={"pending_token": pending, "code": "000000"})
        assert res.status_code == 401
    assert db.execute("SELECT COUNT(*) c FROM login_pending").fetchone()["c"] == 0
    assert len(_audit_actions(db, "2fa_failed")) == 1
    # el pending ya no vale ni con código bueno
    res = client.post("/api/auth/login/2fa", json={"pending_token": pending, "code": "000000"})
    assert res.status_code == 401


def test_2fa_pending_expired_rejected(client, superadmin, db):
    client.post("/api/auth/login", json=superadmin)
    secret, _ = _enable_2fa(client)
    client.post("/api/auth/logout")
    pending = client.post("/api/auth/login", json=superadmin).json()["pending_token"]
    db.execute("UPDATE login_pending SET expires_at = datetime('now', '-1 minute')")
    db.commit()
    res = client.post("/api/auth/login/2fa",
                      json={"pending_token": pending, "code": pyotp.TOTP(secret).now()})
    assert res.status_code == 401
    # y se limpia
    assert db.execute("SELECT COUNT(*) c FROM login_pending").fetchone()["c"] == 0


def test_2fa_unknown_pending_token_rejected(client):
    res = client.post("/api/auth/login/2fa", json={"pending_token": "no-existe", "code": "123456"})
    assert res.status_code == 401


def test_2fa_setup_blocked_when_already_enabled(client, superadmin):
    client.post("/api/auth/login", json=superadmin)
    _enable_2fa(client)
    assert client.post("/api/auth/2fa/setup").status_code == 400


# ------------------------------------------------ 5. reset por admin ---

def test_admin_totp_reset(client, superadmin, viewer, db):
    # el viewer activa su 2FA
    client.post("/api/auth/login", json=viewer)
    _enable_2fa(client)
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json=viewer).json().get("requires_totp") is True

    # el superadmin lo apaga
    client.post("/api/auth/login", json=superadmin)
    uid = db.execute("SELECT id FROM users WHERE username = ?", (viewer["username"],)).fetchone()["id"]
    res = client.patch(f"/api/users/{uid}", json={"totp_reset": True})
    assert res.status_code == 200, res.text
    row = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    assert row["totp_enabled"] == 0 and row["totp_secret"] is None and row["totp_recovery"] is None
    assert len(_audit_actions(db, "2fa_reset_by_admin")) == 1
    assert db.execute("SELECT COUNT(*) c FROM login_pending WHERE user_id = ?", (uid,)).fetchone()["c"] == 0

    # el viewer vuelve a entrar en un solo paso
    client.post("/api/auth/logout")
    res = client.post("/api/auth/login", json=viewer)
    assert res.status_code == 200 and "expires" in res.json()


def test_list_users_includes_totp_enabled(admin_client):
    rows = admin_client.get("/api/users").json()
    assert all("totp_enabled" in r for r in rows)
    assert all(r["totp_enabled"] is False for r in rows)


def test_viewer_cannot_totp_reset(client, viewer, superadmin, db):
    client.post("/api/auth/login", json=viewer)
    uid = db.execute("SELECT id FROM users WHERE username = ?", (superadmin["username"],)).fetchone()["id"]
    assert client.patch(f"/api/users/{uid}", json={"totp_reset": True}).status_code == 403
