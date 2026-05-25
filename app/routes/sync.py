"""Sync management routes — Excel upload only (superadmin)."""
import logging
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, Request
from app.database import get_connection
from app.auth.middleware import require_auth, require_superadmin
from app.audit import log_audit, get_client_ip

router = APIRouter(prefix="/api/sync", tags=["sync"])

MAX_UPLOAD_MB = 25
_log = logging.getLogger("fide.sync")


@router.get("/status")
def sync_status(_user=Depends(require_auth)):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT sl.*, u.username as uploader_username "
            "FROM sync_logs sl LEFT JOIN users u ON u.id = sl.uploaded_by "
            "ORDER BY sl.id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return {"last_sync": None, "status": "never", "records_count": 0}
        d = dict(row)
        count = conn.execute(
            "SELECT COUNT(*) as cnt FROM credits WHERE sync_batch_id = ?", (d["id"],)
        ).fetchone()
        d["records_count"] = count["cnt"] if count else 0
        return d
    finally:
        conn.close()


@router.post("/upload-excel")
async def upload_excel(request: Request,
                       user=Depends(require_superadmin),
                       file: UploadFile = File(...)):
    """Carga BASE (full replace): reemplaza toda la cartera con el archivo
    de la plataforma vieja (cartera vieja, ~3153 filas). Uso esporádico."""
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="Solo archivos .xlsx o .xls")

    content = await file.read()
    size_mb = len(content) / (1024 * 1024)
    if size_mb > MAX_UPLOAD_MB:
        raise HTTPException(status_code=413,
                            detail=f"Archivo excede {MAX_UPLOAD_MB} MB")

    ip = get_client_ip(request) or "unknown"
    from app.sync.job import sync_from_excel
    try:
        result = sync_from_excel(content, uploaded_by=user["user_id"])
        log_audit(user["user_id"], user["username"], "excel_upload",
                  f"file={file.filename} mode=base records={result.get('records', 0)}", ip)
        del content
        return result
    except Exception as e:
        _log.exception("Excel upload (base) failed")
        log_audit(user["user_id"], user["username"], "excel_upload_failed",
                  f"file={file.filename} mode=base error={type(e).__name__}: {str(e)[:200]}", ip)
        raise HTTPException(status_code=500,
                            detail="No se pudo importar el archivo. Revise el formato e intente nuevamente.")


@router.post("/update-incremental")
async def update_incremental(request: Request,
                              user=Depends(require_superadmin),
                              file: UploadFile = File(...)):
    """Actualización incremental: toma el archivo de la plataforma nueva
    (cartera nueva) y actualiza solo las columnas naranja en cuentas
    coincidentes. Cuentas nuevas se insertan, cuentas del base que no
    aparecen en el archivo se mantienen intactas. Uso periódico/diario."""
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="Solo archivos .xlsx o .xls")

    content = await file.read()
    size_mb = len(content) / (1024 * 1024)
    if size_mb > MAX_UPLOAD_MB:
        raise HTTPException(status_code=413,
                            detail=f"Archivo excede {MAX_UPLOAD_MB} MB")

    ip = get_client_ip(request) or "unknown"
    from app.sync.job import incremental_update_from_excel
    try:
        result = incremental_update_from_excel(content, uploaded_by=user["user_id"])
        log_audit(
            user["user_id"], user["username"], "excel_upload",
            f"file={file.filename} mode=update actualizados={result.get('actualizados', 0)} "
            f"nuevos={result.get('nuevos', 0)}", ip
        )
        del content
        return result
    except Exception as e:
        _log.exception("Excel update (incremental) failed")
        log_audit(user["user_id"], user["username"], "excel_upload_failed",
                  f"file={file.filename} mode=update error={type(e).__name__}: {str(e)[:200]}", ip)
        raise HTTPException(status_code=500,
                            detail="No se pudo importar la actualización. Revise el formato e intente nuevamente.")
