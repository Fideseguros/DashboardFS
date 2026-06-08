"""Helpers compartidos para endpoints de upload.

Reemplaza la duplicación masiva de patrones de upload en extras.py /
financieros.py / sync.py:
  - validación del archivo (tipo, tamaño)
  - creación del sync_log
  - try/except con finalize + audit
  - mensaje de error genérico al cliente

Uso:
    @router.post("/upload")
    async def my_upload(request, user=Depends(require_superadmin), file=File(...)):
        async with upload_session(
            request, user, file,
            source='my_module_upload', max_mb=25
        ) as ctx:
            rows = ctx.read_excel()
            records = parse_rows(rows)
            # ... write to DB ...
            ctx.set_counts(fetched=len(rows), inserted=len(records))
            return {"status": "success", "records": len(records)}
"""
import io
import logging
import zipfile
import xml.etree.ElementTree as ET
import re
from contextlib import asynccontextmanager
from datetime import datetime, date
from fastapi import HTTPException, UploadFile, Request
import openpyxl
from app.database import get_db
from app.audit import log_audit, get_client_ip

_log = logging.getLogger("fide.upload")


# ============================================================
#                   PARSERS DE EXCEL
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


def read_excel(content: bytes) -> list[tuple]:
    """Intenta openpyxl primero; si falla por estilos corruptos, usa parser XML directo."""
    try:
        return _parse_with_openpyxl(content)
    except Exception as e:
        _log.warning("openpyxl falló (%s), usando parser XML directo", e)
        return _parse_with_zip_xml(content)


# ============================================================
#                   CONVERTERS REUSABLES
# ============================================================
def to_float(v):
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


def to_date(v):
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


def str_or_none(v):
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


# ============================================================
#                   CONTEXT MANAGER
# ============================================================
class UploadContext:
    """Estado del upload mientras se procesa."""

    def __init__(self, request: Request, user: dict, file: UploadFile,
                 source: str, content: bytes):
        self.request = request
        self.user = user
        self.file = file
        self.source = source
        self.content = content
        self.sync_id: int | None = None
        self.ip = get_client_ip(request) or "unknown"
        self._fetched = 0
        self._inserted = 0
        self._extra_audit = ""

    def read_excel(self) -> list[tuple]:
        """Lee el Excel del upload."""
        return read_excel(self.content)

    def set_counts(self, fetched: int, inserted: int):
        self._fetched = fetched
        self._inserted = inserted

    def set_audit_extra(self, extra: str):
        """Añade detalle adicional al audit log (e.g. 'year=2026 meses=Ene,Feb')."""
        self._extra_audit = extra


def _check_upload_file(file: UploadFile, content: bytes, max_mb: int):
    """Valida tipo, tamaño, magic bytes y ratio de descompresión del archivo.

    Defensas (auditoría M4):
      - extensión .xlsx/.xls
      - tamaño máximo
      - magic bytes 'PK\\x03\\x04' (xlsx es zip) o 'D0CF11E0' (xls compound)
        — previene archivos renombrados (ej. .exe → .xlsx)
      - zip-bomb: ratio uncompressed/compressed >100x es señal de bomba
        (un xlsx normal está entre 3-15x). Si excede, rechazar antes de
        que openpyxl intente parsearlo y consuma toda la RAM.
    """
    if not file.filename or not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="Solo archivos .xlsx o .xls")
    size_mb = len(content) / (1024 * 1024)
    if size_mb > max_mb:
        raise HTTPException(status_code=413, detail=f"Archivo excede {max_mb} MB")
    # Magic bytes
    if len(content) < 8:
        raise HTTPException(status_code=400, detail="Archivo demasiado pequeño o corrupto")
    head = content[:8]
    is_zip = head[:4] == b'PK\x03\x04'   # xlsx (Office Open XML, es un zip)
    is_ole = head[:4] == b'\xD0\xCF\x11\xE0'  # xls legacy (Compound File Binary)
    if not (is_zip or is_ole):
        raise HTTPException(
            status_code=400,
            detail="El archivo no parece un Excel válido (magic bytes incorrectos)."
        )
    # Zip-bomb check (solo aplica a .xlsx que es zip)
    if is_zip:
        import zipfile, io as _io
        try:
            with zipfile.ZipFile(_io.BytesIO(content)) as zf:
                total_uncompressed = sum(zi.file_size for zi in zf.infolist())
                # Ratio razonable para xlsx: 3-15x. >100x es bomba.
                # Cap absoluto: 500 MB descomprimido (un xlsx legítimo de
                # 25 MB raramente excede 200 MB en disco).
                if total_uncompressed > 500 * 1024 * 1024:
                    raise HTTPException(
                        status_code=400,
                        detail="Archivo descomprimido excede 500 MB — posible bomba zip."
                    )
                if len(content) > 0 and total_uncompressed / len(content) > 100:
                    raise HTTPException(
                        status_code=400,
                        detail="Ratio de compresión sospechoso — posible bomba zip."
                    )
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail="Archivo .xlsx corrupto o malformado.")


def _create_sync_log(uploaded_by: int, source: str) -> int:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sync_logs (started_at, status, source, uploaded_by) "
            "VALUES (?, 'running', ?, ?)",
            (datetime.utcnow().isoformat(), source, uploaded_by)
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _finalize_sync(sync_id: int, status: str, fetched: int, inserted: int, error: str = ''):
    with get_db() as conn:
        conn.execute(
            "UPDATE sync_logs SET status=?, completed_at=?, "
            "records_fetched=?, records_inserted=?, error_message=? WHERE id=?",
            (status, datetime.utcnow().isoformat(), fetched, inserted, error[:400], sync_id)
        )


@asynccontextmanager
async def upload_session(request: Request, user: dict, file: UploadFile,
                          source: str, max_mb: int = 25,
                          generic_error_msg: str = "No se pudo importar el archivo. Revisa el formato y vuelve a intentarlo."):
    """Context manager que encapsula el patrón completo de upload.

    - Lee el cuerpo del archivo
    - Valida tipo y tamaño
    - Crea sync_log
    - Provee UploadContext al bloque
    - En éxito: actualiza sync_log a 'success' + audit positivo
    - En error: actualiza a 'failed' + audit negativo + HTTPException 500 con mensaje genérico
    """
    content = await file.read()
    _check_upload_file(file, content, max_mb)
    sync_id = _create_sync_log(user["user_id"], source)
    ctx = UploadContext(request, user, file, source, content)
    ctx.sync_id = sync_id
    try:
        yield ctx
        # éxito
        _finalize_sync(sync_id, 'success', ctx._fetched, ctx._inserted)
        details = f"file={file.filename} records={ctx._inserted}"
        if ctx._extra_audit:
            details += " " + ctx._extra_audit
        log_audit(user["user_id"], user["username"], source, details, ctx.ip)
    except HTTPException:
        # Errores que ya son HTTPException (validación previa) → re-raise sin alterar
        _finalize_sync(sync_id, 'failed', ctx._fetched, ctx._inserted, "HTTPException")
        raise
    except Exception as e:
        _log.exception("upload failed source=%s", source)
        _finalize_sync(sync_id, 'failed', ctx._fetched, ctx._inserted,
                       f"{type(e).__name__}: {str(e)[:200]}")
        log_audit(user["user_id"], user["username"], source + "_failed",
                  f"file={file.filename} error={type(e).__name__}: {str(e)[:200]}", ctx.ip)
        raise HTTPException(status_code=500, detail=generic_error_msg) from e
