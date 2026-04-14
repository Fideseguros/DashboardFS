"""Credit data API routes."""
import csv
import io
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from app.database import get_connection, CREDIT_FIELDS
from app.auth.middleware import require_auth

router = APIRouter(prefix="/api/credits", tags=["credits"])


def _build_query(estado=None, linea=None, calificacion=None, aliado=None,
                 ciudad=None, mora_min=None, mora_max=None):
    """Build WHERE clause from filter params. Returns (where_sql, params)."""
    conditions = []
    params = []

    # Only fetch from the latest successful sync batch
    conditions.append(
        "sync_batch_id = (SELECT id FROM sync_logs WHERE status='success' ORDER BY id DESC LIMIT 1)"
    )

    if estado:
        conditions.append("estado = ?")
        params.append(estado)
    if linea:
        conditions.append("linea = ?")
        params.append(linea)
    if calificacion:
        conditions.append("TRIM(calificacion) = ?")
        params.append(calificacion)
    if aliado:
        conditions.append("aliado = ?")
        params.append(aliado)
    if ciudad:
        conditions.append("ciudad = ?")
        params.append(ciudad)
    if mora_min is not None:
        conditions.append("COALESCE(dias_mora, 0) >= ?")
        params.append(mora_min)
    if mora_max is not None:
        conditions.append("COALESCE(dias_mora, 0) <= ?")
        params.append(mora_max)

    where = " AND ".join(conditions) if conditions else "1=1"
    return where, params


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
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/summary")
def get_summary(_user=Depends(require_auth)):
    conn = get_connection()
    try:
        batch_filter = "sync_batch_id = (SELECT id FROM sync_logs WHERE status='success' ORDER BY id DESC LIMIT 1)"
        row = conn.execute(f"""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN estado='ACTIVO' THEN 1 ELSE 0 END) as activos,
                SUM(valor_credito) as valor_total,
                SUM(saldo_capital) as saldo_capital,
                AVG(CASE WHEN estado='ACTIVO' AND tasa_efectiva > 0 THEN tasa_efectiva END) as tasa_promedio,
                SUM(CASE WHEN estado='ACTIVO' AND COALESCE(dias_mora,0) > 0 THEN 1 ELSE 0 END) as en_mora_count,
                SUM(CASE WHEN estado='ACTIVO' AND COALESCE(dias_mora,0) > 0 THEN saldo_capital ELSE 0 END) as en_mora_saldo
            FROM credits WHERE {batch_filter}
        """).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


@router.get("/export/csv")
def export_csv(
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
    finally:
        conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    headers = ['Cliente', 'Identificacion', 'Estado', 'Linea', 'Valor_Credito',
               'Saldo_Capital', 'Calificacion', 'Dias_Mora', 'Tasa_Efectiva',
               'Fecha_Desembolso', 'Fecha_Vencimiento', 'Aliado', 'Ciudad']
    keys = ['cliente', 'identificacion', 'estado', 'linea', 'valor_credito',
            'saldo_capital', 'calificacion', 'dias_mora', 'tasa_efectiva',
            'fecha_desembolso', 'fecha_vencimiento', 'aliado', 'ciudad']
    writer.writerow(headers)
    for r in rows:
        d = dict(r)
        writer.writerow([d.get(k, '') for k in keys])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=fide_cartera_export.csv"}
    )
