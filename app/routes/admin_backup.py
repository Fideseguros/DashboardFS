"""Administración de respaldos de la BD (solo superadmin).

  GET  /api/admin/backups           → lista (nombre, tamaño, fecha)
  POST /api/admin/backups/run       → ejecuta un respaldo ahora
  GET  /api/admin/backups/{nombre}  → descarga el .gz

La descarga entrega la BD COMPLETA (PII cifrada con Fernet, pero igual es el
activo más sensible), por eso: solo superadmin, nombre validado contra regex
estricto (sin "/", sin "..") y auditoría como acción crítica.
"""
import os
import logging
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import FileResponse

from app.auth.middleware import require_superadmin
from app.audit import log_audit, get_client_ip, CRITICAL_ACTIONS
from app import backup as bk

router = APIRouter(prefix="/api/admin/backups", tags=["admin-backup"])
_log = logging.getLogger("fide.admin_backup")

# La descarga del respaldo no debe perder rastro aunque falle la BD.
CRITICAL_ACTIONS.add("backup_download")


@router.get("")
def listar_backups(user=Depends(require_superadmin)):
    return {"backups": bk.list_backups(), "dir": bk.backup_dir(), "conserva": bk.KEEP_LAST}


@router.post("/run")
def ejecutar_backup(request: Request, user=Depends(require_superadmin)):
    try:
        nombre = bk.run_backup()
    except Exception as e:
        _log.exception("respaldo manual falló")
        raise HTTPException(status_code=500, detail=f"No se pudo crear el respaldo: {type(e).__name__}")
    ip = get_client_ip(request) or "unknown"
    log_audit(user["user_id"], user["username"], "backup_run", f"archivo={nombre}", ip)
    return {"ok": True, "nombre": nombre}


@router.get("/{nombre}")
def descargar_backup(nombre: str, request: Request, user=Depends(require_superadmin)):
    # Validación estricta ANTES de tocar el disco: solo fide_YYYYMMDD_HHMM.db.gz
    if not bk.BACKUP_NAME_RE.match(nombre):
        raise HTTPException(status_code=400, detail="Nombre de respaldo inválido.")
    ruta = os.path.join(bk.backup_dir(), nombre)
    # Defensa en profundidad: la ruta resuelta debe seguir dentro de backups/
    if os.path.dirname(os.path.abspath(ruta)) != os.path.abspath(bk.backup_dir()):
        raise HTTPException(status_code=400, detail="Nombre de respaldo inválido.")
    if not os.path.isfile(ruta):
        raise HTTPException(status_code=404, detail="Respaldo no encontrado.")

    ip = get_client_ip(request) or "unknown"
    log_audit(user["user_id"], user["username"], "backup_download",
              # bytes con separador de miles: el scrubber de auditoría hashea 5+ dígitos seguidos
              f"archivo={nombre} bytes={os.path.getsize(ruta):,}".replace(",", "."), ip)
    return FileResponse(ruta, media_type="application/gzip", filename=nombre)
