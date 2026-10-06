"""Eventos de auditoría que origina el navegador (exportaciones CSV).

La exportación a CSV se hace del lado del cliente (los datos ya están en
memoria), así que el servidor no se enteraba. Para Habeas Data necesitamos
rastro de quién descargó qué y cuántas filas.
"""
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from app.auth.middleware import require_auth
from app.audit import log_audit, get_client_ip

router = APIRouter(prefix="/api/audit", tags=["audit"])


class ExportEvent(BaseModel):
    archivo: str = Field(max_length=120)
    filas: int = Field(ge=0, le=10_000_000)
    columnas: list[str] = Field(default_factory=list, max_length=60)


@router.post("/export")
def export_event(body: ExportEvent, request: Request, user=Depends(require_auth)):
    cols = ",".join(c[:40] for c in body.columnas[:60])
    log_audit(user["user_id"], user["username"], "csv_export",
              f"archivo={body.archivo} filas={body.filas} cols={cols}",
              get_client_ip(request) or "")
    return {"ok": True}
