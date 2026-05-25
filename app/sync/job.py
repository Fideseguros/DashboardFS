"""Sync job: imports data from an uploaded Excel file (processed in-memory)."""
import time
from datetime import datetime
from app.database import get_db, CREDIT_FIELDS

MAX_ROWS = 50_000


def _insert_credits(conn, records: list[dict], sync_id: int):
    placeholders = ", ".join(["?"] * (len(CREDIT_FIELDS) + 1))
    cols = ", ".join(CREDIT_FIELDS + ["sync_batch_id"])
    for record in records:
        values = [record.get(k) for k in CREDIT_FIELDS] + [sync_id]
        conn.execute(f"INSERT INTO credits ({cols}) VALUES ({placeholders})", values)


def _cleanup_old_batches(conn, current_sync_id: int):
    """Mantiene los últimos 7 días de datos de cartera (manual_upload) y 30
    días de sync_logs. Filtra estrictamente por source='manual_upload' para
    evitar eliminar datos de otros módulos (recaudo, solicitudes, etc.)."""
    conn.execute("""
        DELETE FROM credits WHERE sync_batch_id IN (
            SELECT id FROM sync_logs
            WHERE id != ?
              AND source = 'manual_upload'
              AND started_at < datetime('now', '-7 days')
        )
    """, (current_sync_id,))
    conn.execute(
        "DELETE FROM sync_logs WHERE source = 'manual_upload' "
        "AND started_at < datetime('now', '-30 days') AND status != 'running'"
    )


def sync_from_excel(file_bytes: bytes, uploaded_by: int | None = None) -> dict:
    """Import cartera data from an in-memory Excel file. Raises on failure."""
    import openpyxl
    import io
    from app.acano.transformer import transform_excel_batch

    # Create the sync_log first so failures are recorded.
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sync_logs (started_at, status, source, uploaded_by) "
            "VALUES (?, 'running', 'manual_upload', ?)",
            (datetime.utcnow().isoformat(), uploaded_by)
        )
        sync_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    start = time.time()
    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
        sheet_name = "Cartera_Consolidado" if "Cartera_Consolidado" in wb.sheetnames else wb.sheetnames[0]
        ws = wb[sheet_name]

        rows = []
        for i, row in enumerate(ws.iter_rows(min_row=2, values_only=True)):
            if i >= MAX_ROWS:
                raise ValueError(f"Archivo excede el límite de {MAX_ROWS} filas")
            rows.append(row)
        wb.close()

        records = transform_excel_batch(rows)

        # Skip rows with no identificacion/cliente/estado — usually empty trailing rows.
        records = [r for r in records
                   if r.get("identificacion") and r.get("cliente") and r.get("estado")]

        with get_db() as conn:
            _insert_credits(conn, records, sync_id)
            _cleanup_old_batches(conn, sync_id)
            duration = time.time() - start
            conn.execute("""
                UPDATE sync_logs SET status='success', completed_at=?,
                records_fetched=?, records_inserted=?, duration_seconds=?
                WHERE id=?
            """, (datetime.utcnow().isoformat(), len(rows), len(records), duration, sync_id))

        return {
            "status": "success",
            "records": len(records),
            "sheet": sheet_name,
            "duration": round(duration, 2)
        }

    except Exception as e:
        duration = time.time() - start
        try:
            with get_db() as conn:
                conn.execute("""
                    UPDATE sync_logs SET status='failed', completed_at=?,
                    error_message=?, duration_seconds=? WHERE id=?
                """, (datetime.utcnow().isoformat(), f"{type(e).__name__}: {str(e)[:400]}",
                      duration, sync_id))
        except Exception:
            pass
        raise
