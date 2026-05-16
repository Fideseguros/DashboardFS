"""Estado de Resultados Integral mensual.

Estructura del Excel esperado:
- Hoja 'EST. DE RES. MES A MES '
- Col 1 = código + descripción de cuenta (PUC, ej. '41502001 INTERESES FINANCIACION POLIZAS')
- Col 3..25 = valores mensuales (ENERO..DICIEMBRE), intervalos de 2
- Col 27 = ACUMULADO (no se usa, lo calculamos)
- Filas con prefijo 'Total ...' son subtotales (is_total=1)
- El año se infiere del título "ESTADO DE RESULTADO INTEGRAL - MENSUALES DEL <YYYY>"
"""
import io
import logging
import re
from datetime import datetime
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, Request
import openpyxl
from app.database import get_db, get_connection
from app.auth.middleware import require_auth, require_superadmin
from app.audit import log_audit, get_client_ip

router = APIRouter(prefix="/api/estados-financieros", tags=["estados-financieros"])

_log = logging.getLogger("fide.financieros")
MAX_UPLOAD_MB = 25

# Columnas de meses en el Excel (0-indexed): ENERO en col 3, FEB en 5, ... DIC en 25
MONTH_COLS = {1: 3, 2: 5, 3: 7, 4: 9, 5: 11, 6: 13, 7: 15, 8: 17, 9: 19, 10: 21, 11: 23, 12: 25}


def _to_float(v):
    if v is None: return 0.0
    if isinstance(v, (int, float)): return float(v)
    s = str(v).strip()
    if not s: return 0.0
    try: return float(s.replace(',', '').replace('%', ''))
    except ValueError: return 0.0


def _parse_cuenta(raw: str):
    """De '41502001 INTERESES FINANCIACION POLIZAS' devuelve (code, desc, nivel, is_total).

    Detecta filas 'Total xxxx' como is_total=True con code=xxxx.
    """
    raw = (raw or '').strip()
    if not raw:
        return None
    if raw.upper() == 'CUENTA':
        return None
    # Detección de 'Total ...'
    is_total = raw.lower().startswith('total')
    if is_total:
        # 'Total 4150 INGRESOS FINANCIEROS' → code 4150
        m = re.match(r'Total\s+(\d+)\s*(.*)', raw, re.IGNORECASE)
        if not m:
            return None
        code = m.group(1)
        desc = m.group(2).strip()
    else:
        # Cuenta normal: '41502001 INTERESES ...'
        m = re.match(r'(\d+)\s+(.+)', raw)
        if not m:
            return None
        code = m.group(1)
        desc = m.group(2).strip()
    nivel = len(code)
    # Parent code: nivel anterior (cortar de a 2 dígitos para subgrupos PUC)
    parent = None
    if nivel >= 8:    parent = code[:6]
    elif nivel >= 6:  parent = code[:4]
    elif nivel >= 4:  parent = code[:2]
    elif nivel >= 2:  parent = code[:1]
    return code, desc, nivel, parent, is_total


def _detect_year(rows: list[tuple]) -> int | None:
    """Busca el año en las primeras filas del Excel."""
    for r in rows[:10]:
        for v in r:
            if v and isinstance(v, str):
                m = re.search(r'(20\d{2})', v)
                if m:
                    return int(m.group(1))
    return None


