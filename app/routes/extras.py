"""Routers para módulos adicionales: Recaudo, Solicitudes, Cobro Jurídico.

Cada módulo expone:
  - POST /api/{modulo}/upload    (superadmin)
  - GET  /api/{modulo}           (auth)
  - GET  /api/{modulo}/summary   (auth)
"""
import logging
import re
from fastapi import APIRouter, Depends, UploadFile, File, Request
from app.database import get_db, get_connection
from app.auth.middleware import require_auth, require_superadmin
from app.crypto import encrypt, decrypt, mask_identificacion, mask_cliente
from app.sync.upload_helpers import (
    upload_session,
    to_float as _to_float,
    to_date as _to_date,
    str_or_none as _str_or_none,
)

_log = logging.getLogger("fide.extras")
MAX_UPLOAD_MB = 20
MAX_ROWS = 50_000
MAX_LEGACY_MB = 50          # plataforma vieja puede ser pesada (17MB pagos)
MAX_LEGACY_ROWS = 500_000


def _upload_status_for(sources: list[str]) -> dict:
    """Devuelve el último upload de cada source (success/failed/running)."""
    conn = get_connection()
    try:
        result = {}
        for src in sources:
            row = conn.execute(
                "SELECT sl.*, u.username FROM sync_logs sl "
                "LEFT JOIN users u ON u.id = sl.uploaded_by "
                "WHERE source = ? ORDER BY id DESC LIMIT 1",
                (src,)
            ).fetchone()
            if not row:
                result[src] = None
                continue
            d = dict(row)
            # Limpiar el formato de mensaje de error para mostrar amigable
            err = d.get('error_message') or ''
            d['error_short'] = err.split(':')[0] if err else ''
            result[src] = d
        return result
    finally:
        conn.close()


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
    async with upload_session(request, user, file, source="recaudo_upload",
                              max_mb=MAX_UPLOAD_MB) as ctx:
        rows = ctx.read_excel()
        if len(rows) > MAX_ROWS:
            raise ValueError(f"Excede {MAX_ROWS} filas")
        records = [r for r in (_transform_pago_row(row) for row in rows[1:]) if r]

        cols = list(records[0].keys()) if records else []
        with get_db() as conn:
            conn.execute("DELETE FROM pagos WHERE sync_batch_id IN (SELECT id FROM sync_logs WHERE source='recaudo_upload' AND id < ?)", (ctx.sync_id,))
            placeholders = ', '.join(['?'] * (len(cols) + 1))
            cols_sql = ', '.join(cols + ['sync_batch_id'])
            for rec in records:
                conn.execute(f"INSERT INTO pagos ({cols_sql}) VALUES ({placeholders})",
                             [rec.get(c) for c in cols] + [ctx.sync_id])
        ctx.set_counts(fetched=len(rows), inserted=len(records))
        return {"status": "success", "records": len(records)}


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


# ---------- HISTÓRICO de Pagos (plataforma vieja, agregado por id_prestamo) ----------
PAGOS_LEGACY_COLS = {
    0: 'codigo', 1: 'id_prestamo', 2: 'id_solicitud',
    3: 'identificacion', 4: 'nombre',
    5: 'id_transaccion', 6: 'fecha_creacion', 7: 'fecha_valor',
    8: 'metodo_pago', 9: 'estado',
    14: 'reversado', 15: 'tipo_pago', 16: 'descripcion',
    17: 'valor_pago', 18: 'usuario',
    19: 'prestamo_cancelado', 20: 'iva_pagado', 21: 'cargos_netos',
    22: 'fecha_pago',
}


