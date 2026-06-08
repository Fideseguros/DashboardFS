"""Endpoints de cumplimiento Habeas Data (Colombia, Ley 1581 / Decreto 1377).

Implementa el derecho del titular a solicitar la SUPRESIÓN de su información
personal de las bases de datos del responsable del tratamiento (Art. 8, lit. e).

Política técnica:
  - NO se borra la fila entera (preservamos integridad referencial para
    reportes agregados anonimizados — saldos totales, contadores, etc.).
  - SE pone NULL en las columnas PII (identificacion, cliente, nombre,
    solicitante). El resto de campos (saldo, mora, estado, etc.) se
    conservan pero sin asociarlos a una persona identificable.
  - Se loguea el evento como 'habeas_data_delete' con cuenta de filas
    afectadas por tabla, para trazabilidad ante la SIC.

Limitación conocida: la identificación está cifrada con Fernet (IV random),
así que NO podemos hacer WHERE indexed. Hay que decifrar fila a fila. Para
3500 créditos esto toma <2s (aceptable porque es operación puntual).
"""
import logging
import re
from typing import Optional
from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel, Field
from app.database import get_db, get_connection
from app.auth.middleware import require_superadmin
from app.crypto import decrypt
from app.audit import log_audit, get_client_ip

router = APIRouter(prefix="/api/habeas-data", tags=["habeas-data"])
_log = logging.getLogger("fide.habeas")

# Tablas y columnas PII a anonimizar. Si se agrega nueva tabla con PII en
# database.py, ACTUALIZAR ESTA LISTA o quedará data fantasma de titulares.
PII_TABLES = [
    # (tabla, columna_id_cifrada, [columnas_pii_a_anonimizar])
    ("credits",            "identificacion", ["identificacion", "cliente",
                                              "identificacion_masked", "cliente_masked"]),
    ("pagos",              "identificacion", ["identificacion", "cliente"]),
    ("solicitudes",        "identificacion", ["identificacion", "solicitante"]),
    ("pagos_legacy",       "identificacion", ["identificacion", "nombre"]),
    ("solicitudes_legacy", "identificacion", ["identificacion", "nombre_completo"]),
    ("procesos_juridicos", "identificacion", ["identificacion", "nombre"]),
]


def _norm_id(s: Optional[str]) -> str:
    """Normaliza una identificación: solo dígitos, sin ceros a la izquierda."""
    if not s:
        return ""
    digits = re.sub(r'\D', '', str(s))
    return digits.lstrip('0') or digits


class DeleteRequest(BaseModel):
    identificacion: str = Field(..., min_length=4, max_length=40,
                                description="Cédula u otra identificación del titular")
    motivo: str = Field(..., min_length=10, max_length=500,
                        description="Justificación legal (Art. 8 Ley 1581). Queda en audit.")
    confirmar: bool = Field(False, description="Debe ser true para ejecutar")


@router.post("/delete-titular")
def delete_titular(req: DeleteRequest, request: Request,
                   user=Depends(require_superadmin)):
    """Borra la PII (identificacion + nombre/cliente) de un titular en TODAS
    las tablas. Conserva saldos/contadores agregados (anonimización).

    Requiere confirmar=true + motivo justificado. Audit completo.
    """
    if not req.confirmar:
        raise HTTPException(
            status_code=400,
            detail="Debes pasar confirmar=true para ejecutar el borrado."
        )

    target_norm = _norm_id(req.identificacion)
    if not target_norm:
        raise HTTPException(status_code=400, detail="Identificación inválida.")

    ip = get_client_ip(request) or "unknown"
    affected = {}  # {tabla: count}

    conn = get_connection()
    try:
        # Para cada tabla con PII, decifrar identificacion fila a fila,
        # comparar normalizada, juntar IDs match, luego UPDATE.
        for table, id_col, pii_cols in PII_TABLES:
            rows = conn.execute(
                f"SELECT id, {id_col} FROM {table} WHERE {id_col} IS NOT NULL"
            ).fetchall()
            match_ids = []
            for r in rows:
                try:
                    plain = decrypt(r[id_col])
                except Exception:
                    continue  # ciphertext corrupto, saltar
                if _norm_id(plain) == target_norm:
                    match_ids.append(r["id"])
            if not match_ids:
                affected[table] = 0
                continue
            # UPDATE en batches para no construir SQL gigante
            set_clause = ", ".join(f"{c} = NULL" for c in pii_cols)
            for i in range(0, len(match_ids), 500):
                batch = match_ids[i:i+500]
                placeholders = ",".join("?" * len(batch))
                conn.execute(
                    f"UPDATE {table} SET {set_clause} WHERE id IN ({placeholders})",
                    batch
                )
            affected[table] = len(match_ids)
            conn.commit()
    finally:
        conn.close()

    total = sum(affected.values())
    audit_detail = (
        f"titular_norm_hash=********** "  # NO logueamos el ID en claro
        f"motivo={req.motivo[:200]} "
        f"total_filas={total} "
        f"por_tabla={affected}"
    )
    log_audit(user["user_id"], user["username"],
              "habeas_data_delete", audit_detail, ip)

    return {
        "status": "success",
        "total_filas_anonimizadas": total,
        "por_tabla": affected,
        "nota": "La identificación y nombre del titular fueron eliminados. "
                "Datos agregados (saldos, contadores) se conservan anonimizados.",
    }


@router.get("/lookup")
def lookup_titular(identificacion: str, user=Depends(require_superadmin),
                   request: Request = None):
    """Cuenta cuántas filas existen para una identificación, SIN borrar.

    Usar antes de delete-titular para verificar el impacto. Audit explícito.
    """
    target_norm = _norm_id(identificacion)
    if not target_norm or len(target_norm) < 4:
        raise HTTPException(status_code=400, detail="Identificación inválida.")

    ip = get_client_ip(request) if request else "unknown"
    counts = {}
    conn = get_connection()
    try:
        for table, id_col, _pii in PII_TABLES:
            rows = conn.execute(
                f"SELECT {id_col} FROM {table} WHERE {id_col} IS NOT NULL"
            ).fetchall()
            n = 0
            for r in rows:
                try:
                    plain = decrypt(r[id_col])
                except Exception:
                    continue
                if _norm_id(plain) == target_norm:
                    n += 1
            counts[table] = n
    finally:
        conn.close()

    log_audit(user["user_id"], user["username"],
              "habeas_data_lookup", f"counts={counts}", ip)
    return {"por_tabla": counts, "total": sum(counts.values())}
