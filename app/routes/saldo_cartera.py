"""Saldo de Cartera — snapshot agregado del archivo Loggro 'Resumen Estado Cuenta'.

Plantilla del archivo (`Resumen Estado Cuenta YYYYMMDD.xlsx`):
- Fila 1 = headers
- Filas 2..N = una por cuenta de cartera
- Columnas relevantes (1-indexed):
    9  Capital Prestamos
    10 Interes Corriente
    11 Interes De Mora      ← se MUESTRA pero NO suma al saldo_cartera
    12 Cargos Administrativos
    13 Deudores Varios
    14 Retencion En La Fuente
    17 Total                ← bruto del Excel (incluye Mora)
- Solo guardamos AGREGADOS — sin detalle por cuenta ni PII.
- saldo_cartera = total_general − total_int_mora.
"""
import re
import logging
import sqlite3
from datetime import date, datetime
from fastapi import APIRouter, Depends, UploadFile, File, Request, HTTPException
from app.database import get_db, get_connection
from app.auth.middleware import require_auth, require_superadmin
from app.sync.upload_helpers import upload_session, to_float

router = APIRouter(prefix="/api/saldo-cartera", tags=["saldo-cartera"])
_log = logging.getLogger("fide.saldo_cartera")
MAX_UPLOAD_MB = 25

# Columnas (1-indexed Excel → 0-indexed tuple)
COL_CAPITAL = 8
COL_INT_CORRIENTE = 9
COL_INT_MORA = 10
COL_CARGOS_ADMIN = 11
COL_DEUDORES_VARIOS = 12
COL_RETENCION = 13
COL_TOTAL = 16


def _detect_snapshot_date(filename: str) -> str:
    """Extrae YYYYMMDD del nombre del archivo, fallback a hoy.

    Ejemplo: 'Resumen Estado Cuenta 20260604.xlsx' → '2026-06-04'.
    """
    if filename:
        m = re.search(r'(20\d{2})(\d{2})(\d{2})', filename)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
            except ValueError:
                pass
    return date.today().isoformat()


@router.post("/upload")
async def upload(request: Request, user=Depends(require_superadmin), file: UploadFile = File(...)):
    async with upload_session(
        request, user, file, source="saldo_cartera_upload", max_mb=MAX_UPLOAD_MB,
        generic_error_msg="No se pudo importar el archivo. Revisa el formato del Resumen Estado Cuenta."
    ) as ctx:
        rows = ctx.read_excel()
        if not rows or len(rows) < 2:
            raise ValueError("Archivo vacío o sin filas de datos")

        snapshot_date = _detect_snapshot_date(file.filename or "")

        total_capital = total_int_corr = total_int_mora = 0.0
        total_cargos = total_deudores = total_retencion = total_general = 0.0
        n = 0
        # Skip fila 1 (headers). Una fila válida tiene Numero Cuenta + algún monto.
        for r in rows[1:]:
            if not r or len(r) <= COL_TOTAL:
                continue
            cuenta = r[0]
            if cuenta is None or str(cuenta).strip() == '':
                continue  # filas vacías al final
            n += 1
            total_capital      += to_float(r[COL_CAPITAL]) or 0
            total_int_corr     += to_float(r[COL_INT_CORRIENTE]) or 0
            total_int_mora     += to_float(r[COL_INT_MORA]) or 0
            total_cargos       += to_float(r[COL_CARGOS_ADMIN]) or 0
            total_deudores     += to_float(r[COL_DEUDORES_VARIOS]) or 0
            total_retencion    += to_float(r[COL_RETENCION]) or 0
            total_general      += to_float(r[COL_TOTAL]) or 0
        saldo_cartera = total_general - total_int_mora

        try:
            with get_db() as conn:
                # Reemplazar snapshot de la misma fecha si ya existe (re-upload del mismo día).
                conn.execute(
                    "DELETE FROM saldo_cartera_snapshots WHERE snapshot_date = ?",
                    (snapshot_date,)
                )
                conn.execute("""
                    INSERT INTO saldo_cartera_snapshots
                    (snapshot_date, n_cuentas, total_capital, total_int_corriente,
                     total_int_mora, total_cargos_admin, total_deudores_varios,
                     total_retencion_fuente, total_general, saldo_cartera, sync_batch_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (snapshot_date, n, total_capital, total_int_corr, total_int_mora,
                      total_cargos, total_deudores, total_retencion, total_general,
                      saldo_cartera, ctx.sync_id))
        except sqlite3.IntegrityError:
            # Race condition: otro upload del mismo snapshot_date ganó la carrera.
            raise HTTPException(
                status_code=409,
                detail=f"Otra carga del {snapshot_date} ya está procesándose. Espera unos segundos y reintenta."
            )

        ctx.set_counts(fetched=len(rows), inserted=n)
        ctx.set_audit_extra(f"snapshot_date={snapshot_date} n_cuentas={n} saldo={saldo_cartera:.0f}")
        return {
            "status": "success",
            "snapshot_date": snapshot_date,
            "n_cuentas": n,
            "saldo_cartera": saldo_cartera,
            "total_general": total_general,
            "total_int_mora": total_int_mora,
        }


@router.get("/latest")
def latest(_user=Depends(require_auth)):
    """Última snapshot disponible (la más reciente por snapshot_date)."""
    conn = get_connection()
    try:
        r = conn.execute("""
            SELECT snapshot_date, n_cuentas, total_capital, total_int_corriente,
                   total_int_mora, total_cargos_admin, total_deudores_varios,
                   total_retencion_fuente, total_general, saldo_cartera, created_at
            FROM saldo_cartera_snapshots
            ORDER BY snapshot_date DESC, id DESC LIMIT 1
        """).fetchone()
        if not r:
            return None
        return dict(r)
    finally:
        conn.close()


@router.get("/history")
def history(limit: int = 60, _user=Depends(require_auth)):
    """Histórico de snapshots para sparkline / evolución."""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT snapshot_date, saldo_cartera, total_general, total_int_mora, n_cuentas
            FROM saldo_cartera_snapshots
            ORDER BY snapshot_date DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