def _aggregate_pagos_legacy(rows: list[tuple]) -> list[dict]:
    """Agrupa pagos por id_prestamo, sumando valores y consolidando fechas."""
    agg = {}
    for row in rows[1:]:  # skip header
        if not row or len(row) < 18:
            continue
        get = lambda i: row[i] if i < len(row) else None
        id_prestamo = _str_or_none(get(1))
        if not id_prestamo:
            continue
        # excluir reversados
        rev = (_str_or_none(get(14)) or '').lower()
        if rev in ('si', 'yes', 'true', '1'):
            continue
        entry = agg.setdefault(id_prestamo, {
            'id_prestamo': id_prestamo,
            'id_solicitud': _str_or_none(get(2)),
            'identificacion': _str_or_none(get(3)),
            'nombre': _str_or_none(get(4)),
            'num_pagos': 0,
            'valor_pago_total': 0.0,
            'iva_pagado_total': 0.0,
            'cargos_netos_total': 0.0,
            'interes_mora_total': 0.0,
            'fecha_primer_pago': None,
            'fecha_ultimo_pago': None,
            'prestamo_cancelado': _str_or_none(get(19)),
            'metodo_pago_principal': _str_or_none(get(8)),
        })
        entry['num_pagos'] += 1
        entry['valor_pago_total'] += _to_float(get(17)) or 0
        entry['iva_pagado_total'] += _to_float(get(20)) or 0
        entry['cargos_netos_total'] += _to_float(get(21)) or 0
        # En el Excel legacy cada fila representa UN componente de pago de una
        # transacción (col 15 = tipo: 'Capital', 'Interes Mora', etc.; col 17 =
        # valor de ese componente específico). Cuando tipo es Interes Mora,
        # col 17 ES exclusivamente el valor del interés mora pagado.
        tipo = (_str_or_none(get(15)) or '').lower()
        if 'mora' in tipo:
            entry['interes_mora_total'] += _to_float(get(17)) or 0
        fp = _to_date(get(22))
        if fp:
            if not entry['fecha_primer_pago'] or fp < entry['fecha_primer_pago']:
                entry['fecha_primer_pago'] = fp
            if not entry['fecha_ultimo_pago'] or fp > entry['fecha_ultimo_pago']:
                entry['fecha_ultimo_pago'] = fp
    # cifrar PII al final
    for e in agg.values():
        if e['identificacion']:
            e['identificacion'] = encrypt(e['identificacion'])
        if e['nombre']:
            e['nombre'] = encrypt(e['nombre'])
    return list(agg.values())


@recaudo.get("/uploads-status")
def recaudo_status(_user=Depends(require_auth)):
    """Devuelve estado de los últimos uploads (Recaudo nuevo + legacy)."""
    return _upload_status_for(['recaudo_upload', 'recaudo_legacy_upload'])


@recaudo.post("/upload-legacy")
async def recaudo_upload_legacy(request: Request, user=Depends(require_superadmin), file: UploadFile = File(...)):
    """Carga el archivo histórico de pagos de la plataforma vieja.
    Agrupa por id_prestamo y reemplaza la tabla pagos_legacy completa."""
    async with upload_session(
        request, user, file, source="recaudo_legacy_upload",
        max_mb=MAX_LEGACY_MB,
        generic_error_msg="No se pudo importar el archivo histórico. Revisa el formato y vuelve a intentarlo."
    ) as ctx:
        rows = ctx.read_excel()
        if len(rows) > MAX_LEGACY_ROWS:
            raise ValueError(f"Excede {MAX_LEGACY_ROWS} filas")
        records = _aggregate_pagos_legacy(rows)
        cols = ['id_prestamo','id_solicitud','identificacion','nombre','num_pagos',
                'valor_pago_total','iva_pagado_total','cargos_netos_total','interes_mora_total',
                'fecha_primer_pago','fecha_ultimo_pago','prestamo_cancelado','metodo_pago_principal']
        with get_db() as conn:
            conn.execute("DELETE FROM pagos_legacy")
            placeholders = ', '.join(['?'] * (len(cols) + 1))
            cols_sql = ', '.join(cols + ['sync_batch_id'])
            for rec in records:
                conn.execute(f"INSERT INTO pagos_legacy ({cols_sql}) VALUES ({placeholders})",
                             [rec.get(c) for c in cols] + [ctx.sync_id])
        ctx.set_counts(fetched=len(rows), inserted=len(records))
        return {"status": "success", "prestamos": len(records), "filas_origen": len(rows) - 1}


