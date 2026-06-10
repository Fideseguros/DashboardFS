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

# Validación de estructura: palabra clave que DEBE aparecer en el header de
# cada columna que sumamos. Si Loggro reordena/inserta columnas, el header
# no coincide y abortamos con mensaje claro en vez de sumar la columna
# equivocada en silencio (que corrompería el KPI de saldo).
EXPECTED_HEADERS = {
    COL_CAPITAL: 'capital',
    COL_INT_CORRIENTE: 'corriente',
    COL_INT_MORA: 'mora',
    COL_CARGOS_ADMIN: 'cargos',
    COL_DEUDORES_VARIOS: 'deudores',
    COL_RETENCION: 'retencion',
    COL_TOTAL: 'total',
}


def _validate_headers(header_row):
    """Verifica que cada columna esperada contenga su palabra clave.

    Tolera tildes y mayúsculas. Lanza ValueError con detalle si no coincide,
    para que el superadmin vea exactamente qué columna no cuadra.
    """
    import unicodedata
    def _norm(s):
        s = str(s or '').strip().lower()
        return ''.join(c for c in unicodedata.normalize('NFD', s)
                       if unicodedata.category(c) != 'Mn')
    problemas = []
    for col, keyword in EXPECTED_HEADERS.items():
        actual = _norm(header_row[col]) if col < len(header_row) else ''
        if keyword not in actual:
            letra = chr(ord('A') + col)  # columna en notación Excel
            problemas.append(f"columna {letra}: esperaba '{keyword}', encontré '{actual or '(vacío)'}'")
    if problemas:
        raise ValueError(
            "La estructura del archivo no coincide con el formato esperado de "
            "Loggro (Resumen Estado Cuenta). Puede que Loggro haya cambiado el "
            "orden de las columnas. Detalle: " + "; ".join(problemas)
        )


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

        # Validar que las columnas estén donde esperamos ANTES de sumar.
        _validate_headers(rows[0])

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


#: Saldo adicional FIJO pedido por la gerencia. Representa cartera que NO
#: viene en el archivo Loggro (saldos legacy, garantías, etc.) y siempre
#: se suma al saldo calculado del archivo. Cada vez que se carga un archivo
#: nuevo el KPI total = (cálculo real) + este monto fijo.
SALDO_FIJO_ADICIONAL = 110_398_316


@router.get("/latest")
def latest(_user=Depends(require_auth)):
    """Última snapshot disponible.

    Política gerencia: `saldo_cartera` = saldo calculado del archivo
    (Total − Mora) + SALDO_FIJO_ADICIONAL. El campo `saldo_cartera_real`
    expone el monto que viene del archivo solo, y `saldo_fijo_adicional`
    el sumando fijo, para que la desagregación pueda mostrar ambos
    componentes por separado al hacer click.
    """
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
            # Sin archivo cargado: el KPI sigue mostrando el saldo fijo.
            return {
                "snapshot_date": None,
                "n_cuentas": 0,
                "total_capital": 0, "total_int_corriente": 0, "total_int_mora": 0,
                "total_cargos_admin": 0, "total_deudores_varios": 0,
                "total_retencion_fuente": 0, "total_general": 0,
                "saldo_cartera_real": 0,
                "saldo_fijo_adicional": SALDO_FIJO_ADICIONAL,
                "saldo_cartera": SALDO_FIJO_ADICIONAL,
                "created_at": None,
            }
        d = dict(r)
        saldo_real = d.get('saldo_cartera') or 0  # = total_general - total_int_mora del archivo
        d['saldo_cartera_real'] = saldo_real
        d['saldo_fijo_adicional'] = SALDO_FIJO_ADICIONAL
        d['saldo_cartera'] = saldo_real + SALDO_FIJO_ADICIONAL
        return d
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
