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

# Mapeo de campos de Recaudo POR NOMBRE de columna (no por índice fijo).
# La plataforma inserta/reordena columnas cada cierto tiempo (24-jun añadió
# Ciclo Cobro, Fecha Cuota, Cuota, Gastos Aplicacion Cloud, etc.) — antes el
# mapeo por índice fijo leía columnas equivocadas y producía valores negativos
# (ej. 'total' leía 'Deudores Varios'). Ubicar cada campo por su título lo
# hace inmune a esos cambios.
#
# Cada spec: (campo_bd, parser, modo, frase_norm, requerido)
#   modo 'exact'  → header normalizado == frase
#   modo 'contains' → header normalizado contiene frase
def _norm_col(s):
    """Normaliza header: minúsculas, sin tildes, sin puntos/guiones, espacios
    colapsados. 'I.v.a' → 'iva'; ' Interes De Mora' → 'interes de mora'."""
    import unicodedata
    s = str(s or '').strip().lower()
    s = ''.join(c for c in unicodedata.normalize('NFD', s)
                if unicodedata.category(c) != 'Mn')
    s = s.replace('.', '')              # puntos se ELIMINAN ('i.v.a' → 'iva')
    s = s.replace('-', ' ')             # guiones → espacio (separan palabras)
    return ' '.join(s.split())          # colapsa espacios

PAGOS_FIELD_SPECS = [
    ('entidad',          _str_or_none, 'exact',    'entidad',            False),
    ('linea_credito',    _str_or_none, 'contains', 'linea credito',      False),
    ('fecha_movimiento', _to_date,     'contains', 'fecha movimiento',   False),
    ('fecha_documento',  _to_date,     'contains', 'fecha documento',    False),
    ('identificacion',   None,         'exact',    'identificacion',     True),
    ('cliente',          None,         'exact',    'cliente',            False),
    ('cuenta',           _str_or_none, 'exact',    'cuenta',             False),
    ('solicitud',        _str_or_none, 'exact',    'solicitud',          False),
    ('aliado',           _str_or_none, 'contains', 'aliado',             False),
    ('tipo_mvto',        _str_or_none, 'contains', 'tipo mvto',          False),
    ('tipo_documento',   _str_or_none, 'contains', 'tipo documento',     False),
    ('documento',        _str_or_none, 'exact',    'documento',          False),
    ('usuario',          _str_or_none, 'exact',    'usuario',            False),
    ('capital',          _to_float,    'contains', 'capital prestamos',  True),
    ('interes_corriente',_to_float,    'contains', 'interes corriente',  False),
    ('interes_mora',     _to_float,    'contains', 'interes de mora',    False),
    ('iva',              _to_float,    'exact',    'iva',                False),
    ('saldo_favor',      _to_float,    'contains', 'saldo favor',        False),
    ('gastos_pj',        _to_float,    'contains', 'gastos prejuridico', False),
    ('cargos_admin',     _to_float,    'contains', 'cargos administrativos', False),
    ('total',            _to_float,    'exact',    'total',              True),
    ('autorizacion',     _str_or_none, 'contains', 'autorizacion',       False),
    ('observaciones',    _str_or_none, 'contains', 'observaciones',      False),
]


def _resolve_pago_columns(header_row):
    """Mapea cada campo de Recaudo a su índice de columna por nombre.
    Lanza ValueError si falta una columna REQUERIDA (archivo equivocado)."""
    norm = [_norm_col(h) for h in header_row]
    # Firma del archivo de movimientos/recaudo: distingue de otros reportes
    # que comparten columnas de montos (ej. el Resumen Estado Cuenta también
    # tiene Capital y Total). Si no tiene firma de movimientos, es otro archivo.
    firma_recaudo = ['fecha movimiento', 'tipo mvto', 'entidad']
    if not any(any(f in h for h in norm) for f in firma_recaudo):
        raise ValueError(
            "Este archivo no parece ser el reporte de Recaudo/Movimientos "
            "(no encontré columnas como «Fecha Movimiento» o «Tipo Mvto»). "
            "Verifica que estés subiendo el archivo correcto en este botón."
        )
    mapping = {}   # campo_bd -> (idx, parser)
    faltan = []
    for campo, parser, modo, frase, requerido in PAGOS_FIELD_SPECS:
        idx = None
        for i, h in enumerate(norm):
            if (modo == 'exact' and h == frase) or (modo == 'contains' and frase in h):
                idx = i
                break
        if idx is None:
            if requerido:
                faltan.append(f"«{frase}»")
        else:
            mapping[campo] = (idx, parser)
    if faltan:
        raise ValueError(
            "Este archivo no parece ser el reporte de Recaudo/Movimientos. "
            "Verifica que estés subiendo el archivo correcto en este botón. "
            "No encontré las columnas: " + ", ".join(faltan)
        )
    return mapping


