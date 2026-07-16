"""Transform raw Excel rows into our internal schema (with PII encryption)."""
import logging
import unicodedata
from datetime import datetime
from app.crypto import encrypt
from .config import CARTERA_COLUMNS

_log = logging.getLogger("fide.transformer")


DATE_FIELDS = {
    'fecha_inicio', 'fecha_vencimiento', 'fecha_ult_pago',
    'fecha_desembolso'
}

NUMERIC_FIELDS = {
    'valor_credito', 'saldo_capital', 'saldo_favor', 'valor_cuota',
    'tasa_efectiva', 'plazo', 'cuotas_pactadas', 'cuotas_pagadas',
    'dias_mora', 'maxima_mora'
}

# Fields stored encrypted (PII under Habeas Data).
PII_FIELDS = {'identificacion', 'cliente'}


def _normalize_date(val):
    if val is None:
        return None
    if hasattr(val, 'strftime'):
        return val.strftime('%Y-%m-%d')
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return None
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(val, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return str(val) if val else None


def _normalize_number(val):
    """Convert numeric strings (including '24.00 %') to float. Returns None if not parseable."""
    if val is None or val == '':
        return None
    if isinstance(val, (int, float)):
        return val
    if isinstance(val, str):
        cleaned = val.strip().replace('%', '').replace(',', '').strip()
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return val


def _norm_header(s):
    """minúsculas, sin tildes, '_'/espacios colapsados a un solo espacio."""
    s = str(s or '').strip().lower().replace('_', ' ')
    s = ' '.join(s.split())
    return ''.join(c for c in unicodedata.normalize('NFD', s)
                   if unicodedata.category(c) != 'Mn')


def resolve_column_map(header_row) -> dict:
    """Devuelve {campo_interno: índice} ubicando cada columna por su NOMBRE.

    Inmune a inserción/reordenamiento de columnas. Lanza ValueError si falta
    un campo CRÍTICO (el archivo no es una cartera válida). Los campos
    opcionales ausentes se registran en el log y quedan sin mapear (None), sin
    tumbar el upload por una columna secundaria renombrada.
    """
    norm = [_norm_header(h) for h in header_row]
    norm_index = {}
    for i, h in enumerate(norm):
        norm_index.setdefault(h, i)  # primera aparición gana

    colmap = {}
    faltan_criticos = []
    faltan_opcionales = []
    for campo, (nombres, critico) in CARTERA_COLUMNS.items():
        idx = next((norm_index[n] for n in nombres if n in norm_index), None)
        if idx is not None:
            colmap[campo] = idx
        elif critico:
            faltan_criticos.append(f"{campo} (esperaba «{nombres[0]}»)")
        else:
            faltan_opcionales.append(campo)

    if faltan_criticos:
        # No caemos a índices fijos: un mapeo silencioso equivocado es
        # justamente el bug que esta migración elimina. Mejor error claro.
        # (En el flujo normal check_file_signature ya validó estas columnas
        # antes de llegar aquí, así que esto casi nunca se dispara.)
        raise ValueError(
            "El archivo de cartera no tiene las columnas obligatorias: "
            + ", ".join(faltan_criticos)
            + ". Verifica que sea el Informe de Cartera correcto."
        )
    if faltan_opcionales:
        _log.info("Cartera: columnas opcionales no encontradas (quedan vacías): %s",
                  ", ".join(faltan_opcionales))
    return colmap


def transform_excel_row(row: tuple, colmap: dict) -> dict:
    record = {}
    for our_key, col_idx in colmap.items():
        val = row[col_idx] if col_idx < len(row) else None
        if our_key in DATE_FIELDS:
            val = _normalize_date(val)
        elif our_key in NUMERIC_FIELDS:
            val = _normalize_number(val)
        elif val is not None:
            val = str(val).strip() if not isinstance(val, str) else val.strip()
        if our_key in PII_FIELDS and val:
            val = encrypt(val)
        record[our_key] = val
    return record


def transform_excel_batch(rows: list[tuple], header_row) -> list[dict]:
    """rows = filas de datos (sin encabezado). header_row = la fila de títulos,
    usada para resolver los índices por nombre una sola vez."""
    colmap = resolve_column_map(header_row)
    return [transform_excel_row(r, colmap) for r in rows]