@recaudo.get("/legacy")
def recaudo_legacy_list(_user=Depends(require_auth)):
    """Devuelve los pagos legacy agregados por id_prestamo.

    Acceso: cualquier rol autenticado. PII (identificación, nombre) viene
    enmascarada con mask_identificacion/mask_cliente. No existe endpoint
    de reveal para datos legacy, por lo cual no hay riesgo de fuga PII
    completo desde este endpoint.
    """
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM pagos_legacy ORDER BY valor_pago_total DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d['identificacion'] = mask_identificacion(decrypt(d.get('identificacion')))
            d['nombre'] = mask_cliente(decrypt(d.get('nombre')))
            out.append(d)
        return out
    finally:
        conn.close()


@recaudo.get("/legacy-summary")
def recaudo_legacy_summary(_user=Depends(require_auth)):
    """Totales del histórico."""
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT COUNT(*) as prestamos,
                COALESCE(SUM(num_pagos),0) as pagos_total,
                COALESCE(SUM(valor_pago_total),0) as valor_total,
                COALESCE(SUM(iva_pagado_total),0) as iva_total,
                COALESCE(SUM(cargos_netos_total),0) as cargos_total,
                COALESCE(SUM(interes_mora_total),0) as mora_total
            FROM pagos_legacy
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
    async with upload_session(request, user, file, source="solicitudes_upload",
                              max_mb=MAX_UPLOAD_MB) as ctx:
        rows = ctx.read_excel()
        if len(rows) > MAX_ROWS:
            raise ValueError(f"Excede {MAX_ROWS} filas")
        records = [r for r in (_transform_solic_row(row) for row in rows[1:]) if r]
        cols = list(records[0].keys()) if records else []
        with get_db() as conn:
            conn.execute("DELETE FROM solicitudes WHERE sync_batch_id IN (SELECT id FROM sync_logs WHERE source='solicitudes_upload' AND id < ?)", (ctx.sync_id,))
            placeholders = ', '.join(['?'] * (len(cols) + 1))
            cols_sql = ', '.join(cols + ['sync_batch_id'])
            for rec in records:
                conn.execute(f"INSERT INTO solicitudes ({cols_sql}) VALUES ({placeholders})",
                             [rec.get(c) for c in cols] + [ctx.sync_id])
        ctx.set_counts(fetched=len(rows), inserted=len(records))
        return {"status": "success", "records": len(records)}


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


# ---------- HISTÓRICO de Solicitudes (plataforma vieja, 40 cols) ----------
SOLIC_LEGACY_COLS = {
    0: ('id_solicitud', _str_or_none),
    1: ('id_entidad', _str_or_none),
    2: ('fecha_solicitud', _to_date),
    3: ('tipo_identificacion', _str_or_none),
    4: ('identificacion', None),       # cifrado
    6: ('nombre_completo', None),      # cifrado
    7: ('originador', _str_or_none),
    8: ('producto', _str_or_none),
    10: ('estado', _str_or_none),
    11: ('estado_precalif', _str_or_none),
    12: ('fecha_desembolso', _to_date),
    13: ('monto', _to_float),
    14: ('plazo_dias', _to_float),
    15: ('numero_cuotas', _to_float),
    16: ('frecuencia_pagos', _str_or_none),
    17: ('fecha_inicio_pagos', _to_date),
    18: ('tasa_interes', _to_float),
    19: ('canal', _str_or_none),
    20: ('genero', _str_or_none),
    21: ('edad', _to_float),
    24: ('departamento', _str_or_none),
    25: ('ciudad', _str_or_none),
    27: ('nombre_banco', _str_or_none),
    36: ('tipo_solicitud', _str_or_none),
    37: ('asesor_comercial', _str_or_none),
    38: ('decision_modelo', _str_or_none),
    39: ('cliente_recurrente', _str_or_none),
}


