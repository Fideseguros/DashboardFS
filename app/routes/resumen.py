"""Resumen Ejecutivo: consolida en un solo endpoint las cifras clave de
todos los modulos + que se actualizo y cuando. Todo con SQL agregado
(sin descifrar PII) -> respuesta rapida para el home de la gerente.
"""
import logging
from fastapi import APIRouter, Depends
from app.database import get_connection
from app.auth.middleware import require_auth

router = APIRouter(prefix="/api/resumen", tags=["resumen"])
_log = logging.getLogger("fide.resumen")


def _batch(conn, source):
    r = conn.execute(
        "SELECT id, completed_at FROM sync_logs WHERE source=? AND status='success' "
        "ORDER BY id DESC LIMIT 1", (source,)
    ).fetchone()
    return (r["id"], r["completed_at"]) if r else (None, None)


@router.get("/ejecutivo")
def resumen_ejecutivo(linea: str | None = None, aliado: str | None = None,
                      ciudad: str | None = None, calificacion: str | None = None,
                      desde: str | None = None, hasta: str | None = None,
                      _user=Depends(require_auth)):
    """Cifras consolidadas de todos los modulos para el home.

    FILTROS (pedido gerencia jul-2026): acepta los mismos filtros de la
    pestaña Cartera. Cada módulo honra los que su tabla PUEDE aplicar
    (verificado con los archivos reales que los valores coinciden entre
    módulos — aliado de recaudo es subconjunto del de cartera, y línea usa
    los mismos 4 valores en cartera/recaudo/solicitudes):
      - cartera:     linea, aliado, ciudad, calificacion, fecha (desembolso)
      - recaudo:     linea (col linea_credito), aliado, fecha (movimiento)
      - solicitudes: linea, fecha (solicitud)
      - juridico y saldo_cartera: NINGUNO (vienen de archivos sin esas
        dimensiones) — siempre globales.
    La FECHA (desde/hasta, YYYY-MM-DD) filtra por la fecha natural de cada
    módulo: en cartera es fecha_desembolso (misma semántica que el filtro de
    la pestaña Cartera), en recaudo fecha_movimiento y en solicitudes
    fecha_solicitud. Las tres columnas están normalizadas a YYYY-MM-DD al
    ingerir, así que la comparación de strings es un rango correcto.
    Cada bloque expone 'aplica': [filtros que ese módulo honra], para que la
    UI marque las tarjetas globales/parciales y no aparente un filtrado que
    no ocurrió.
    """
    from datetime import date as _date
    linea = (linea or "").strip() or None
    aliado = (aliado or "").strip() or None
    ciudad = (ciudad or "").strip() or None
    calificacion = (calificacion or "").strip() or None

    def _fecha_valida(s):
        """Solo fechas ISO reales (rechaza '2026-13-99', no solo el formato).
        El input type=date del navegador ya lo garantiza; esto es defensa
        del lado servidor."""
        s = (s or "").strip()
        try:
            return _date.fromisoformat(s).isoformat()
        except ValueError:
            return None
    desde = _fecha_valida(desde)
    hasta = _fecha_valida(hasta)
    filtros_activos = {k: v for k, v in
                       [("linea", linea), ("aliado", aliado),
                        ("ciudad", ciudad), ("calificacion", calificacion)] if v}
    if desde or hasta:
        filtros_activos["fecha"] = f"{desde or '…'} → {hasta or '…'}"

    def _fecha_conds(col, conds, params):
        """Añade el rango de fecha sobre la columna natural del módulo."""
        if desde:
            conds.append(f"{col} >= ?"); params.append(desde)
        if hasta:
            conds.append(f"{col} <= ?"); params.append(hasta)
    conn = get_connection()
    try:
        out = {"actualizaciones": {}, "alertas": [],
               "filtros": filtros_activos}

        # ---------- Cartera ----------
        b_cart, cart_when = _batch(conn, "manual_upload")
        out["actualizaciones"]["cartera"] = cart_when
        if b_cart:
            conds, params = ["sync_batch_id=?"], [b_cart]
            if linea:
                conds.append("linea=?"); params.append(linea)
            if aliado:
                conds.append("aliado=?"); params.append(aliado)
            if ciudad:
                conds.append("ciudad=?"); params.append(ciudad)
            if calificacion:
                conds.append("TRIM(calificacion)=?"); params.append(calificacion)
            _fecha_conds("fecha_desembolso", conds, params)
            row = conn.execute(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN estado='ACTIVO' THEN 1 ELSE 0 END) as activos, "
                "SUM(CASE WHEN estado='ACTIVO' THEN saldo_capital ELSE 0 END) as saldo_activo, "
                "SUM(CASE WHEN estado='ACTIVO' AND COALESCE(dias_mora,0)>30 THEN 1 ELSE 0 END) as mora_count, "
                "SUM(CASE WHEN estado='ACTIVO' AND COALESCE(dias_mora,0)>30 THEN saldo_capital ELSE 0 END) as mora_saldo "
                f"FROM credits WHERE {' AND '.join(conds)}", params
            ).fetchone()
            d = dict(row)
            saldo_act = d["saldo_activo"] or 0
            icv = ((d["mora_saldo"] or 0) / saldo_act * 100) if saldo_act > 0 else 0
            out["cartera"] = {
                "total": d["total"] or 0, "activos": d["activos"] or 0,
                "saldo_activo": saldo_act, "mora_count": d["mora_count"] or 0,
                "mora_saldo": d["mora_saldo"] or 0, "icv_pct": round(icv, 2),
                "aplica": ["linea", "aliado", "ciudad", "calificacion", "fecha"],
            }
            # Alertas de ICV: si hay filtros, la alerta describe el SEGMENTO
            # filtrado, no la cartera total — se aclara en el texto.
            scope = " (con los filtros aplicados)" if filtros_activos else ""
            if icv > 8:
                out["alertas"].append({"nivel": "alta",
                    "texto": f"ICV en {icv:.1f}%{scope} - por encima del umbral recomendado (5%)."})
            elif icv > 5:
                out["alertas"].append({"nivel": "media",
                    "texto": f"ICV en {icv:.1f}%{scope} - vigilar, cerca del limite."})
        else:
            out["cartera"] = None

        # Opciones para poblar los selects del filtro en el home (sin PII:
        # son nombres de línea/aliado/ciudad y letras de calificación).
        # Siempre del batch completo, para que el select no se encoja al filtrar.
        if b_cart:
            def _opts(col):
                return [r[0] for r in conn.execute(
                    f"SELECT DISTINCT {col} FROM credits WHERE sync_batch_id=? "
                    f"AND {col} IS NOT NULL AND TRIM({col})!='' ORDER BY {col}",
                    (b_cart,))]
            out["filtro_opciones"] = {
                "linea": _opts("linea"), "aliado": _opts("aliado"),
                "ciudad": _opts("ciudad"), "calificacion": _opts("TRIM(calificacion)"),
            }

        # ---------- Recaudo ----------
        b_rec, rec_when = _batch(conn, "recaudo_upload")
        out["actualizaciones"]["recaudo"] = rec_when
        if b_rec:
            # Mismo criterio que /api/recaudo/summary: solo movimientos 'Pago%'
            # son recaudo (excluye condonaciones, notas débito, reintegros y
            # cheques devueltos). Si el home usara SUM(total) crudo, mostraría
            # una cifra distinta a la pestaña Recaudo para el mismo archivo.
            # Filtros que pagos SÍ puede honrar: linea (col linea_credito) y
            # aliado. Ciudad/calificación no existen aquí.
            conds, params = ["sync_batch_id=?"], [b_rec]
            if linea:
                conds.append("linea_credito=?"); params.append(linea)
            if aliado:
                conds.append("aliado=?"); params.append(aliado)
            _fecha_conds("fecha_movimiento", conds, params)
            row = conn.execute(
                "SELECT SUM(CASE WHEN UPPER(COALESCE(tipo_mvto,'')) LIKE 'PAGO%' THEN 1 ELSE 0 END) as n, "
                "COALESCE(SUM(CASE WHEN UPPER(COALESCE(tipo_mvto,'')) LIKE 'PAGO%' THEN total ELSE 0 END),0) as total, "
                "COALESCE(SUM(CASE WHEN UPPER(COALESCE(tipo_mvto,'')) LIKE 'PAGO%' THEN capital ELSE 0 END),0) as capital "
                f"FROM pagos WHERE {' AND '.join(conds)}",
                params
            ).fetchone()
            out["recaudo"] = {"movimientos": row["n"] or 0, "total": row["total"], "capital": row["capital"],
                              "aplica": ["linea", "aliado", "fecha"]}
        else:
            out["recaudo"] = None

        # ---------- Solicitudes (solo plataforma nueva) ----------
        # Usa la MISMA _normalize_estado que /api/solicitudes/combined. Antes
        # había aquí una tercera copia de las reglas con fuzzy matching propio:
        # coincidía por casualidad con la pestaña, pero cualquier estado nuevo
        # de la plataforma haría divergir el home de la pestaña sin aviso
        # (p.ej. le faltaba 'CENTRAL' y no clasificaba las anuladas).
        # 'plataforma' se expone para que la UI pueda aclarar que este conteo
        # NO incluye el histórico legacy (la pestaña Solicitudes sí lo suma).
        b_sol, sol_when = _batch(conn, "solicitudes_upload")
        out["actualizaciones"]["solicitudes"] = sol_when
        if b_sol:
            from app.routes.extras import _normalize_estado
            # Filtro que solicitudes SÍ puede honrar: linea (mismos 4 valores
            # de producto que cartera, verificado con los archivos reales).
            conds, params = ["sync_batch_id=?"], [b_sol]
            if linea:
                conds.append("linea=?"); params.append(linea)
            _fecha_conds("fecha_solicitud", conds, params)
            rows = conn.execute(
                f"SELECT estado, COUNT(*) as n FROM solicitudes WHERE {' AND '.join(conds)} GROUP BY estado",
                params
            ).fetchall()
            desem = en_est = anuladas = negadas = 0
            for r in rows:
                e = _normalize_estado(r["estado"], "nueva")
                if e == "DESEMBOLSADA":
                    desem += r["n"]
                elif e == "EN ESTUDIO":
                    en_est += r["n"]
                elif e == "ANULADA":
                    anuladas += r["n"]
                elif e == "NEGADA":
                    negadas += r["n"]
            total_sol = sum(r["n"] for r in rows)
            out["solicitudes"] = {"total": total_sol, "desembolsadas": desem,
                                  "en_estudio": en_est, "anuladas": anuladas,
                                  "negadas": negadas, "plataforma": "nueva",
                                  "aplica": ["linea", "fecha"]}
        else:
            out["solicitudes"] = None

        # ---------- Juridico ----------
        b_jur, jur_when = _batch(conn, "juridico_upload")
        out["actualizaciones"]["juridico"] = jur_when
        if b_jur:
            row = conn.execute(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN LOWER(probabilidad) LIKE '%probable%' THEN 1 ELSE 0 END) as probables "
                "FROM procesos_juridicos WHERE sync_batch_id=?", (b_jur,)
            ).fetchone()
            # aplica vacío: el informe de procesos no trae línea/aliado/ciudad,
            # así que esta tarjeta es SIEMPRE global aunque haya filtros.
            out["juridico"] = {"total": row["total"] or 0, "probables": row["probables"] or 0,
                               "aplica": []}
        else:
            out["juridico"] = None

        # ---------- Saldo Cartera (ultimo snapshot) ----------
        sc = conn.execute(
            "SELECT snapshot_date, saldo_cartera FROM saldo_cartera_snapshots "
            "ORDER BY snapshot_date DESC, id DESC LIMIT 1"
        ).fetchone()
        from app.routes.saldo_cartera import _AJUSTE_CARTERA
        if sc:
            # aplica vacío: el Resumen Estado Cuenta es un total del archivo,
            # sin desglose por línea/aliado — siempre global.
            out["saldo_cartera"] = {
                "fecha": sc["snapshot_date"],
                "valor": (sc["saldo_cartera"] or 0) + _AJUSTE_CARTERA,
                "aplica": [],
            }
            out["actualizaciones"]["saldo_cartera"] = sc["snapshot_date"]
        else:
            out["saldo_cartera"] = {"fecha": None, "valor": _AJUSTE_CARTERA, "aplica": []}
            out["actualizaciones"]["saldo_cartera"] = None

        # ---------- Tendencia de saldo (ultimos 2 snapshots) ----------
        # El % debe calcularse sobre LA MISMA BASE que se muestra en la tarjeta
        # (línea ~113: saldo + ajuste). Antes se calculaba sobre el saldo crudo
        # mientras la tarjeta mostraba el ajustado, así que el "▲ X%" no
        # correspondía al número que tenía al lado: el ajuste es constante en
        # ambos snapshots, y al no estar en el denominador exageraba la
        # variación (el ajuste es ~3,8% del saldo capital activo).
        snaps = conn.execute(
            "SELECT saldo_cartera FROM saldo_cartera_snapshots "
            "ORDER BY snapshot_date DESC, id DESC LIMIT 2"
        ).fetchall()
        if len(snaps) == 2:
            hoy = (snaps[0]["saldo_cartera"] or 0) + _AJUSTE_CARTERA
            ant = (snaps[1]["saldo_cartera"] or 0) + _AJUSTE_CARTERA
            if ant > 0:
                out["saldo_tendencia_pct"] = round((hoy - ant) / ant * 100, 1)

        return out
    finally:
        conn.close()
