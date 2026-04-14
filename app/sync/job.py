"""Sync job: pulls data from ACANO API or Excel into the database."""
import asyncio
import time
from datetime import datetime
from app.database import get_db, CREDIT_FIELDS


def _insert_credits(conn, records: list[dict], sync_id: int):
    """Insert a batch of credit records linked to a sync log."""
    placeholders = ", ".join(["?"] * (len(CREDIT_FIELDS) + 1))
    cols = ", ".join(CREDIT_FIELDS + ["sync_batch_id"])
    for record in records:
        values = [record.get(k) for k in CREDIT_FIELDS] + [sync_id]
        conn.execute(f"INSERT INTO credits ({cols}) VALUES ({placeholders})", values)


def _cleanup_old_batches(conn, current_sync_id: int):
    """Keep only the last 7 days of sync data for rollback capability."""
    conn.execute("""
        DELETE FROM credits WHERE sync_batch_id IN (
            SELECT id FROM sync_logs
            WHERE id != ? AND started_at < datetime('now', '-7 days')
        )
    """, (current_sync_id,))
    conn.execute("DELETE FROM sync_logs WHERE started_at < datetime('now', '-30 days') AND status != 'running'")


async def sync_from_api():
    """Pull data from ACANO API and insert into database."""
    from app.acano.client import AcanoClient
    from app.acano.transformer import transform_api_batch

    with get_db() as conn:
        conn.execute(
            "INSERT INTO sync_logs (started_at, status, source) VALUES (?, 'running', 'acano_api')",
            (datetime.utcnow().isoformat(),)
        )
        sync_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    start = time.time()
    try:
        client = AcanoClient()
        raw_records = await client.fetch_products()
        records = transform_api_batch(raw_records)

        with get_db() as conn:
            _insert_credits(conn, records, sync_id)
            _cleanup_old_batches(conn, sync_id)

            duration = time.time() - start
            conn.execute("""
                UPDATE sync_logs SET status='success', completed_at=?,
                records_fetched=?, records_inserted=?, duration_seconds=?
                WHERE id=?
            """, (datetime.utcnow().isoformat(), len(raw_records), len(records), duration, sync_id))

        return {"status": "success", "records": len(records), "duration": round(duration, 2)}

    except Exception as e:
        duration = time.time() - start
        with get_db() as conn:
            conn.execute("""
                UPDATE sync_logs SET status='failed', completed_at=?,
                error_message=?, duration_seconds=? WHERE id=?
            """, (datetime.utcnow().isoformat(), str(e)[:500], duration, sync_id))
        raise


def sync_from_excel(file_bytes: bytes):
    """Import data from an uploaded Excel file (fallback/transition)."""
    import openpyxl
    import io
    from app.acano.transformer import transform_excel_batch

    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(min_row=2, values_only=True))

    records = transform_excel_batch(rows)

    with get_db() as conn:
        conn.execute(
            "INSERT INTO sync_logs (started_at, status, source) VALUES (?, 'running', 'manual_upload')",
            (datetime.utcnow().isoformat(),)
        )
        sync_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        start = time.time()
        _insert_credits(conn, records, sync_id)
        _cleanup_old_batches(conn, sync_id)

        duration = time.time() - start
        conn.execute("""
            UPDATE sync_logs SET status='success', completed_at=?,
            records_fetched=?, records_inserted=?, duration_seconds=?
            WHERE id=?
        """, (datetime.utcnow().isoformat(), len(rows), len(records), duration, sync_id))

    return {"status": "success", "records": len(records), "duration": round(duration, 2)}


if __name__ == "__main__":
    result = asyncio.run(sync_from_api())
    print(f"Sync completed: {result}")
