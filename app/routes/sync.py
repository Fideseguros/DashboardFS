"""Sync management routes."""
import asyncio
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from app.database import get_connection
from app.auth.middleware import require_auth

router = APIRouter(prefix="/api/sync", tags=["sync"])


@router.get("/status")
def sync_status(_user=Depends(require_auth)):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM sync_logs ORDER BY id DESC LIMIT 1"
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


@router.post("/trigger")
async def trigger_sync(_user=Depends(require_auth)):
    from app.sync.job import sync_from_api
    try:
        result = await sync_from_api()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync failed: {str(e)}")


@router.post("/upload-excel")
async def upload_excel(_user=Depends(require_auth), file: UploadFile = File(...)):
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="Solo archivos .xlsx o .xls")
    from app.sync.job import sync_from_excel
    try:
        content = await file.read()
        result = sync_from_excel(content)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Import failed: {str(e)}")
