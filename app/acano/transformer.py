"""Transform raw ACANO records into our internal schema."""
from datetime import datetime
from .config import FIELD_MAP, EXCEL_COL_MAP


def _normalize_date(val):
    """Convert various date formats to ISO 8601 (YYYY-MM-DD)."""
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


DATE_FIELDS = {
    'fecha_inicio', 'fecha_vencimiento', 'fecha_ult_pago',
    'fecha_desembolso'
}


def transform_api_record(raw: dict) -> dict:
    """Map one ACANO API record to our internal schema."""
    record = {}
    for acano_key, our_key in FIELD_MAP.items():
        val = raw.get(acano_key)
        if our_key in DATE_FIELDS:
            val = _normalize_date(val)
        record[our_key] = val
    return record


def transform_api_batch(raw_records: list[dict]) -> list[dict]:
    return [transform_api_record(r) for r in raw_records]


def transform_excel_row(row: tuple) -> dict:
    """Map one Excel row (tuple of values) to our internal schema."""
    record = {}
    for col_idx, our_key in EXCEL_COL_MAP.items():
        val = row[col_idx] if col_idx < len(row) else None
        if our_key in DATE_FIELDS:
            val = _normalize_date(val)
        record[our_key] = val
    return record


def transform_excel_batch(rows: list[tuple]) -> list[dict]:
    return [transform_excel_row(r) for r in rows]
