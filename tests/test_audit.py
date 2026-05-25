"""Tests del audit log: insert normal y fallback a archivo."""
import os
import pytest
from unittest.mock import patch
from app.audit import log_audit, CRITICAL_ACTIONS, _fallback_log_path


def test_log_audit_inserts(db):
    """log_audit insertar fila en audit_logs."""
    log_audit(123, "alguien", "test_action", "details aquí", "1.2.3.4")
    row = db.execute(
        "SELECT * FROM audit_logs WHERE action = 'test_action'"
    ).fetchone()
    assert row is not None
    assert row["user_id"] == 123
    assert row["username"] == "alguien"
    assert row["details"] == "details aquí"
    assert row["ip"] == "1.2.3.4"


def test_log_audit_truncates_details(db):
    """details > 1000 chars se trunca."""
    huge = "X" * 5000
    log_audit(1, "u", "trunc_action", huge, "ip")
    row = db.execute(
        "SELECT details FROM audit_logs WHERE action = 'trunc_action'"
    ).fetchone()
    assert len(row["details"]) <= 1000


def test_log_audit_does_not_raise_on_db_error(db):
    """Si la BD falla, log_audit NUNCA debe levantar (audit no bloquea acciones)."""
    with patch("app.audit.get_db") as mock_db:
        mock_db.side_effect = Exception("simulated DB error")
        # No debe levantar
        log_audit(1, "u", "non_critical_action", "x", "ip")


def test_log_audit_writes_fallback_on_critical(db, tmp_path, monkeypatch):
    """Para acciones críticas, si la BD falla → escribir a archivo fallback."""
    # Apuntar fallback a tmp_path
    monkeypatch.setattr("app.audit._fallback_log_path", lambda: str(tmp_path / "audit.log"))
    with patch("app.audit.get_db") as mock_db:
        mock_db.side_effect = Exception("DB down")
        # csv_export está en CRITICAL_ACTIONS
        log_audit(1, "u", "csv_export", "ids=1,2,3", "1.1.1.1")
    log_file = tmp_path / "audit.log"
    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "csv_export" in content
    assert "ids=1,2,3" in content
    assert "DB down" in content or "Exception" in content


def test_log_audit_no_fallback_on_noncritical(db, tmp_path, monkeypatch):
    """Para acciones NO críticas, si la BD falla → NO escribir fallback."""
    monkeypatch.setattr("app.audit._fallback_log_path", lambda: str(tmp_path / "audit.log"))
    with patch("app.audit.get_db") as mock_db:
        mock_db.side_effect = Exception("DB down")
        log_audit(1, "u", "some_random_action", "x", "1.1.1.1")
    log_file = tmp_path / "audit.log"
    assert not log_file.exists()


def test_critical_actions_includes_uploads():
    """Los uploads sensibles están en CRITICAL_ACTIONS (no perder rastro)."""
    expected = {
        "excel_upload", "recaudo_legacy_upload", "solicitudes_legacy_upload",
        "juridico_upload", "financieros_upload",
    }
    assert expected.issubset(CRITICAL_ACTIONS)


def test_critical_actions_includes_reveal_and_export():
    """Reveal/export PII están en CRITICAL_ACTIONS."""
    assert "credit_reveal" in CRITICAL_ACTIONS
    assert "csv_export" in CRITICAL_ACTIONS


def test_critical_actions_includes_user_mgmt():
    """Gestión de usuarios está en CRITICAL_ACTIONS."""
    assert "user_create" in CRITICAL_ACTIONS
    assert "user_deactivate" in CRITICAL_ACTIONS


def test_fallback_log_path_in_db_dir():
    """El archivo fallback vive junto a la BD (volumen persistente)."""
    from app.config import DATABASE_PATH
    p = _fallback_log_path()
    assert "audit_fallback.log" in p
    # mismo directorio que la BD
    assert os.path.dirname(p) == (os.path.dirname(DATABASE_PATH) or ".")