def _transform_solic_legacy_row(row: tuple) -> dict | None:
    rec = {}
    for idx, (key, parser) in SOLIC_LEGACY_COLS.items():
        val = row[idx] if idx < len(row) else None
        if key in ('identificacion', 'nombre_completo'):
            v = _str_or_none(val)
            rec[key] = encrypt(v) if v else None
        else:
            rec[key] = parser(val) if parser else val
    if not rec.get('id_solicitud') and not rec.get('estado'):
        return None
    return rec


@solicitudes.get("/uploads-status")
def solic_status(_user=Depends(require_auth)):
    return _upload_status_for(['solicitudes_upload', 'solicitudes_legacy_upload'])


@solicitudes.post("/upload-legacy")
async def solic_upload_legacy(request: Request, user=Depends(require_superadmin), file: UploadFile = File(...)):
    """Carga el archivo histórico de solicitudes de la plataforma vieja."""
    async with upload_session(
        request, user, file, source="solicitudes_legacy_upload",
        max_mb=MAX_LEGACY_MB,
        generic_error_msg="No se pudo importar el archivo histórico. Revisa el formato y vuelve a intentarlo."
    ) as ctx:
        rows = ctx.read_excel()
        if len(rows) > MAX_LEGACY_ROWS:
            raise ValueError(f"Excede {MAX_LEGACY_ROWS} filas")
        records = [r for r in (_transform_solic_legacy_row(row) for row in rows[1:]) if r]
        cols = list(records[0].keys()) if records else []
        with get_db() as conn:
            conn.execute("DELETE FROM solicitudes_legacy")
            placeholders = ', '.join(['?'] * (len(cols) + 1))
            cols_sql = ', '.join(cols + ['sync_batch_id'])
            for rec in records:
                conn.execute(f"INSERT INTO solicitudes_legacy ({cols_sql}) VALUES ({placeholders})",
                             [rec.get(c) for c in cols] + [ctx.sync_id])
        ctx.set_counts(fetched=len(rows), inserted=len(records))
        return {"status": "success", "records": len(records)}


@solicitudes.get("/combined")
def solic_combined(_user=Depends(require_auth)):
    """UNION de legacy + nueva plataforma. Cada solicitud es única."""
    conn = get_connection()
    try:
        # Nueva plataforma
        new_rows = conn.execute(
            "SELECT * FROM solicitudes WHERE sync_batch_id = (SELECT id FROM sync_logs WHERE source='solicitudes_upload' AND status='success' ORDER BY id DESC LIMIT 1)"
        ).fetchall()
        # Legacy
        legacy_rows = conn.execute("SELECT * FROM solicitudes_legacy").fetchall()

        out = []
        for r in new_rows:
            d = dict(r)
            d['identificacion'] = mask_identificacion(decrypt(d.get('identificacion')))
            d['solicitante'] = mask_cliente(decrypt(d.get('solicitante')))
            d['source'] = 'nueva'
            out.append(d)
        for r in legacy_rows:
            d = dict(r)
            out.append({
                'source': 'legacy',
                'solicitud': d.get('id_solicitud'),
                'linea': d.get('producto'),
                'identificacion': mask_identificacion(decrypt(d.get('identificacion'))),
                'solicitante': mask_cliente(decrypt(d.get('nombre_completo'))),
                'valor': d.get('monto'),
                'estado': d.get('estado'),
                'paso_ruta': d.get('estado_precalif'),
                'oficina': d.get('canal'),
                'fecha_solicitud': d.get('fecha_solicitud'),
                'producto': d.get('producto'),
                'tasa_interes': d.get('tasa_interes'),
                'plazo_dias': d.get('plazo_dias'),
                'numero_cuotas': d.get('numero_cuotas'),
                'fecha_desembolso': d.get('fecha_desembolso'),
                'canal': d.get('canal'),
                'asesor_comercial': d.get('asesor_comercial'),
            })
        return out
    finally:
        conn.close()