def _transform_pago_row(row: tuple, col_map: dict) -> dict | None:
    """Transforma una fila usando el mapeo {campo: (idx, parser)} resuelto
    por nombre desde el header."""
    rec = {}
    for campo, (idx, parser) in col_map.items():
        val = row[idx] if idx < len(row) else None
        if campo in ('identificacion', 'cliente'):
            v = _str_or_none(val); rec[campo] = encrypt(v) if v else None
        else:
            rec[campo] = parser(val) if parser else val
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
        if not rows or len(rows) < 2:
            raise ValueError("Archivo vacío o sin filas de datos")
        # Ubicar columnas por NOMBRE (robusto a cambios de formato de la plataforma)
        col_map = _resolve_pago_columns(rows[0])
        records = [r for r in (_transform_pago_row(row, col_map) for row in rows[1:]) if r]

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
def recaudo_list(user=Depends(require_auth)):
    """Para superadmin: identificación + cliente en plaintext (buscar por
    cédula). Para otros roles: enmascarado."""
    is_super = user.get("role") == "superadmin"
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM pagos WHERE sync_batch_id = (SELECT id FROM sync_logs WHERE source='recaudo_upload' AND status='success' ORDER BY id DESC LIMIT 1)"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            ident = decrypt(d.get('identificacion')) or ""
            cli = decrypt(d.get('cliente')) or ""
            if is_super:
                d['identificacion'] = ident
                d['cliente'] = cli
            else:
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
                COALESCE(SUM(interes_mora),0) as interes_mora
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
def recaudo_legacy_list(user=Depends(require_auth)):
    """Devuelve los pagos legacy agregados por id_prestamo.

    Para superadmin: identificación + nombre en plaintext (gerente necesita
    buscar por cédula). Otros roles: enmascarado.
    """
    is_super = user.get("role") == "superadmin"
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM pagos_legacy ORDER BY valor_pago_total DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            ident = decrypt(d.get('identificacion')) or ""
            nombre = decrypt(d.get('nombre')) or ""
            if is_super:
                d['identificacion'] = ident
                d['nombre'] = nombre
            else:
                d['identificacion'] = mask_identificacion(ident)
                d['nombre'] = mask_cliente(nombre)
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
def solic_list(user=Depends(require_auth)):
    is_super = user.get("role") == "superadmin"
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM solicitudes WHERE sync_batch_id = (SELECT id FROM sync_logs WHERE source='solicitudes_upload' AND status='success' ORDER BY id DESC LIMIT 1)"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            ident = decrypt(d.get('identificacion')) or ""
            sol = decrypt(d.get('solicitante')) or ""
            if is_super:
                d['identificacion'] = ident
                d['solicitante'] = sol
            else:
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


