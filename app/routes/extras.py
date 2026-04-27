"""Routers para módulos adicionales: Recaudo, Solicitudes, Cobro Jurídico.

Cada módulo expone:
  - POST /api/{modulo}/upload    (superadmin)
  - GET  /api/{modulo}           (auth)
  - GET  /api/{modulo}/summary   (auth)
"""
import io
import logging
import zipfile
import xml.etree.ElementTree as ET
import re
from datetime import datetime, date
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, Request
import openpyxl
from app.database import get_db, get_connection
from app.auth.middleware import require_auth, require_superadmin
from app.audit import log_audit, get_client_ip
from app.crypto import encrypt, decrypt, mask_identificacion, mask_cliente

_log = logging.getLogger("fide.extras")
MAX_UPLOAD_MB = 20
MAX_ROWS = 50_000


# ============================================================
#                   HELPERS DE PARSEO
# ============================================================
def _parse_with_openpyxl(content: bytes):
    """Parser principal — funciona con la mayoría de Excel."""
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    return rows


def _parse_with_zip_xml(content: bytes):
    """Fallback para archivos con estilos corruptos (ListadoSolicitudes)."""
    ns = {'main': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(io.BytesIO(content), 'r') as z:
        shared = []
        if 'xl/sharedStrings.xml' in z.namelist():
            with z.open('xl/sharedStrings.xml') as f:
                root = ET.parse(f).getroot()
                for si in root.findall('main:si', ns):
                    t_els = si.findall('.//main:t', ns)
                    shared.append(''.join(t.text or '' for t in t_els))
        sheet_files = sorted(n for n in z.namelist() if n.startswith('xl/worksheets/sheet'))
        if not sheet_files:
            return []
        with z.open(sheet_files[0]) as f:
            root = ET.parse(f).getroot()
            rows_el = root.findall('.//main:sheetData/main:row', ns)

            def col_idx(letter):
                n = 0
                for c in letter:
                    n = n * 26 + (ord(c.upper()) - ord('A') + 1)
                return n - 1

            rows_out = []
            for row in rows_el:
                cells = row.findall('main:c', ns)
                row_dict = {}
                for c in cells:
                    ref = c.get('r', '')
                    m = re.match(r'([A-Z]+)\d+', ref)
                    if not m:
                        continue
                    ci = col_idx(m.group(1))
                    t = c.get('t')
                    v_el = c.find('main:v', ns)
                    if v_el is None:
                        if t == 'inlineStr':
                            is_el = c.find('main:is', ns)
                            t_el = is_el.find('main:t', ns) if is_el is not None else None
                            row_dict[ci] = t_el.text if t_el is not None else None
                        continue
                    v = v_el.text
                    if t == 's' and v is not None:
                        try:
                            v = shared[int(v)]
                        except (ValueError, IndexError):
                            pass
                    row_dict[ci] = v
                if row_dict:
                    max_c = max(row_dict.keys()) + 1
                    rows_out.append(tuple(row_dict.get(i) for i in range(max_c)))
            return rows_out


def _read_excel(content: bytes):
    """Intenta openpyxl primero, fallback a parser XML directo."""
    try:
        return _parse_with_openpyxl(content)
    except Exception as e:
        _log.warning("openpyxl falló (%s), usando parser XML directo", e)
        return _parse_with_zip_xml(content)


def _to_float(v):
    if v is None or v == '':
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace('%', '').replace(',', '').strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_date(v):
    """Normaliza a YYYY-MM-DD."""
    if v is None or v == '':
        return None
    if isinstance(v, (datetime, date)):
        return v.strftime('%Y-%m-%d')
    s = str(v).strip()
    if not s:
        return None
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%d/%m/%Y', '%Y-%m-%dT%H:%M:%S', '%Y/%m/%d %H:%M:%S'):
        try:
            return datetime.strptime(s[:19], fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue
    return None


def _str_or_none(v):
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _create_sync_log(uploaded_by: int, source: str) -> int:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sync_logs (started_at, status, source, uploaded_by) VALUES (?, 'running', ?, ?)",
            (datetime.utcnow().isoformat(), source, uploaded_by)
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _finalize_sync(sync_id: int, status: str, fetched: int, inserted: int, error: str = ''):
    with get_db() as conn:
        conn.execute(
            "UPDATE sync_logs SET status=?, completed_at=?, records_fetched=?, records_inserted=?, error_message=? WHERE id=?",
            (status, datetime.utcnow().isoformat(), fetched, inserted, error[:400], sync_id)
        )


def _check_upload(file: UploadFile, content: bytes):
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="Solo archivos .xlsx o .xls")
    size_mb = len(content) / (1024 * 1024)
    if size_mb > MAX_UPLOAD_MB:
        raise HTTPException(status_code=413, detail=f"Archivo excede {MAX_UPLOAD_MB} MB")


# ============================================================
#                    MÓDULO RECAUDO (Pagos)
# ============================================================
recaudo = APIRouter(prefix="/api/recaudo", tags=["recaudo"])

# Mapeo columna Excel -> campo BD (basado en IngresosDesembolso-Pagos.xlsx)
PAGOS_COL_MAP = {
    0: ('entidad', _str_or_none),
    1: ('linea_credito', _str_or_none),
    2: ('fecha_movimiento', _to_date),
    3: ('fecha_documento', _to_date),
    4: ('identificacion', None),  # cifrado abajo
    5: ('cliente', None),         # cifrado
    6: ('cuenta', _str_or_none),
    7: ('solicitud', _str_or_none),
    8: ('aliado', _str_or_none),
    9: ('tipo_mvto', _str_or_none),
    10: ('tipo_documento', _str_or_none),
    11: ('documento', _str_or_none),
    12: ('usuario', _str_or_none),
    14: ('capital', _to_float),
    15: ('interes_corriente', _to_float),
    16: ('interes_mora', _to_float),
    17: ('iva', _to_float),
    18: ('saldo_favor', _to_float),
    19: ('gastos_pj', _to_float),
    20: ('cargos_admin', _to_float),
    21: ('total', _to_float),
    22: ('total_cheque', _to_float),
    23: ('total_efectivo', _to_float),
    24: ('total_tarjeta', _to_float),
    25: ('total_interno', _to_float),
    26: ('autorizacion', _str_or_none),
    27: ('observaciones', _str_or_none),
}


def _transform_pago_row(row: tuple) -> dict | None:
    rec = {}
    for idx, (key, parser) in PAGOS_COL_MAP.items():
        val = row[idx] if idx < len(row) else None
        if key == 'identificacion':
            v = _str_or_none(val); rec[key] = encrypt(v) if v else None
        elif key == 'cliente':
            v = _str_or_none(val); rec[key] = encrypt(v) if v else None
        else:
            rec[key] = parser(val) if parser else val
    if not rec.get('total') and not rec.get('capital'):
        return None
    return rec


@recaudo.post("/upload")
async def recaudo_upload(request: Request, user=Depends(require_superadmin), file: UploadFile = File(...)):
    content = await file.read()
    _check_upload(file, content)
    ip = get_client_ip(request) or "unknown"
    sync_id = _create_sync_log(user["user_id"], "recaudo_upload")
    try:
        rows = _read_excel(content)
        if len(rows) > MAX_ROWS:
            raise ValueError(f"Excede {MAX_ROWS} filas")
        records = [r for r in (_transform_pago_row(row) for row in rows[1:]) if r]

        cols = list(records[0].keys()) if records else []
        with get_db() as conn:
            conn.execute("DELETE FROM pagos WHERE sync_batch_id IN (SELECT id FROM sync_logs WHERE source='recaudo_upload' AND id < ?)", (sync_id,))
            placeholders = ', '.join(['?'] * (len(cols) + 1))
            cols_sql = ', '.join(cols + ['sync_batch_id'])
            for rec in records:
                conn.execute(f"INSERT INTO pagos ({cols_sql}) VALUES ({placeholders})",
                             [rec.get(c) for c in cols] + [sync_id])
        _finalize_sync(sync_id, 'success', len(rows), len(records))
        log_audit(user["user_id"], user["username"], "recaudo_upload",
                  f"file={file.filename} records={len(records)}", ip)
        return {"status": "success", "records": len(records)}
    except Exception as e:
        _log.exception("recaudo_upload failed")
        _finalize_sync(sync_id, 'failed', 0, 0, f"{type(e).__name__}: {e}")
        log_audit(user["user_id"], user["username"], "recaudo_upload_failed", str(e)[:200], ip)
        raise HTTPException(status_code=500, detail="No se pudo importar el archivo")


@recaudo.get("")
def recaudo_list(_user=Depends(require_auth)):
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM pagos WHERE sync_batch_id = (SELECT id FROM sync_logs WHERE source='recaudo_upload' AND status='success' ORDER BY id DESC LIMIT 1)"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            ident = decrypt(d.get('identificacion'))
            cli = decrypt(d.get('cliente'))
            d['identificacion'] = mask_identificacion(ident)
            d['cliente'] = mask_cliente(cli)
            out.append(d)
        return out
    finally:
        conn.close()


@recaudo.get("/summary")
def recaudo_summary(_user=Depends(require_auth)):
    conn = get_connection()
    try:
        batch = "(SELECT id FROM sync_logs WHERE source='recaudo_upload' AND status='success' ORDER BY id DESC LIMIT 1)"
        row = conn.execute(f"""
            SELECT COUNT(*) as total,
                COALESCE(SUM(total),0) as recaudo_total,
                COALESCE(SUM(capital),0) as capital_total,
                COALESCE(SUM(interes_corriente),0) as interes_corr,
                COALESCE(SUM(interes_mora),0) as interes_mora,
                COALESCE(SUM(total_efectivo),0) as efectivo,
                COALESCE(SUM(total_cheque),0) as cheque,
                COALESCE(SUM(total_tarjeta),0) as tarjeta,
                COALESCE(SUM(total_interno),0) as interno
            FROM pagos WHERE sync_batch_id = {batch}
        """).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


# ============================================================
#                  MÓDULO SOLICITUDES (Pipeline)
# ============================================================
solicitudes = APIRouter(prefix="/api/solicitudes", tags=["solicitudes"])

SOLIC_COL_MAP = {
    0: ('solicitud_origen', _str_or_none),
    1: ('solicitud', _str_or_none),
    2: ('hoja_ruta', _str_or_none),
    3: ('linea', _str_or_none),
    4: ('identificacion', None),  # cifrado
    5: ('solicitante', None),     # cifrado
    6: ('tipo_moneda', _str_or_none),
    7: ('valor', _to_float),
    8: ('paso_ruta', _str_or_none),
    9: ('responsable', _str_or_none),
    10: ('estado', _str_or_none),
    11: ('subestado', _str_or_none),
    12: ('empresa', _str_or_none),
    13: ('oficina', _str_or_none),
    14: ('fecha_solicitud', _to_date),
    15: ('periodo_convocatoria', _str_or_none),
    16: ('auxilio', _str_or_none),
    17: ('usuario', _str_or_none),
}


def _transform_solic_row(row: tuple) -> dict | None:
    rec = {}
    for idx, (key, parser) in SOLIC_COL_MAP.items():
        val = row[idx] if idx < len(row) else None
        if key == 'identificacion':
            v = _str_or_none(val); rec[key] = encrypt(v) if v else None
        elif key == 'solicitante':
            v = _str_or_none(val); rec[key] = encrypt(v) if v else None
        else:
            rec[key] = parser(val) if parser else val
    if not rec.get('solicitud') and not rec.get('estado'):
        return None
    return rec


@solicitudes.post("/upload")
async def solic_upload(request: Request, user=Depends(require_superadmin), file: UploadFile = File(...)):
    content = await file.read()
    _check_upload(file, content)
    ip = get_client_ip(request) or "unknown"
    sync_id = _create_sync_log(user["user_id"], "solicitudes_upload")
    try:
        rows = _read_excel(content)
        if len(rows) > MAX_ROWS:
            raise ValueError(f"Excede {MAX_ROWS} filas")
        records = [r for r in (_transform_solic_row(row) for row in rows[1:]) if r]
        cols = list(records[0].keys()) if records else []
        with get_db() as conn:
            conn.execute("DELETE FROM solicitudes WHERE sync_batch_id IN (SELECT id FROM sync_logs WHERE source='solicitudes_upload' AND id < ?)", (sync_id,))
            placeholders = ', '.join(['?'] * (len(cols) + 1))
            cols_sql = ', '.join(cols + ['sync_batch_id'])
            for rec in records:
                conn.execute(f"INSERT INTO solicitudes ({cols_sql}) VALUES ({placeholders})",
                             [rec.get(c) for c in cols] + [sync_id])
        _finalize_sync(sync_id, 'success', len(rows), len(records))
        log_audit(user["user_id"], user["username"], "solicitudes_upload",
                  f"file={file.filename} records={len(records)}", ip)
        return {"status": "success", "records": len(records)}
    except Exception as e:
        _log.exception("solicitudes_upload failed")
        _finalize_sync(sync_id, 'failed', 0, 0, f"{type(e).__name__}: {e}")
        log_audit(user["user_id"], user["username"], "solicitudes_upload_failed", str(e)[:200], ip)
        raise HTTPException(status_code=500, detail="No se pudo importar el archivo")


@solicitudes.get("")
def solic_list(_user=Depends(require_auth)):
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM solicitudes WHERE sync_batch_id = (SELECT id FROM sync_logs WHERE source='solicitudes_upload' AND status='success' ORDER BY id DESC LIMIT 1)"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            ident = decrypt(d.get('identificacion'))
            sol = decrypt(d.get('solicitante'))
            d['identificacion'] = mask_identificacion(ident)
            d['solicitante'] = mask_cliente(sol)
            out.append(d)
        return out
    finally:
        conn.close()


@solicitudes.get("/summary")
def solic_summary(_user=Depends(require_auth)):
    conn = get_connection()
    try:
        batch = "(SELECT id FROM sync_logs WHERE source='solicitudes_upload' AND status='success' ORDER BY id DESC LIMIT 1)"
        row = conn.execute(f"""
            SELECT COUNT(*) as total,
                COALESCE(SUM(valor),0) as valor_total,
                SUM(CASE WHEN estado='DESEMBOLSADA' THEN 1 ELSE 0 END) as desembolsadas,
                SUM(CASE WHEN estado='DESEMBOLSADA' THEN COALESCE(valor,0) ELSE 0 END) as valor_desembolsado,
                SUM(CASE WHEN estado<>'DESEMBOLSADA' AND estado IS NOT NULL THEN 1 ELSE 0 END) as pipeline,
                SUM(CASE WHEN estado<>'DESEMBOLSADA' AND estado IS NOT NULL THEN COALESCE(valor,0) ELSE 0 END) as valor_pipeline
            FROM solicitudes WHERE sync_batch_id = {batch}
        """).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


# ============================================================
#                  MÓDULO COBRO JURÍDICO
#       (datos sensibles → solo superadmin para list/upload)
# ============================================================
juridico = APIRouter(prefix="/api/juridico", tags=["juridico"])

JURIDICO_COL_MAP = {
    0: ('identificacion', None),  # cifrado
    1: ('nombre', None),          # cifrado
    2: ('naturaleza_litigio', _str_or_none),
    3: ('avance', _str_or_none),
    4: ('respuesta_compania', _str_or_none),
    5: ('probabilidad', _str_or_none),
    6: ('medida_cautelar', _str_or_none),
    7: ('juzgado', _str_or_none),
}


def _transform_juridico_row(row: tuple) -> dict | None:
    rec = {}
    for idx, (key, parser) in JURIDICO_COL_MAP.items():
        val = row[idx] if idx < len(row) else None
        if key in ('identificacion', 'nombre'):
            v = _str_or_none(val); rec[key] = encrypt(v) if v else None
        else:
            rec[key] = parser(val) if parser else val
    if not rec.get('nombre') and not rec.get('juzgado'):
        return None
    return rec


@juridico.post("/upload")
async def jur_upload(request: Request, user=Depends(require_superadmin), file: UploadFile = File(...)):
    content = await file.read()
    _check_upload(file, content)
    ip = get_client_ip(request) or "unknown"
    sync_id = _create_sync_log(user["user_id"], "juridico_upload")
    try:
        rows = _read_excel(content)
        records = [r for r in (_transform_juridico_row(row) for row in rows[2:]) if r]
        cols = list(records[0].keys()) if records else []
        with get_db() as conn:
            conn.execute("DELETE FROM procesos_juridicos WHERE sync_batch_id IN (SELECT id FROM sync_logs WHERE source='juridico_upload' AND id < ?)", (sync_id,))
            placeholders = ', '.join(['?'] * (len(cols) + 1))
            cols_sql = ', '.join(cols + ['sync_batch_id'])
            for rec in records:
                conn.execute(f"INSERT INTO procesos_juridicos ({cols_sql}) VALUES ({placeholders})",
                             [rec.get(c) for c in cols] + [sync_id])
        _finalize_sync(sync_id, 'success', len(rows), len(records))
        log_audit(user["user_id"], user["username"], "juridico_upload",
                  f"file={file.filename} records={len(records)}", ip)
        return {"status": "success", "records": len(records)}
    except Exception as e:
        _log.exception("juridico_upload failed")
        _finalize_sync(sync_id, 'failed', 0, 0, f"{type(e).__name__}: {e}")
        log_audit(user["user_id"], user["username"], "juridico_upload_failed", str(e)[:200], ip)
        raise HTTPException(status_code=500, detail="No se pudo importar el archivo")


@juridico.get("")
def jur_list(user=Depends(require_superadmin)):
    """Listado completo, cruzado con cartera por identificación.

    Para cada proceso: agrega resumen del crédito (estado, saldo, días mora,
    valor crédito, fechas) si el cliente existe en la cartera.

    Por contener PII de cobro → solo superadmin.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM procesos_juridicos WHERE sync_batch_id = (SELECT id FROM sync_logs WHERE source='juridico_upload' AND status='success' ORDER BY id DESC LIMIT 1)"
        ).fetchall()
        if not rows:
            return []

        # Construir índice de cartera por identificación (decrypt una sola vez).
        # Solo el último batch exitoso de cartera.
        credit_rows = conn.execute("""
            SELECT identificacion, cliente, estado, linea, valor_credito, saldo_capital,
                   dias_mora, calificacion, fecha_desembolso, fecha_vencimiento, aliado
            FROM credits
            WHERE sync_batch_id = (
                SELECT id FROM sync_logs
                WHERE source='manual_upload' AND status='success'
                ORDER BY id DESC LIMIT 1
            )
        """).fetchall()

        cartera_idx = {}
        for c in credit_rows:
            ident = decrypt(c["identificacion"])
            if not ident:
                continue
            # Un cliente puede tener varios créditos: agregamos saldo, máx mora.
            agg = cartera_idx.setdefault(ident, {
                "creditos_total": 0,
                "creditos_activos": 0,
                "saldo_capital_total": 0.0,
                "valor_credito_total": 0.0,
                "max_dias_mora": 0,
                "estado_principal": None,
                "lineas": set(),
                "calificaciones": set(),
                "aliado_principal": None,
            })
            agg["creditos_total"] += 1
            if c["estado"] == "ACTIVO":
                agg["creditos_activos"] += 1
            agg["saldo_capital_total"] += float(c["saldo_capital"] or 0)
            agg["valor_credito_total"] += float(c["valor_credito"] or 0)
            dm = int(c["dias_mora"] or 0)
            if dm > agg["max_dias_mora"]:
                agg["max_dias_mora"] = dm
            if c["linea"]:
                agg["lineas"].add(c["linea"])
            if c["calificacion"]:
                agg["calificaciones"].add((c["calificacion"] or "").strip())
            # estado/aliado del crédito con mayor saldo
            if not agg["estado_principal"] or (c["estado"] == "ACTIVO" and agg["estado_principal"] != "ACTIVO"):
                agg["estado_principal"] = c["estado"]
                agg["aliado_principal"] = c["aliado"]

        out = []
        for r in rows:
            d = dict(r)
            d['identificacion'] = decrypt(d.get('identificacion'))
            d['nombre'] = decrypt(d.get('nombre'))
            # Cruce
            cartera = cartera_idx.get(str(d['identificacion']) if d['identificacion'] else None)
            if cartera:
                d['cartera_creditos_total'] = cartera['creditos_total']
                d['cartera_creditos_activos'] = cartera['creditos_activos']
                d['cartera_saldo_capital'] = cartera['saldo_capital_total']
                d['cartera_valor_credito'] = cartera['valor_credito_total']
                d['cartera_dias_mora_max'] = cartera['max_dias_mora']
                d['cartera_estado'] = cartera['estado_principal']
                d['cartera_lineas'] = ', '.join(sorted(cartera['lineas']))
                d['cartera_calificaciones'] = ', '.join(sorted(c for c in cartera['calificaciones'] if c))
                d['cartera_aliado'] = cartera['aliado_principal']
                d['cartera_match'] = True
            else:
                d['cartera_match'] = False
            out.append(d)
        return out
    finally:
        conn.close()


@juridico.get("/cartera-summary")
def jur_cartera_summary(user=Depends(require_superadmin)):
    """Resumen del cruce procesos jurídicos vs cartera."""
    rows = jur_list(user)
    if not rows:
        return {"total": 0, "matched": 0, "saldo_total_cartera": 0, "saldo_en_mora": 0}
    matched = [r for r in rows if r.get('cartera_match')]
    return {
        "total": len(rows),
        "matched": len(matched),
        "saldo_total_cartera": sum(r.get('cartera_saldo_capital') or 0 for r in matched),
        "saldo_en_mora": sum(
            r.get('cartera_saldo_capital') or 0 for r in matched
            if (r.get('cartera_dias_mora_max') or 0) > 30
        ),
    }


@juridico.get("/summary")
def jur_summary(user=Depends(require_superadmin)):
    conn = get_connection()
    try:
        batch = "(SELECT id FROM sync_logs WHERE source='juridico_upload' AND status='success' ORDER BY id DESC LIMIT 1)"
        row = conn.execute(f"""
            SELECT COUNT(*) as total,
                SUM(CASE WHEN LOWER(probabilidad) LIKE '%probable%' THEN 1 ELSE 0 END) as probables,
                SUM(CASE WHEN LOWER(probabilidad) LIKE '%remot%' THEN 1 ELSE 0 END) as remotas,
                COUNT(DISTINCT juzgado) as juzgados,
                SUM(CASE WHEN medida_cautelar IS NOT NULL AND medida_cautelar <> '' AND medida_cautelar <> 'N/A' THEN 1 ELSE 0 END) as con_medida
            FROM procesos_juridicos WHERE sync_batch_id = {batch}
        """).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()