@solicitudes.get("/combined-summary")
def solic_combined_summary(_user=Depends(require_auth)):
    """Totales unión legacy + nueva."""
    conn = get_connection()
    try:
        batch = "(SELECT id FROM sync_logs WHERE source='solicitudes_upload' AND status='success' ORDER BY id DESC LIMIT 1)"
        new_row = conn.execute(f"""
            SELECT COUNT(*) as total, COALESCE(SUM(valor),0) as valor_total,
                SUM(CASE WHEN estado='DESEMBOLSADA' THEN 1 ELSE 0 END) as desembolsadas,
                SUM(CASE WHEN estado='DESEMBOLSADA' THEN COALESCE(valor,0) ELSE 0 END) as valor_desembolsado,
                SUM(CASE WHEN estado<>'DESEMBOLSADA' AND estado IS NOT NULL THEN 1 ELSE 0 END) as pipeline,
                SUM(CASE WHEN estado<>'DESEMBOLSADA' AND estado IS NOT NULL THEN COALESCE(valor,0) ELSE 0 END) as valor_pipeline
            FROM solicitudes WHERE sync_batch_id = {batch}
        """).fetchone()
        leg_row = conn.execute("""
            SELECT COUNT(*) as total, COALESCE(SUM(monto),0) as valor_total,
                SUM(CASE WHEN UPPER(COALESCE(estado,''))='APROBADA' THEN 1 ELSE 0 END) as desembolsadas,
                SUM(CASE WHEN UPPER(COALESCE(estado,''))='APROBADA' THEN COALESCE(monto,0) ELSE 0 END) as valor_desembolsado
            FROM solicitudes_legacy
        """).fetchone()
        new_d = dict(new_row) if new_row else {}
        leg_d = dict(leg_row) if leg_row else {}
        return {
            'total': (new_d.get('total') or 0) + (leg_d.get('total') or 0),
            'valor_total': (new_d.get('valor_total') or 0) + (leg_d.get('valor_total') or 0),
            'desembolsadas': (new_d.get('desembolsadas') or 0) + (leg_d.get('desembolsadas') or 0),
            'valor_desembolsado': (new_d.get('valor_desembolsado') or 0) + (leg_d.get('valor_desembolsado') or 0),
            'pipeline': new_d.get('pipeline') or 0,
            'valor_pipeline': new_d.get('valor_pipeline') or 0,
            'legacy_total': leg_d.get('total') or 0,
            'nueva_total': new_d.get('total') or 0,
        }
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
    async with upload_session(request, user, file, source="juridico_upload",
                              max_mb=MAX_UPLOAD_MB) as ctx:
        rows = ctx.read_excel()
        records = [r for r in (_transform_juridico_row(row) for row in rows[2:]) if r]
        cols = list(records[0].keys()) if records else []
        with get_db() as conn:
            conn.execute("DELETE FROM procesos_juridicos WHERE sync_batch_id IN (SELECT id FROM sync_logs WHERE source='juridico_upload' AND id < ?)", (ctx.sync_id,))
            placeholders = ', '.join(['?'] * (len(cols) + 1))
            cols_sql = ', '.join(cols + ['sync_batch_id'])
            for rec in records:
                conn.execute(f"INSERT INTO procesos_juridicos ({cols_sql}) VALUES ({placeholders})",
                             [rec.get(c) for c in cols] + [ctx.sync_id])
        ctx.set_counts(fetched=len(rows), inserted=len(records))
        return {"status": "success", "records": len(records)}


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

        def _norm_id(v):
            """Normaliza identificación para hacer match robusto.
            Strip whitespace, remove leading zeros, remove non-digit chars (.,-)."""
            if not v:
                return None
            s = str(v).strip()
            # Quitar caracteres no numéricos comunes (puntos de miles, guiones)
            s = re.sub(r'[\s.\-,]', '', s)
            # Quitar ceros iniciales (preserva "0" como caso especial)
            s = s.lstrip('0') or s
            return s if s else None

        cartera_idx = {}
        for c in credit_rows:
            ident_raw = decrypt(c["identificacion"])
            ident = _norm_id(ident_raw)
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
            # Cruce con identificación normalizada (lstrip ceros, sin guiones/puntos)
            cartera = cartera_idx.get(_norm_id(d.get('identificacion')))
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