# Normalización de estados de solicitudes solicitada por la líder de cartera:
#  Nueva plataforma:
#    DEVUELTA ATENDIDA / DEVUELTA HOJA RUTA → EN ESTUDIO
#  Legacy:
#    Aprobada → DESEMBOLSADA
#    Borrada → ANULADA
#    Pendiente → se excluye (préstamos pre-plataforma-anterior, sin información)
#    Iniciada / En Estudio → se conservan como están (caso por caso)
SOLIC_NUEVA_REMAP = {
    'DEVUELTA ATENDIDA': 'EN ESTUDIO',
    'DEVUELTA HOJA RUTA': 'EN ESTUDIO',
    # 'TRATAMIENTO DE DATOS ACEPTADO' es el estado intermedio del pipeline
    # nuevo: la solicitud aceptó Habeas Data y está en estudio crediticio
    # (paso ruta = Decisión Solicitud, Centrales de Riesgo, Cargar documentos).
    # Sin este mapeo, ~15% de las solicitudes del archivo quedaban con un
    # nombre técnico que la gerente no reconocía como "En estudio".
    'TRATAMIENTO DE DATOS ACEPTADO': 'EN ESTUDIO',
    # Pedido gerencia: en la plataforma nueva, NEGADA (y demás formas de
    # rechazo/cancelación) cuentan como ANULADA — igual que en legacy, para
    # que el estado sea consistente entre ambas plataformas.
    'NEGADA':    'ANULADA',
    'RECHAZADA': 'ANULADA',
    'DESISTIDA': 'ANULADA',
    'BORRADA':   'ANULADA',
}
SOLIC_LEGACY_REMAP = {
    'APROBADA':   'DESEMBOLSADA',
    'BORRADA':    'ANULADA',
    'NEGADA':     'ANULADA',
    'DESISTIDA':  'ANULADA',
    # Iter 2 (pedido líder): las que en legacy quedaron como "Iniciada" o
    # "En Estudio" pasan también a ANULADA — no se considera pipeline activo
    # en la plataforma nueva (la plataforma vieja se cerró).
    # El 'EN ESTUDIO' del archivo NUEVO (que viene de DEVUELTA ATENDIDA /
    # DEVUELTA HOJA RUTA) se conserva como EN ESTUDIO porque SOLIC_NUEVA_REMAP
    # se aplica antes que esto — este map solo afecta source='legacy'.
    'INICIADA':   'ANULADA',
    'EN ESTUDIO': 'ANULADA',
}
SOLIC_LEGACY_EXCLUDE = {'PENDIENTE'}


def _normalize_estado(estado, source: str) -> str | None:
    """Normaliza el estado según la plataforma. Devuelve None si se debe excluir la fila.

    Normalización defensiva (gerente reportó que TRATAMIENTO DE DATOS
    ACEPTADO no se mapeaba): además de strip+upper, colapsamos espacios
    múltiples y quitamos tildes. Si el match exacto falla, aplicamos
    fuzzy contains para los estados intermedios del pipeline nuevo.
    """
    import unicodedata
    if not estado:
        return estado
    key = str(estado).strip().upper()
    key = ' '.join(key.split())  # colapsa espacios múltiples
    # Sin tildes para tolerar variantes de capturación del Excel
    key = ''.join(c for c in unicodedata.normalize('NFD', key)
                  if unicodedata.category(c) != 'Mn')
    if source == 'legacy':
        if key in SOLIC_LEGACY_EXCLUDE:
            return None
        return SOLIC_LEGACY_REMAP.get(key, key)
    # Nueva plataforma — match exacto primero
    if key in SOLIC_NUEVA_REMAP:
        return SOLIC_NUEVA_REMAP[key]
    # Rechazo / cancelación → ANULADA (consistente con legacy). Se evalúa
    # antes que EN ESTUDIO porque son estados finales.
    if 'NEGAD' in key or 'RECHAZ' in key or 'DESIST' in key or 'BORRAD' in key:
        return 'ANULADA'
    # Fuzzy fallback: cualquier estado intermedio del pipeline cuenta como
    # EN ESTUDIO.
    if 'TRATAMIENTO' in key and 'DATOS' in key:
        return 'EN ESTUDIO'
    if 'DEVUELTA' in key:
        return 'EN ESTUDIO'
    if 'ESTUDIO' in key or 'PROCESO' in key or 'ANALISIS' in key:
        return 'EN ESTUDIO'
    if 'PENDIENTE' in key or 'CENTRAL' in key:  # centrales de riesgo
        return 'EN ESTUDIO'
    return key


