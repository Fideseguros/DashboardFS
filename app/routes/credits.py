"""Credit data API routes with PII masking + audit logging."""
import csv
import io
import hashlib
from fastapi import APIRouter, Depends, Query, Request, Response, HTTPException, Header
from fastapi.responses import StreamingResponse
from app.database import get_connection
from app.auth.middleware import require_auth, require_superadmin
from app.crypto import decrypt, mask_identificacion, mask_cliente
from app.audit import log_audit, get_client_ip


def _active_batch_etag(conn) -> str:
    """ETag basado en el último sync exitoso del batch activo de cartera.
    Cuando admin sube una nueva cartera, el id sube → etag cambia → cache invalida."""
    row = conn.execute(
        "SELECT id, completed_at FROM sync_logs "
        "WHERE source='manual_upload' AND status='success' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return '"no-batch"'
    return f'"batch-{row["id"]}-{row["completed_at"] or ""}"'

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
    """Return a dict safe for display: PII enmascarada.

    Prefiere la versión cacheada (identificacion_masked / cliente_masked)
    para evitar el costo de decrypt+mask en cada request. Fallback al
    decrypt para filas pre-migración (cuando aún no se ha hecho backfill).
    """
    out = dict(row)
    im = out.pop("identificacion_masked", None)
    cm = out.pop("cliente_masked", None)
    if im is not None:
        out["identificacion"] = im
    else:
        out["identificacion"] = mask_identificacion(decrypt(out.get("identificacion")))
    if cm is not None:
        out["cliente"] = cm
    else:
        out["cliente"] = mask_cliente(decrypt(out.get("cliente")))
    return out


@router.get("")
def get_credits(
    response: Response,
    _user=Depends(require_auth),
    estado: str = Query(None), linea: str = Query(None),
    calificacion: str = Query(None), aliado: str = Query(None),
    ciudad: str = Query(None), mora_min: int = Query(None),
    mora_max: int = Query(None),
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
):
    """Lista de créditos del batch activo.

    Cuando se llama SIN filtros (caso típico del dashboard), se devuelve
    un ETag basado en el id del último sync. Si el cliente lo manda en
    If-None-Match, respondemos 304 Not Modified sin re-serializar nada.
    Resultado: cargas posteriores sin nueva carga de cartera = instantáneas.
    """
    conn = get_connection()
    try:
        # Caché solo cuando no hay filtros — el dashboard llama sin filtros.
        no_filters = not any([estado, linea, calificacion, aliado, ciudad,
                              mora_min is not None, mora_max is not None])
        if no_filters:
            etag = _active_batch_etag(conn)
            if if_none_match and if_none_match == etag:
                # No fue modificado desde la última vez que el cliente lo pidió
                response.status_code = 304
                response.headers["ETag"] = etag
                response.headers["Cache-Control"] = "private, must-revalidate"
                return Response(status_code=304)
            response.headers["ETag"] = etag
            response.headers["Cache-Control"] = "private, must-revalidate"

        where, params = _build_query(estado, linea, calificacion, aliado, ciudad, mora_min, mora_max)
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


@router.get("/kpis")
def get_kpis(_user=Depends(require_auth)):
    """KPIs del dashboard pre-calculados en SQL para la carga inicial sin filtros.

    Mover el cómputo al servidor evita que el navegador itere ~3.5k filas en JS
    en el primer paint. Cuando el usuario aplica un filtro, el frontend vuelve
    a calcular los KPIs sobre filteredData (función updateKPIs).

    Política Fideseguros aplicada:
      - "mora real" = dias_mora > 30 sobre estado='ACTIVO'.
      - "tasa_promedio_activos" = SUM(tasa*saldo)/SUM(saldo) para activos con
        tasa > 0 y saldo > 0 (ponderada por saldo capital).
      - "icv_pct" = saldo_mora_30 / saldo_capital_activo * 100.
    """
    conn = get_connection()
    try:
        batch = ("sync_batch_id = (SELECT id FROM sync_logs "
                 "WHERE status='success' AND source='manual_upload' "
                 "ORDER BY id DESC LIMIT 1)")
        # Una sola query agregada: la fila vuelve con todas las métricas que
        # consumen los KPI cards. Math.round en el cliente cuando hace falta.
        row = conn.execute(f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN estado='ACTIVO' THEN 1 ELSE 0 END) AS activos,
                SUM(CASE WHEN estado!='ACTIVO' THEN 1 ELSE 0 END) AS cancelados,
                COALESCE(SUM(valor_credito), 0) AS valor_total,
                COALESCE(SUM(CASE WHEN estado='ACTIVO' THEN valor_credito ELSE 0 END), 0) AS valor_total_activos,
                COALESCE(SUM(saldo_capital), 0) AS saldo_capital,
                COALESCE(SUM(CASE WHEN estado='ACTIVO' THEN saldo_capital ELSE 0 END), 0) AS saldo_capital_activos,
                CAST(SUM(CASE WHEN estado='ACTIVO' AND tasa_efectiva > 0 AND saldo_capital > 0
                              THEN tasa_efectiva * saldo_capital ELSE 0 END) AS REAL) /
                    NULLIF(SUM(CASE WHEN estado='ACTIVO' AND tasa_efectiva > 0 AND saldo_capital > 0
                                    THEN saldo_capital ELSE 0 END), 0) AS tasa_promedio_activos,
                MIN(CASE WHEN estado='ACTIVO' AND tasa_efectiva > 0 THEN tasa_efectiva END) AS tasa_min_activos,
                MAX(CASE WHEN estado='ACTIVO' AND tasa_efectiva > 0 THEN tasa_efectiva END) AS tasa_max_activos,
                SUM(CASE WHEN estado='ACTIVO' AND COALESCE(dias_mora, 0) > 30 THEN 1 ELSE 0 END) AS en_mora_count,
                COALESCE(SUM(CASE WHEN estado='ACTIVO' AND COALESCE(dias_mora, 0) > 30
                                  THEN saldo_capital ELSE 0 END), 0) AS en_mora_saldo,
                COUNT(CASE WHEN valor_credito > 0 THEN 1 END) AS n_valor_pos,
                COALESCE(SUM(CASE WHEN valor_credito > 0 THEN valor_credito ELSE 0 END), 0) AS sum_valor_pos,
                COUNT(CASE WHEN estado='ACTIVO' AND valor_credito > 0 THEN 1 END) AS n_valor_pos_activos,
                COALESCE(SUM(CASE WHEN estado='ACTIVO' AND valor_credito > 0 THEN valor_credito ELSE 0 END), 0) AS sum_valor_pos_activos
            FROM credits WHERE {batch}
        """).fetchone()
        if not row or not row["total"]:
            return {
                "total": 0, "activos": 0, "cancelados": 0,
                "valor_total": 0, "valor_total_activos": 0,
                "saldo_capital": 0, "saldo_capital_activos": 0,
                "tasa_promedio_activos": 0, "tasa_min_activos": 0, "tasa_max_activos": 0,
                "en_mora_count": 0, "en_mora_saldo": 0,
                "ticket_promedio": 0, "ticket_promedio_activos": 0,
                "icv_pct": 0,
            }
        d = dict(row)
        saldo_act = float(d["saldo_capital_activos"] or 0)
        en_mora_saldo = float(d["en_mora_saldo"] or 0)
        icv_pct = (en_mora_saldo / saldo_act * 100.0) if saldo_act > 0 else 0.0
        ticket = (float(d["sum_valor_pos"] or 0) / d["n_valor_pos"]) if d["n_valor_pos"] else 0.0
        ticket_act = (float(d["sum_valor_pos_activos"] or 0) / d["n_valor_pos_activos"]) if d["n_valor_pos_activos"] else 0.0
        return {
            "total": int(d["total"] or 0),
            "activos": int(d["activos"] or 0),
            "cancelados": int(d["cancelados"] or 0),
            "valor_total": float(d["valor_total"] or 0),
            "valor_total_activos": float(d["valor_total_activos"] or 0),
            "saldo_capital": float(d["saldo_capital"] or 0),
            "saldo_capital_activos": saldo_act,
            "tasa_promedio_activos": float(d["tasa_promedio_activos"] or 0),
            "tasa_min_activos": float(d["tasa_min_activos"] or 0),
            "tasa_max_activos": float(d["tasa_max_activos"] or 0),
            "en_mora_count": int(d["en_mora_count"] or 0),
            "en_mora_saldo": en_mora_saldo,
            "ticket_promedio": ticket,
            "ticket_promedio_activos": ticket_act,
            "icv_pct": icv_pct,
        }
    finally:
        conn.close()


@router.get("/desembolso-vs-recaudo")
def desembolso_vs_recaudo(_user=Depends(require_auth)):
    """Comparativo Valor Desembolsado vs Valor Recaudado por estado del crédito.

    - Desembolsado = SUM(valor_credito) de los créditos en el batch activo.
    - Recaudado = SUM(pagos.total) de pagos del último batch de recaudo
      (plataforma nueva) MÁS SUM(pagos_legacy.valor_pago_total) del histórico
      de la plataforma vieja, ambos cruzados con créditos por 'cuenta' (o por
      id_prestamo en el caso del legacy) para distribuirlos por estado.
    """
    conn = get_connection()
    try:
        batch_cart = ("(SELECT id FROM sync_logs WHERE source='manual_upload' "
                      "AND status='success' ORDER BY id DESC LIMIT 1)")
        batch_rec  = ("(SELECT id FROM sync_logs WHERE source='recaudo_upload' "
                      "AND status='success' ORDER BY id DESC LIMIT 1)")

        # Desembolsado por estado + breakdown nueva/legacy via cartera_nueva_cuentas.
        # Una cuenta es "nueva" si aparece en cartera_nueva_cuentas (= estaba en
        # el último archivo de la plataforma nueva subido). De lo contrario, "legacy".
        desem = conn.execute(f"""
            SELECT
                COALESCE(SUM(c.valor_credito), 0) as total,
                COALESCE(SUM(CASE WHEN c.estado='CANCELADO' THEN c.valor_credito ELSE 0 END), 0) as cerrado,
                COALESCE(SUM(CASE WHEN c.estado='ACTIVO'    THEN c.valor_credito ELSE 0 END), 0) as activos,
                COALESCE(SUM(CASE WHEN n.cuenta IS NOT NULL                          THEN c.valor_credito ELSE 0 END), 0) as total_nueva,
                COALESCE(SUM(CASE WHEN n.cuenta IS NOT NULL AND c.estado='CANCELADO' THEN c.valor_credito ELSE 0 END), 0) as cerrado_nueva,
                COALESCE(SUM(CASE WHEN n.cuenta IS NOT NULL AND c.estado='ACTIVO'    THEN c.valor_credito ELSE 0 END), 0) as activos_nueva
            FROM credits c
            LEFT JOIN cartera_nueva_cuentas n ON n.cuenta = c.cuenta
            WHERE c.sync_batch_id = {batch_cart}
        """).fetchone()

        # Recaudado plataforma NUEVA (tabla pagos, JOIN por cuenta).
        rec_new = conn.execute(f"""
            SELECT
                COALESCE(SUM(p.total), 0) as total,
                COALESCE(SUM(CASE WHEN c.estado='CANCELADO' THEN p.total ELSE 0 END), 0) as cerrado,
                COALESCE(SUM(CASE WHEN c.estado='ACTIVO'    THEN p.total ELSE 0 END), 0) as activos
            FROM pagos p
            LEFT JOIN credits c
                ON c.cuenta = p.cuenta
                AND c.sync_batch_id = {batch_cart}
            WHERE p.sync_batch_id = {batch_rec}
        """).fetchone()

        # Recaudado plataforma VIEJA (pagos_legacy, agregado por id_prestamo).
        # JOIN normalizado: TRIM + LTRIM leading zeros para evitar misses
        # silenciosos por formatos distintos (ej. "00005" vs "5", " 2230" vs "2230").
        # Devuelve también 'sin_match' = pagos que no encuentran credits.cuenta —
        # estos NO entran en cerrado/activos pero sí en total, lo que evita la
        # discrepancia silenciosa (total != cerrado + activos) que detectó la auditoría.
        rec_leg = conn.execute(f"""
            SELECT
                COALESCE(SUM(pl.valor_pago_total), 0) as total,
                COALESCE(SUM(CASE WHEN c.estado='CANCELADO' THEN pl.valor_pago_total ELSE 0 END), 0) as cerrado,
                COALESCE(SUM(CASE WHEN c.estado='ACTIVO'    THEN pl.valor_pago_total ELSE 0 END), 0) as activos,
                COALESCE(SUM(CASE WHEN c.cuenta IS NULL     THEN pl.valor_pago_total ELSE 0 END), 0) as sin_match
            FROM pagos_legacy pl
            LEFT JOIN credits c
                ON (
                    TRIM(c.cuenta) = TRIM(pl.id_prestamo)
                    OR LTRIM(TRIM(c.cuenta), '0') = LTRIM(TRIM(pl.id_prestamo), '0')
                )
                AND c.sync_batch_id = {batch_cart}
        """).fetchone()

        # Combinar recaudo nuevo + legacy
        rec_total   = float(rec_new["total"] or 0)   + float(rec_leg["total"] or 0)
        rec_cerrado = float(rec_new["cerrado"] or 0) + float(rec_leg["cerrado"] or 0)
        rec_activos = float(rec_new["activos"] or 0) + float(rec_leg["activos"] or 0)

        total_des = float(desem["total"] or 0)
        des = {
            "total":    {"desembolsado": float(desem["total"] or 0),    "recaudado": rec_total,   "pct": None},
            "cerrado":  {"desembolsado": float(desem["cerrado"] or 0),  "recaudado": rec_cerrado},
            "activos":  {"desembolsado": float(desem["activos"] or 0),  "recaudado": rec_activos},
        }
        des["cerrado"]["pct"] = (des["cerrado"]["desembolsado"] / total_des * 100.0) if total_des > 0 else 0
        des["activos"]["pct"] = (des["activos"]["desembolsado"] / total_des * 100.0) if total_des > 0 else 0
        # Desglose Nueva / Histórica para cada fila — la UI lo muestra debajo
        # del valor para que la líder vea cómo se distribuye.
        des["recaudado_breakdown"] = {
            "total":   {"nueva": float(rec_new["total"] or 0),   "legacy": float(rec_leg["total"] or 0)},
            "cerrado": {"nueva": float(rec_new["cerrado"] or 0), "legacy": float(rec_leg["cerrado"] or 0)},
            "activos": {"nueva": float(rec_new["activos"] or 0), "legacy": float(rec_leg["activos"] or 0)},
            # Diagnóstico: pagos legacy que no encuentran su crédito en cartera.
            # Si este número es alto, las cuentas de pagos_legacy e id_prestamo
            # vs credits.cuenta tienen un formato distinto que no detecta la
            # normalización TRIM/LTRIM. La UI lo puede surfacar para acción.
            "legacy_sin_match": float(rec_leg["sin_match"] or 0),
        }
        # Desglose Vr Desembolsado por plataforma.
        # 'nueva' = cuentas marcadas en cartera_nueva_cuentas (último upload incremental).
        # 'legacy' = resto = total - nueva.
        des["desembolsado_breakdown"] = {
            "total":   {"nueva": float(desem["total_nueva"] or 0),   "legacy": float(desem["total"] or 0)   - float(desem["total_nueva"] or 0)},
            "cerrado": {"nueva": float(desem["cerrado_nueva"] or 0), "legacy": float(desem["cerrado"] or 0) - float(desem["cerrado_nueva"] or 0)},
            "activos": {"nueva": float(desem["activos_nueva"] or 0), "legacy": float(desem["activos"] or 0) - float(desem["activos_nueva"] or 0)},
        }
        return des
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


@router.get("/top-clientes")
def top_clientes(
    request: Request,
    user=Depends(require_superadmin),
    n: int = Query(20, ge=1, le=100),
):
    """Top N clientes por saldo capital ACTIVO con ID + nombre completos.

    Habeas Data: solo superadmin. Se loguea un evento credit_reveal con el
    listado de IDs revelados (o hash + count si N>50) para trazabilidad.
    Trabaja solo sobre el batch activo (último upload exitoso de cartera).
    """
    ip = get_client_ip(request)
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT identificacion, cliente, saldo_capital, valor_credito, "
            "       dias_mora, calificacion, linea, aliado "
            "FROM credits WHERE estado='ACTIVO' AND sync_batch_id = "
            "(SELECT id FROM sync_logs WHERE source='manual_upload' AND status='success' "
            " ORDER BY id DESC LIMIT 1)"
        ).fetchall()
    finally:
        conn.close()

    # Agregar por identificación (decifrada). Un cliente puede tener varios créditos.
    by_ident = {}
    for r in rows:
        ident = decrypt(r["identificacion"])
        cli = decrypt(r["cliente"])
        if not ident:
            continue
        agg = by_ident.setdefault(ident, {
            "identificacion": ident,
            "cliente": cli or "—",
            "n_creditos": 0,
            "saldo_capital": 0.0,
            "valor_credito": 0.0,
            "saldo_mora_30": 0.0,
            "max_mora": 0,
            "lineas": set(),
            "calificaciones": set(),
        })
        agg["n_creditos"] += 1
        saldo = float(r["saldo_capital"] or 0)
        agg["saldo_capital"] += saldo
        agg["valor_credito"] += float(r["valor_credito"] or 0)
        dm = int(r["dias_mora"] or 0)
        if dm > 30:
            agg["saldo_mora_30"] += saldo  # política Fideseguros
        if dm > agg["max_mora"]:
            agg["max_mora"] = dm
        if r["linea"]:
            agg["lineas"].add(r["linea"])
        if r["calificacion"]:
            agg["calificaciones"].add((r["calificacion"] or "").strip())

    # Ordenar por saldo desc y tomar top N
    top = sorted(by_ident.values(), key=lambda x: -x["saldo_capital"])[:n]

    # Convertir sets a listas para serialización JSON
    for t in top:
        t["lineas"] = sorted(t["lineas"])
        t["calificaciones"] = sorted(c for c in t["calificaciones"] if c)

    # Audit: registrar los IDs revelados. Si son muchos, hash + count.
    revealed_ids = [t["identificacion"] for t in top]
    if len(revealed_ids) <= 50:
        ids_str = ",".join(str(i) for i in revealed_ids)
    else:
        h = hashlib.sha256(",".join(str(i) for i in revealed_ids).encode()).hexdigest()[:16]
        ids_str = f"hash={h} count={len(revealed_ids)}"
    log_audit(
        user["user_id"], user["username"], "credit_reveal",
        f"top_clientes n={n} ids={ids_str}", ip
    )
    return top


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