@router.post("/upload")
async def upload(request: Request, user=Depends(require_superadmin), file: UploadFile = File(...)):
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="Solo archivos .xlsx o .xls")
    content = await file.read()
    if len(content) / (1024 * 1024) > MAX_UPLOAD_MB:
        raise HTTPException(status_code=413, detail=f"Archivo excede {MAX_UPLOAD_MB} MB")

    ip = get_client_ip(request) or "unknown"
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sync_logs (started_at, status, source, uploaded_by) VALUES (?, 'running', 'financieros_upload', ?)",
            (datetime.utcnow().isoformat(), user["user_id"])
        )
        sync_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
        sheet_name = next((s for s in wb.sheetnames if 'mes' in s.lower() or 'res' in s.lower()), wb.sheetnames[0])
        ws = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
        wb.close()

        year = _detect_year(rows)
        if not year:
            raise ValueError("No se pudo detectar el año en el archivo")

        records = []
        for r in rows:
            if not r or len(r) < 2:
                continue
            parsed = _parse_cuenta(r[1] if r[1] else '')
            if not parsed:
                continue
            code, desc, nivel, parent, is_total = parsed
            # Para cada mes, si tiene valor != 0, guardar
            for month, col_idx in MONTH_COLS.items():
                if col_idx >= len(r):
                    continue
                val = _to_float(r[col_idx])
                # Guardar incluso valores 0 para meses con datos del año actual
                # Pero solo guardamos meses con algún valor real para no inflar
                if val == 0:
                    continue
                records.append({
                    'year': year, 'month': month,
                    'cuenta_code': code, 'cuenta_descripcion': desc,
                    'nivel': nivel, 'parent_code': parent,
                    'is_total': 1 if is_total else 0, 'valor': val,
                })

        # Reemplazar año completo
        with get_db() as conn:
            conn.execute("DELETE FROM estados_financieros WHERE year = ?", (year,))
            for rec in records:
                conn.execute("""
                    INSERT INTO estados_financieros
                    (year, month, cuenta_code, cuenta_descripcion, nivel, parent_code, is_total, valor, sync_batch_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (rec['year'], rec['month'], rec['cuenta_code'], rec['cuenta_descripcion'],
                      rec['nivel'], rec['parent_code'], rec['is_total'], rec['valor'], sync_id))
            conn.execute("""
                UPDATE sync_logs SET status='success', completed_at=?, records_fetched=?, records_inserted=?
                WHERE id=?
            """, (datetime.utcnow().isoformat(), len(rows), len(records), sync_id))

        log_audit(user["user_id"], user["username"], "financieros_upload",
                  f"file={file.filename} year={year} records={len(records)}", ip)
        return {"status": "success", "year": year, "records": len(records)}
    except Exception as e:
        _log.exception("financieros_upload failed")
        with get_db() as conn:
            conn.execute("""
                UPDATE sync_logs SET status='failed', completed_at=?, error_message=? WHERE id=?
            """, (datetime.utcnow().isoformat(), f"{type(e).__name__}: {str(e)[:200]}", sync_id))
        log_audit(user["user_id"], user["username"], "financieros_upload_failed", str(e)[:200], ip)
        raise HTTPException(status_code=500, detail=f"No se pudo importar el archivo. {type(e).__name__}: {str(e)[:200]}")


@router.get("/years")
def years_available(_user=Depends(require_auth)):
    conn = get_connection()
    try:
        rows = conn.execute("SELECT DISTINCT year FROM estados_financieros ORDER BY year DESC").fetchall()
        return [r["year"] for r in rows]
    finally:
        conn.close()


@router.get("")
def list_records(year: int, _user=Depends(require_auth)):
    """Todas las filas del año (todos los meses y cuentas), incluyendo subtotales."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT year, month, cuenta_code, cuenta_descripcion, nivel, parent_code, is_total, valor "
            "FROM estados_financieros WHERE year = ? ORDER BY cuenta_code, month",
            (year,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/summary")
def summary(year: int, _user=Depends(require_auth)):
    """KPIs del año: ingresos, gastos, costos, utilidad bruta/operacional/neta + por mes."""
    conn = get_connection()
    try:
        # Top-level: códigos PUC '4' (ingresos), '5' (gastos), '6' (costos), '7' (costos producción)
        # Sumamos los valores de cuentas DETALLE (is_total=0) para evitar doble contabilización
        agg = conn.execute("""
            SELECT
                month,
                SUM(CASE WHEN substr(cuenta_code,1,1)='4' AND is_total=0 THEN valor ELSE 0 END) as ingresos,
                SUM(CASE WHEN substr(cuenta_code,1,1)='5' AND is_total=0 THEN valor ELSE 0 END) as gastos,
                SUM(CASE WHEN substr(cuenta_code,1,1)='6' AND is_total=0 THEN valor ELSE 0 END) as costos,
                SUM(CASE WHEN substr(cuenta_code,1,1)='7' AND is_total=0 THEN valor ELSE 0 END) as costos_prod
            FROM estados_financieros WHERE year = ?
            GROUP BY month ORDER BY month
        """, (year,)).fetchall()

        by_month = []
        total_ingresos = total_gastos = total_costos = 0.0
        for r in agg:
            d = dict(r)
            d['utilidad'] = (d['ingresos'] or 0) - (d['gastos'] or 0) - (d['costos'] or 0) - (d['costos_prod'] or 0)
            by_month.append(d)
            total_ingresos += d['ingresos'] or 0
            total_gastos += d['gastos'] or 0
            total_costos += (d['costos'] or 0) + (d['costos_prod'] or 0)
        utilidad = total_ingresos - total_gastos - total_costos
        margen = (utilidad / total_ingresos * 100) if total_ingresos > 0 else 0
        n_months = len(by_month)
        return {
            'year': year,
            'meses_con_datos': n_months,
            'ingresos_total': total_ingresos,
            'gastos_total': total_gastos,
            'costos_total': total_costos,
            'utilidad_total': utilidad,
            'margen_pct': margen,
            'ingresos_promedio_mensual': total_ingresos / n_months if n_months else 0,
            'gastos_promedio_mensual': total_gastos / n_months if n_months else 0,
            'utilidad_promedio_mensual': utilidad / n_months if n_months else 0,
            'by_month': by_month,
        }
    finally:
        conn.close()
