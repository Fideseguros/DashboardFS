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
def resumen_ejecutivo(_user=Depends(require_auth)):
    """Cifras consolidadas de todos los modulos para el home."""
    conn = get_connection()
    try:
        out = {"actualizaciones": {}, "alertas": []}

        # ---------- Cartera ----------
        b_cart, cart_when = _batch(conn, "manual_upload")
        out["actualizaciones"]["cartera"] = cart_when
        if b_cart:
            row = conn.execute(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN estado='ACTIVO' THEN 1 ELSE 0 END) as activos, "
                "SUM(CASE WHEN estado='ACTIVO' THEN saldo_capital ELSE 0 END) as saldo_activo, "
                "SUM(CASE WHEN estado='ACTIVO' AND COALESCE(dias_mora,0)>30 THEN 1 ELSE 0 END) as mora_count, "
                "SUM(CASE WHEN estado='ACTIVO' AND COALESCE(dias_mora,0)>30 THEN saldo_capital ELSE 0 END) as mora_saldo "
                "FROM credits WHERE sync_batch_id=?", (b_cart,)
            ).fetchone()
            d = dict(row)
            saldo_act = d["saldo_activo"] or 0
            icv = (d["mora_saldo"] / saldo_act * 100) if saldo_act > 0 else 0
            out["cartera"] = {
                "total": d["total"] or 0, "activos": d["activos"] or 0,
                "saldo_activo": saldo_act, "mora_count": d["mora_count"] or 0,
                "mora_saldo": d["mora_saldo"] or 0, "icv_pct": round(icv, 2),
            }
            if icv > 8:
                out["alertas"].append({"nivel": "alta",
                    "texto": f"ICV en {icv:.1f}% - por encima del umbral recomendado (5%)."})
            elif icv > 5:
                out["alertas"].append({"nivel": "media",
                    "texto": f"ICV en {icv:.1f}% - vigilar, cerca del limite."})
        else:
            out["cartera"] = None

        # ---------- Recaudo ----------
        b_rec, rec_when = _batch(conn, "recaudo_upload")
        out["actualizaciones"]["recaudo"] = rec_when
        if b_rec:
            # Mismo criterio que /api/recaudo/summary: solo movimientos 'Pago%'
            # son recaudo (excluye condonaciones, notas débito, reintegros y
            # cheques devueltos). Si el home usara SUM(total) crudo, mostraría
            # una cifra distinta a la pestaña Recaudo para el mismo archivo.
            row = conn.execute(
                "SELECT SUM(CASE WHEN UPPER(COALESCE(tipo_mvto,'')) LIKE 'PAGO%' THEN 1 ELSE 0 END) as n, "
                "COALESCE(SUM(CASE WHEN UPPER(COALESCE(tipo_mvto,'')) LIKE 'PAGO%' THEN total ELSE 0 END),0) as total, "
                "COALESCE(SUM(CASE WHEN UPPER(COALESCE(tipo_mvto,'')) LIKE 'PAGO%' THEN capital ELSE 0 END),0) as capital "
                "FROM pagos WHERE sync_batch_id=?",
                (b_rec,)
            ).fetchone()
            out["recaudo"] = {"movimientos": row["n"] or 0, "total": row["total"], "capital": row["capital"]}
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
            rows = conn.execute(
                "SELECT estado, COUNT(*) as n FROM solicitudes WHERE sync_batch_id=? GROUP BY estado",
                (b_sol,)
            ).fetchall()
            desem = en_est = anuladas = 0
            for r in rows:
                e = _normalize_estado(r["estado"], "nueva")
                if e == "DESEMBOLSADA":
                    desem += r["n"]
                elif e == "EN ESTUDIO":
                    en_est += r["n"]
                elif e == "ANULADA":
                    anuladas += r["n"]
            total_sol = sum(r["n"] for r in rows)
            out["solicitudes"] = {"total": total_sol, "desembolsadas": desem,
                                  "en_estudio": en_est, "anuladas": anuladas,
                                  "plataforma": "nueva"}
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
            out["juridico"] = {"total": row["total"] or 0, "probables": row["probables"] or 0}
        else:
            out["juridico"] = None

        # ---------- Saldo Cartera (ultimo snapshot) ----------
        sc = conn.execute(
            "SELECT snapshot_date, saldo_cartera FROM saldo_cartera_snapshots "
            "ORDER BY snapshot_date DESC, id DESC LIMIT 1"
        ).fetchone()
        from app.routes.saldo_cartera import _AJUSTE_CARTERA
        if sc:
            out["saldo_cartera"] = {
                "fecha": sc["snapshot_date"],
                "valor": (sc["saldo_cartera"] or 0) + _AJUSTE_CARTERA,
            }
            out["actualizaciones"]["saldo_cartera"] = sc["snapshot_date"]
        else:
            out["saldo_cartera"] = {"fecha": None, "valor": _AJUSTE_CARTERA}
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
