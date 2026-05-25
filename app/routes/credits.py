"""Credit data API routes with PII masking + audit logging."""
import csv
import io
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from app.database import get_connection
from app.auth.middleware import require_auth, require_superadmin
from app.crypto import decrypt, mask_identificacion, mask_cliente
from app.audit import log_audit, get_client_ip

router = APIRouter(prefix="/api/credits", tags=["credits"])


def _build_query(estado=None, linea=None, calificacion=None, aliado=None,
                 ciudad=None, mora_min=None, mora_max=None):
    conditions = [
        "sync_batch_id = (SELECT id FROM sync_logs WHERE status='success' AND source='manual_upload' ORDER BY id DESC LIMIT 1)"
    ]
    params = []
    if estado:
        conditions.append("estado = ?"); params.append(estado)
    if linea:
        conditions.append("linea = ?"); params.append(linea)
    if calificacion:
        conditions.append("TRIM(calificacion) = ?"); params.append(calificacion)
    if aliado:
        conditions.append("aliado = ?"); params.append(aliado)
    if ciudad:
        conditions.append("ciudad = ?"); params.append(ciudad)
    if mora_min is not None:
        conditions.append("COALESCE(dias_mora, 0) >= ?"); params.append(mora_min)
    if mora_max is not None:
        conditions.append("COALESCE(dias_mora, 0) <= ?"); params.append(mora_max)
    return " AND ".join(conditions), params


def _row_masked(row: dict) -> dict:
    """Return a dict safe for display: PII decrypted then masked."""
    ident = decrypt(row.get("identificacion"))
    cliente = decrypt(row.get("cliente"))
    out = dict(row)
    out["identificacion"] = mask_identificacion(ident)
    out["cliente"] = mask_cliente(cliente)
    return out


@router.get("")
def get_credits(
    _user=Depends(require_auth),
    estado: str = Query(None), linea: str = Query(None),
    calificacion: str = Query(None), aliado: str = Query(None),
    ciudad: str = Query(None), mora_min: int = Query(None),
    mora_max: int = Query(None)
):
    where, params = _build_query(estado, linea, calificacion, aliado, ciudad, mora_min, mora_max)
    conn = get_connection()
    try:
        rows = conn.execute(f"SELECT * FROM credits WHERE {where}", params).fetchall()
        return [_row_masked(dict(r)) for r in rows]
    finally:
        conn.close()


@router.get("/summary")
def get_summary(_user=Depends(require_auth)):
    conn = get_connection()
    try:
        batch = "sync_batch_id = (SELECT id FROM sync_logs WHERE status='success' AND source='manual_upload' ORDER BY id DESC LIMIT 1)"
        # Mora siguiendo política Fideseguros: > 30 días.
        # Tasa promedio ponderada por saldo capital activo.
        row = conn.execute(f"""
            SELECT COUNT(*) as total,
                SUM(CASE WHEN estado='ACTIVO' THEN 1 ELSE 0 END) as activos,
                SUM(valor_credito) as valor_total,
                SUM(CASE WHEN estado='ACTIVO' THEN saldo_capital ELSE 0 END) as saldo_capital_activo,
                SUM(saldo_capital) as saldo_capital,
                CAST(SUM(CASE WHEN estado='ACTIVO' AND tasa_efectiva > 0 THEN tasa_efectiva * saldo_capital ELSE 0 END) AS REAL) /
                    NULLIF(SUM(CASE WHEN estado='ACTIVO' AND tasa_efectiva > 0 THEN saldo_capital ELSE 0 END), 0) as tasa_promedio,
                SUM(CASE WHEN estado='ACTIVO' AND COALESCE(dias_mora,0) > 30 THEN 1 ELSE 0 END) as en_mora_count,
                SUM(CASE WHEN estado='ACTIVO' AND COALESCE(dias_mora,0) > 30 THEN saldo_capital ELSE 0 END) as en_mora_saldo
            FROM credits WHERE {batch}
        """).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


