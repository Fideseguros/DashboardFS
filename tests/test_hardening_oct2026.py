"""Regresiones de la auditoría de seguridad de octubre 2026."""


def test_failed_login_is_persisted(client, db, superadmin):
    """El intento fallido debe quedar guardado aunque la respuesta sea 401.

    Antes, el raise HTTPException ocurría dentro de `with get_db()` y el
    rollback borraba la fila: el límite de intentos nunca se alcanzaba.
    """
    res = client.post("/api/auth/login",
                      json={"username": superadmin["username"], "password": "mala"})
    assert res.status_code == 401
    n = db.execute("SELECT COUNT(*) c FROM login_attempts WHERE success = 0").fetchone()["c"]
    assert n == 1
    a = db.execute("SELECT COUNT(*) c FROM audit_logs WHERE action = 'login_failed'").fetchone()["c"]
    assert a == 1


def test_rate_limit_is_audited(client, db):
    for i in range(5):
        client.post("/api/auth/login", json={"username": f"x{i}", "password": "w"})
    res = client.post("/api/auth/login", json={"username": "x9", "password": "w"})
    assert res.status_code == 429
    a = db.execute(
        "SELECT COUNT(*) c FROM audit_logs WHERE action = 'login_blocked_rate_limit'"
    ).fetchone()["c"]
    assert a == 1


def test_openapi_docs_are_disabled(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/redoc").status_code == 404


def test_csv_export_is_audited(viewer_client, db):
    res = viewer_client.post("/api/audit/export",
                             json={"archivo": "cartera_detalle", "filas": 42,
                                   "columnas": ["cliente", "identificacion"]})
    assert res.status_code == 200
    row = db.execute(
        "SELECT details, username FROM audit_logs WHERE action = 'csv_export'"
    ).fetchone()
    assert row is not None
    assert "archivo=cartera_detalle" in row["details"]
    assert "filas=42" in row["details"]
    assert row["username"] == "viewer_test"


def test_csv_export_requires_auth(client):
    res = client.post("/api/audit/export", json={"archivo": "x", "filas": 1})
    assert res.status_code == 401


def test_diagnose_requires_superadmin(viewer_client):
    assert viewer_client.get("/api/solicitudes/diagnose").status_code == 403


def test_client_ip_uses_last_forwarded_hop():
    from app.audit import get_client_ip

    class _Req:
        headers = {"x-forwarded-for": "6.6.6.6, 10.0.0.1, 190.1.2.3"}
        client = None

    assert get_client_ip(_Req()) == "190.1.2.3"
