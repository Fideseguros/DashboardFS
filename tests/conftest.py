"""Pytest fixtures for fide-seguro tests.

Cada test corre con:
  - una BD SQLite temporal (DATABASE_PATH override)
  - FIELD_ENCRYPTION_KEY fijada con una clave de prueba
  - APP_ENV=test para que crypto.py no falle si la clave es la de dev
"""
import os
import tempfile
import pytest


# El entorno se fija AQUÍ, al importar conftest (pytest lo importa antes que
# cualquier módulo de tests). Antes vivía en un fixture de sesión, pero
# tests/test_audit.py hace `from app.audit import ...` en la cabecera, lo que
# importaba app.database durante la COLECCIÓN con DATABASE_PATH aún sin
# fijar: la suite completa terminaba apuntando a data/fide.db (la BD real del
# repo) y los tests de sesión fallaban con 401 al correr todos juntos.
_TMPDIR = tempfile.mkdtemp(prefix="fide-test-")
_DB_PATH = os.path.join(_TMPDIR, "test.db")
os.environ["DATABASE_PATH"] = _DB_PATH
os.environ["FIELD_ENCRYPTION_KEY"] = "test-encryption-key-for-pytest-only"
os.environ["APP_ENV"] = "test"
os.environ["COOKIE_SECURE"] = "0"
os.environ["LOGIN_MAX_ATTEMPTS"] = "5"
os.environ["LOGIN_LOCKOUT_MINUTES"] = "15"


@pytest.fixture(scope="session", autouse=True)
def _isolate_env():
    yield
    for suffix in ("", "-wal", "-shm"):
        try:
            if os.path.exists(_DB_PATH + suffix):
                os.unlink(_DB_PATH + suffix)
        except Exception:
            pass


@pytest.fixture
def db():
    """BD fresca para cada test. Recrea el schema."""
    from app.database import init_db, get_connection, DATABASE_PATH
    assert DATABASE_PATH == _DB_PATH, "app.database no está usando la BD temporal de pruebas"
    # Borrar la BD si existe (también WAL/SHM para no arrastrar estado)
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(DATABASE_PATH + suffix):
            os.unlink(DATABASE_PATH + suffix)
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