@router.get("/export/csv")
def export_csv(
    request: Request,
    user=Depends(require_superadmin),
    estado: str = Query(None), linea: str = Query(None),
    calificacion: str = Query(None), aliado: str = Query(None),
    ciudad: str = Query(None), mora_min: int = Query(None),
    mora_max: int = Query(None)
):
    where, params = _build_query(estado, linea, calificacion, aliado, ciudad, mora_min, mora_max)
    conn = get_connection()
    try:
        rows = conn.execute(f"SELECT * FROM credits WHERE {where}", params).fetchall()
    finally:
        conn.close()

    ip = get_client_ip(request)
    # Para trazabilidad bajo Habeas Data, registramos los IDs de los créditos
    # exportados. Si son muchos, truncamos el listado y guardamos hash + rango
    # para permitir reconstruir el set sin saturar audit_logs.
    ids = [r['id'] for r in rows]
    if len(ids) <= 200:
        ids_str = ','.join(str(i) for i in ids)
    else:
        import hashlib
        h = hashlib.sha256(','.join(str(i) for i in ids).encode()).hexdigest()[:16]
        ids_str = f"hash={h} count={len(ids)} ids[0:20]={','.join(str(i) for i in ids[:20])}"
    log_audit(user["user_id"], user["username"], "csv_export",
              f"filas={len(rows)} filtros={dict(request.query_params)} ids={ids_str}", ip)

    output = io.StringIO()
    writer = csv.writer(output)
    headers = ['Cliente', 'Identificacion', 'Estado', 'Linea', 'Valor_Credito',
               'Saldo_Capital', 'Calificacion', 'Dias_Mora', 'Tasa_Efectiva',
               'Fecha_Desembolso', 'Fecha_Vencimiento', 'Aliado', 'Ciudad']
    writer.writerow(headers)
    for r in rows:
        d = dict(r)
        writer.writerow([
            decrypt(d.get('cliente')),
            decrypt(d.get('identificacion')),
            d.get('estado', ''), d.get('linea', ''),
            d.get('valor_credito', ''), d.get('saldo_capital', ''),
            d.get('calificacion', ''), d.get('dias_mora', ''),
            d.get('tasa_efectiva', ''), d.get('fecha_desembolso', ''),
            d.get('fecha_vencimiento', ''), d.get('aliado', ''),
            d.get('ciudad', ''),
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=fide_cartera_export.csv"}
    )


@router.get("/{credit_id}/reveal")
def reveal_credit(credit_id: int, request: Request,
                  user=Depends(require_superadmin)):
    """Return full (decrypted) identificacion + cliente for one record.
    Solo permite revelar registros del batch ACTIVO (último upload exitoso de
    manual_upload). Bajo Habeas Data, no se exponen registros de cargas
    históricas que ya no se muestran en el dashboard. Audited.
    """
    ip = get_client_ip(request)
    conn = get_connection()
    try:
        # Restringe a batch activo
        row = conn.execute(
            "SELECT c.id, c.identificacion, c.cliente, c.sync_batch_id "
            "FROM credits c WHERE c.id = ? AND c.sync_batch_id = "
            "(SELECT id FROM sync_logs WHERE source='manual_upload' AND status='success' "
            " ORDER BY id DESC LIMIT 1)",
            (credit_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        log_audit(user["user_id"], user["username"], "credit_reveal_denied",
                  f"credit_id={credit_id} reason=not_in_active_batch", ip)
        raise HTTPException(status_code=404,
                            detail="Registro no disponible en la carga actual")
    log_audit(user["user_id"], user["username"], "credit_reveal",
              f"credit_id={credit_id} batch={row['sync_batch_id']}", ip)
    return {
        "id": row["id"],
        "identificacion": decrypt(row["identificacion"]),
        "cliente": decrypt(row["cliente"]),
    }
