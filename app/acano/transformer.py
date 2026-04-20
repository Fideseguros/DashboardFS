"""Transform raw Excel rows into our internal schema (with PII encryption)."""
from datetime import datetime
from app.crypto import encrypt
from .config import EXCEL_COL_MAP


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


def transform_excel_row(row: tuple) -> dict:
    record = {}
    for col_idx, our_key in EXCEL_COL_MAP.items():
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


def transform_excel_batch(rows: list[tuple]) -> list[dict]:
    return [transform_excel_row(r) for r in rows]
