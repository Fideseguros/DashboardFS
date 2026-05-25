"""Sync job: imports data from an uploaded Excel file (processed in-memory)."""
import time
from datetime import datetime
from app.database import get_db, CREDIT_FIELDS

MAX_ROWS = 50_000

# Campos que se actualizan desde el archivo de la plataforma NUEVA (cartera nueva)
# sobre la cartera BASE (cartera vieja). El resto de campos se preserva del base.
# Corresponde a las columnas naranja del 'Informe cartera plataforma vieja.xlsx'.
ORANGE_FIELDS = (
    'estado', 'saldo_capital', 'saldo_favor', 'valor_cuota', 'fecha_ult_pago',
    'calificacion', 'cuotas_pagadas', 'dias_mora', 'maxima_mora', 'aliado',
)


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


def incremental_update_from_excel(file_bytes: bytes, uploaded_by: int | None = None) -> dict:
    """Actualización incremental de cartera desde el archivo de la plataforma NUEVA.

    Política:
      - Para cada cuenta presente en el archivo nuevo:
          * Si ya existe en la base → UPDATE solo las columnas naranja (ORANGE_FIELDS).
          * Si no existe → INSERT como crédito nuevo.
      - Cuentas existentes que NO están en el archivo nuevo permanecen intactas.

    Se crea un nuevo sync_log y todos los créditos del batch anterior se
    migran a este nuevo batch (para que las queries del dashboard, que
    apuntan al último 'manual_upload' exitoso, sigan viendo todo).
    """
    import openpyxl
    import io
    from app.acano.transformer import transform_excel_batch

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

        new_records = transform_excel_batch(rows)
        # Necesitamos cuenta como clave de matching; descartamos filas vacías.
        new_records = [r for r in new_records if r.get("cuenta") and r.get("identificacion")]

        updated_count = 0
        inserted_count = 0

        with get_db() as conn:
            # Migrar todos los créditos del último batch exitoso anterior al nuevo batch,
            # para preservar el base + actualizaciones previas en el batch activo.
            prev_sync = conn.execute(
                "SELECT id FROM sync_logs WHERE source='manual_upload' AND status='success' "
                "AND id < ? ORDER BY id DESC LIMIT 1", (sync_id,)
            ).fetchone()
            if prev_sync:
                conn.execute(
                    "UPDATE credits SET sync_batch_id=? WHERE sync_batch_id=?",
                    (sync_id, prev_sync[0])
                )

            placeholders = ", ".join(["?"] * (len(CREDIT_FIELDS) + 1))
            cols_sql = ", ".join(CREDIT_FIELDS + ["sync_batch_id"])

            for rec in new_records:
                cuenta = rec.get('cuenta')
                if not cuenta:
                    continue
                existing = conn.execute(
                    "SELECT id FROM credits WHERE cuenta=? AND sync_batch_id=?",
                    (str(cuenta), sync_id)
                ).fetchone()
                if existing:
                    # UPDATE solo campos naranja con valor no-None del nuevo archivo
                    update_pairs = [(f, rec[f]) for f in ORANGE_FIELDS
                                    if f in rec and rec[f] is not None]
                    if update_pairs:
                        set_clause = ', '.join(f"{f}=?" for f, _ in update_pairs)
                        params = [v for _, v in update_pairs] + [existing[0]]
                        conn.execute(f"UPDATE credits SET {set_clause} WHERE id=?", params)
                        updated_count += 1
                else:
                    values = [rec.get(k) for k in CREDIT_FIELDS] + [sync_id]
                    conn.execute(f"INSERT INTO credits ({cols_sql}) VALUES ({placeholders})", values)
                    inserted_count += 1

            _cleanup_old_batches(conn, sync_id)

            duration = time.time() - start
            conn.execute(
                "UPDATE sync_logs SET status='success', completed_at=?, "
                "records_fetched=?, records_inserted=?, duration_seconds=? WHERE id=?",
                (datetime.utcnow().isoformat(), len(rows),
                 updated_count + inserted_count, duration, sync_id)
            )

        return {
            "status": "success",
            "actualizados": updated_count,
            "nuevos": inserted_count,
            "filas_archivo": len(rows),
            "duration": round(duration, 2),
        }

    except Exception as e:
        duration = time.time() - start
        try:
            with get_db() as conn:
                conn.execute(
                    "UPDATE sync_logs SET status='failed', completed_at=?, "
                    "error_message=?, duration_seconds=? WHERE id=?",
                    (datetime.utcnow().isoformat(),
                     f"{type(e).__name__}: {str(e)[:400]}", duration, sync_id)
                )
        except Exception:
            pass
        raise
