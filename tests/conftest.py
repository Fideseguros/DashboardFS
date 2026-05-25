"""Pytest fixtures for fide-seguro tests.

Cada test corre con:
  - una BD SQLite temporal (DATABASE_PATH override)
  - FIELD_ENCRYPTION_KEY fijada con una clave de prueba
  - APP_ENV=test para que crypto.py no falle si la clave es la de dev
"""
import os
import tempfile
import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolate_env():
    """Antes de importar nada de app.*, fijar el entorno de prueba."""
    tmpdir = tempfile.mkdtemp(prefix="fide-test-")
    db_path = os.path.join(tmpdir, "test.db")
    os.environ["DATABASE_PATH"] = db_path
    os.environ["FIELD_ENCRYPTION_KEY"] = "test-encryption-key-for-pytest-only"
    os.environ["APP_ENV"] = "test"
    os.environ["COOKIE_SECURE"] = "0"
    os.environ["LOGIN_MAX_ATTEMPTS"] = "5"
    os.environ["LOGIN_LOCKOUT_MINUTES"] = "15"
    yield
    # cleanup
    try:
        if os.path.exists(db_path):
            os.unlink(db_path)
    except Exception:
        pass


@pytest.fixture
def db():
    """BD fresca para cada test. Recrea el schema."""
    from app.database import init_db, get_connection, DATABASE_PATH
    # Borrar la BD si existe
    if os.path.exists(DATABASE_PATH):
        os.unlink(DATABASE_PATH)
    init_db()
    conn = get_connection()
    yield conn
    conn.close()


@pytest.fixture
def client(db):
    """TestClient FastAPI con BD limpia."""
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def superadmin(db):
    """Crea un superadmin y devuelve sus credenciales."""
    import bcrypt as _bcrypt
    pwd_hash = _bcrypt.hashpw(b"admin-pass-test", _bcrypt.gensalt(rounds=4)).decode()
    db.execute(
        "INSERT INTO users (username, password_hash, display_name, role, is_active) "
        "VALUES (?, ?, ?, 'superadmin', 1)",
        ("admin_test", pwd_hash, "Admin Test")
    )
    db.commit()
    return {"username": "admin_test", "password": "admin-pass-test"}


@pytest.fixture
def viewer(db):
    """Crea un viewer y devuelve sus credenciales."""
    import bcrypt as _bcrypt
    pwd_hash = _bcrypt.hashpw(b"viewer-pass-test", _bcrypt.gensalt(rounds=4)).decode()
    db.execute(
        "INSERT INTO users (username, password_hash, display_name, role, is_active) "
        "VALUES (?, ?, ?, 'viewer', 1)",
        ("viewer_test", pwd_hash, "Viewer Test")
    )
    db.commit()
    return {"username": "viewer_test", "password": "viewer-pass-test"}


@pytest.fixture
def consulta(db):
    """Crea un usuario de consulta y devuelve sus credenciales."""
    import bcrypt as _bcrypt
    pwd_hash = _bcrypt.hashpw(b"consulta-pass-test", _bcrypt.gensalt(rounds=4)).decode()
    db.execute(
        "INSERT INTO users (username, password_hash, display_name, role, is_active) "
        "VALUES (?, ?, ?, 'consulta', 1)",
        ("consulta_test", pwd_hash, "Consulta Test")
    )
    db.commit()
    return {"username": "consulta_test", "password": "consulta-pass-test"}


def _do_login(client, creds: dict) -> str:
    """Helper: login y devuelve el token de cookie."""
    res = client.post("/api/auth/login", json=creds)
    assert res.status_code == 200, f"login failed: {res.text}"
    return client.cookies.get("fide_token")


@pytest.fixture
def admin_client(client, superadmin):
    """TestClient ya autenticado como superadmin."""
    _do_login(client, superadmin)
    return client


@pytest.fixture
def viewer_client(client, viewer):
    """TestClient ya autenticado como viewer."""
    _do_login(client, viewer)
    return client