@solicitudes.get("/combined")
def solic_combined(user=Depends(require_auth)):
    """UNION de legacy + nueva plataforma con estados normalizados.

    Para superadmin: identificación + nombre en plaintext (gerente requiere
    buscar por cédula). Otros roles: enmascarado.
    """
    is_super = user.get("role") == "superadmin"
    def _id(plain):
        return plain if is_super else mask_identificacion(plain)
    def _nom(plain):
        return plain if is_super else mask_cliente(plain)
    conn = get_connection()
    try:
        new_rows = conn.execute(
            "SELECT * FROM solicitudes WHERE sync_batch_id = (SELECT id FROM sync_logs WHERE source='solicitudes_upload' AND status='success' ORDER BY id DESC LIMIT 1)"
        ).fetchall()
        legacy_rows = conn.execute("SELECT * FROM solicitudes_legacy").fetchall()

        out = []
        for r in new_rows:
            d = dict(r)
            normalized = _normalize_estado(d.get('estado'), 'nueva')
            if normalized is None:
                continue
            d['estado'] = normalized
            d['identificacion'] = _id(decrypt(d.get('identificacion')) or "")
            d['solicitante'] = _nom(decrypt(d.get('solicitante')) or "")
            d['source'] = 'nueva'
            out.append(d)
        for r in legacy_rows:
            d = dict(r)
            normalized = _normalize_estado(d.get('estado'), 'legacy')
            if normalized is None:
                # 'Pendiente' legacy → se excluye del análisis
                continue
            out.append({
                'source': 'legacy',
                'solicitud': d.get('id_solicitud'),
                'linea': d.get('producto'),
                'identificacion': _id(decrypt(d.get('identificacion')) or ""),
                'solicitante': _nom(decrypt(d.get('nombre_completo')) or ""),
                'valor': d.get('monto'),
                'estado': normalized,
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


@solicitudes.get("/diagnose")
def solic_diagnose(_user=Depends(require_auth)):
    """Diagnóstico: muestra qué estados crudos hay en la BD y cómo los
    normaliza _normalize_estado. Permite verificar desde el navegador
    (sin DevTools) si el mapeo funciona después de un upload.

    Abrir: https://<dashboard>/api/solicitudes/diagnose
    """
    from collections import Counter
    conn = get_connection()
    try:
        # Plataforma nueva: último batch exitoso
        new_rows = conn.execute(
            "SELECT estado FROM solicitudes WHERE sync_batch_id = "
            "(SELECT id FROM sync_logs WHERE source='solicitudes_upload' "
            "AND status='success' ORDER BY id DESC LIMIT 1)"
        ).fetchall()
        # Legacy: todo
        legacy_rows = conn.execute("SELECT estado FROM solicitudes_legacy").fetchall()

        def _audit_estados(rows, source):
            raw = Counter()
            normalized = Counter()
            mapping_trace = {}  # raw → set of normalized
            for r in rows:
                e = r['estado']
                raw[e or '(vacío)'] += 1
                n = _normalize_estado(e, source)
                normalized[n or '(excluido)'] += 1
                if e:
                    mapping_trace.setdefault(e, set()).add(n or '(excluido)')
            return {
                'total': len(rows),
                'crudos': dict(raw.most_common()),
                'normalizados': dict(normalized.most_common()),
                'mapeo': {k: list(v) for k, v in mapping_trace.items()},
            }

        return {
            'nueva_plataforma': _audit_estados(new_rows, 'nueva'),
            'legacy': _audit_estados(legacy_rows, 'legacy'),
            'nota': (
                "Si 'crudos' tiene un estado que NO aparece como 'EN ESTUDIO' "
                "en 'normalizados', agregar al mapeo en SOLIC_NUEVA_REMAP."
            ),
        }
    finally:
        conn.close()


@solicitudes.get("/combined-summary")
def solic_combined_summary(_user=Depends(require_auth)):
    """Totales unión legacy + nueva, con estados normalizados.

    Nueva: DESEMBOLSADA cuenta como tal.
    Legacy: APROBADA cuenta como desembolsada (mapeo solicitado por líder).
    Legacy: PENDIENTE se EXCLUYE de todos los conteos (pre-plataforma anterior).
    """
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
            WHERE UPPER(COALESCE(estado,'')) <> 'PENDIENTE'
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
            # IMPORTANTE: separamos saldo total vs saldo activo y vs saldo activo
            # en mora — la auditoría detectó que jur_cartera_summary mezclaba
            # saldo de cancelados en la cifra "saldo en mora".
            agg = cartera_idx.setdefault(ident, {
                "creditos_total": 0,
                "creditos_activos": 0,
                "saldo_capital_total": 0.0,     # incluye cancelados (información)
                "saldo_capital_activo": 0.0,    # solo ACTIVO — base para los KPIs
                "saldo_activo_en_mora_30": 0.0, # solo ACTIVO + mora>30 (NPL parcial)
                "valor_credito_total": 0.0,
                "max_dias_mora": 0,
                "estado_principal": None,
                "lineas": set(),
                "calificaciones": set(),
                "aliado_principal": None,
            })
            agg["creditos_total"] += 1
            saldo = float(c["saldo_capital"] or 0)
            agg["saldo_capital_total"] += saldo
            agg["valor_credito_total"] += float(c["valor_credito"] or 0)
            dm = int(c["dias_mora"] or 0)
            if c["estado"] == "ACTIVO":
                agg["creditos_activos"] += 1
                agg["saldo_capital_activo"] += saldo
                if dm > 30:
                    agg["saldo_activo_en_mora_30"] += saldo
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
        # El endpoint ya requiere superadmin (require_superadmin), así que
        # devolvemos plaintext directo. La gerente lo necesita para buscar
        # por cédula en cobro jurídico.
        for r in rows:
            d = dict(r)
            ident_plain = decrypt(d.get('identificacion')) or ""
            nombre_plain = decrypt(d.get('nombre')) or ""
            d['identificacion'] = ident_plain
            d['nombre'] = nombre_plain
            # Cruce con identificación normalizada (lstrip ceros, sin guiones/puntos)
            cartera = cartera_idx.get(_norm_id(ident_plain))
            if cartera:
                d['cartera_creditos_total'] = cartera['creditos_total']
                d['cartera_creditos_activos'] = cartera['creditos_activos']
                d['cartera_saldo_capital'] = cartera['saldo_capital_total']
                d['cartera_saldo_activo'] = cartera['saldo_capital_activo']
                d['cartera_saldo_activo_mora30'] = cartera['saldo_activo_en_mora_30']
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
    """Resumen del cruce procesos jurídicos vs cartera.

    Política tras la auditoría (issue #7 y #8):
      - 'saldo_total_cartera' = solo créditos ACTIVO de los clientes con
        proceso jurídico. Antes incluía cancelados (saldo típicamente 0
        pero conceptualmente incorrecto).
      - 'saldo_en_mora' = solo el saldo del crédito ACTIVO con mora > 30,
        no el saldo total del cliente. Antes sumaba todos los créditos del
        cliente si CUALQUIERA de ellos tenía mora > 30.
    """
    rows = jur_list(user)
    if not rows:
        return {"total": 0, "matched": 0, "saldo_total_cartera": 0, "saldo_en_mora": 0}
    matched = [r for r in rows if r.get('cartera_match')]
    return {
        "total": len(rows),
        "matched": len(matched),
        "saldo_total_cartera": sum(r.get('cartera_saldo_activo') or 0 for r in matched),
        "saldo_en_mora": sum(r.get('cartera_saldo_activo_mora30') or 0 for r in matched),
    }


@juridico.get("/summary")
def jur_summary(user=Depends(require_superadmin)):
    conn = get_connection()
    try:
        batch = "(SELECT id FROM sync_logs WHERE source='juridico_upload' AND status='success' ORDER BY id DESC LIMIT 1)"
        # Filtro 'sin medida cautelar' ampliado tras auditoría: antes solo
        # excluía 'N/A', ahora también las variantes típicas que aparecen
        # en archivos Excel (NA, No, NO, -, n/a, sin, ninguna, etc.).
        row = conn.execute(f"""
            SELECT COUNT(*) as total,
                SUM(CASE WHEN LOWER(probabilidad) LIKE '%probable%' THEN 1 ELSE 0 END) as probables,
                SUM(CASE WHEN LOWER(probabilidad) LIKE '%remot%' THEN 1 ELSE 0 END) as remotas,
                COUNT(DISTINCT juzgado) as juzgados,
                SUM(CASE WHEN medida_cautelar IS NOT NULL
                          AND TRIM(medida_cautelar) <> ''
                          AND LOWER(TRIM(medida_cautelar)) NOT IN
                              ('n/a', 'na', 'no', '-', 'sin', 'ninguna', 'ninguno', 'sin medida', 'n.a.')
                     THEN 1 ELSE 0 END) as con_medida
            FROM procesos_juridicos WHERE sync_batch_id = {batch}
        """).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()
