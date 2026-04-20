"""Sync management routes — Excel upload only (superadmin)."""
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, Request
from app.database import get_connection
from app.auth.middleware import require_auth, require_superadmin
from app.audit import log_audit, get_client_ip

router = APIRouter(prefix="/api/sync", tags=["sync"])

MAX_UPLOAD_MB = 25


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
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="Solo archivos .xlsx o .xls")

    content = await file.read()
    size_mb = len(content) / (1024 * 1024)
    if size_mb > MAX_UPLOAD_MB:
        raise HTTPException(status_code=413,
                            detail=f"Archivo excede {MAX_UPLOAD_MB} MB")

    ip = get_client_ip(request)
    from app.sync.job import sync_from_excel
    try:
        result = sync_from_excel(content, uploaded_by=user["user_id"])
        log_audit(user["user_id"], user["username"], "excel_upload",
                  f"file={file.filename} records={result.get('records', 0)}", ip)
        # Discard the in-memory buffer explicitly
        del content
        return result
    except Exception as e:
        log_audit(user["user_id"], user["username"], "excel_upload_failed",
                  f"file={file.filename} error={str(e)[:200]}", ip)
        raise HTTPException(status_code=500, detail=f"Import failed: {str(e)}")
