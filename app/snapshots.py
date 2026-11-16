"""Foto mensual de la cartera (tabla cartera_snapshots).

POR QUÉ EXISTE
--------------
Cada carga de cartera reemplaza el lote anterior, así que las cifras de STOCK
(saldo activo, ICV, mora) no dejaban historia: no había forma de comparar el
ICV de este mes contra el del mismo mes del año pasado.

Aquí se guarda UNA fila por mes ('YYYY-MM', hora Colombia). Cada carga de
cartera hace UPSERT del mes en curso, de modo que la ÚLTIMA carga de cada mes
queda como cierre de ese mes.

DEFINICIONES (las mismas de app/routes/resumen.py y credits.py):
  - activos        = estado = 'ACTIVO'
  - saldo_activo   = SUM(saldo_capital) de los ACTIVO
  - mora 30 (ICV)  = ACTIVO con dias_mora > 30
  - mora 90 (NPL)  = ACTIVO con dias_mora > 90
  - icv_pct        = mora30_saldo / saldo_activo * 100
  - npl_pct        = mora90_saldo / saldo_activo * 100
Solo agregados: aquí no entra PII.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

from app.database import get_db

_log = logging.getLogger("fide.snapshots")

# Colombia no tiene horario de verano: UTC-5 fijo.
_TZ_COLOMBIA = timezone(timedelta(hours=-5))

CAMPOS_NUMERICOS = (
    "total", "activos", "saldo_activo", "mora30_count", "mora30_saldo",
    "mora90_saldo", "icv_pct", "npl_pct", "valor_desembolsado_mes",
)


def ahora_colombia() -> datetime:
    return datetime.now(_TZ_COLOMBIA)


def lote_cartera_vigente(conn) -> int | None:
    """id del último lote exitoso de cartera (el que ve todo el dashboard)."""
    r = conn.execute(
        "SELECT id FROM sync_logs WHERE source='manual_upload' AND status='success' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return r[0] if r else None


def calcular_cifras_cartera(conn, batch_id: int, mes: str) -> dict | None:
    """Cifras de stock del lote `batch_id`. None si el lote no tiene créditos."""
    r = conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN estado='ACTIVO' THEN 1 ELSE 0 END) AS activos, "
        "SUM(CASE WHEN estado='ACTIVO' THEN saldo_capital ELSE 0 END) AS saldo_activo, "
        "SUM(CASE WHEN estado='ACTIVO' AND COALESCE(dias_mora,0)>30 THEN 1 ELSE 0 END) AS mora30_count, "
        "SUM(CASE WHEN estado='ACTIVO' AND COALESCE(dias_mora,0)>30 THEN saldo_capital ELSE 0 END) AS mora30_saldo, "
        "SUM(CASE WHEN estado='ACTIVO' AND COALESCE(dias_mora,0)>90 THEN saldo_capital ELSE 0 END) AS mora90_saldo, "
        # fecha_desembolso se normaliza a YYYY-MM-DD al ingerir → prefijo = mes.
        "SUM(CASE WHEN substr(COALESCE(fecha_desembolso,''),1,7)=? THEN valor_credito ELSE 0 END) AS desembolsado "
        "FROM credits WHERE sync_batch_id=?", (mes, batch_id)
    ).fetchone()
    total = r[0] or 0
    if not total:
        return None
    saldo = float(r[2] or 0)
    mora30, mora90 = float(r[4] or 0), float(r[5] or 0)

    def _desglose(col_sql: str) -> dict:
        out = {}
        for g in conn.execute(
            f"SELECT COALESCE(NULLIF(TRIM({col_sql}),''),'(sin dato)') AS k, COUNT(*) AS activos, "
            "COALESCE(SUM(saldo_capital),0) AS saldo, "
            "COALESCE(SUM(CASE WHEN COALESCE(dias_mora,0)>30 THEN saldo_capital ELSE 0 END),0) AS mora30_saldo "
            "FROM credits WHERE sync_batch_id=? AND estado='ACTIVO' GROUP BY k ORDER BY k",
            (batch_id,)
        ):
            out[str(g[0])] = {"activos": g[1], "saldo": float(g[2] or 0),
                              "mora30_saldo": float(g[3] or 0)}
        return out

    return {
        "total": total,
        "activos": r[1] or 0,
        "saldo_activo": saldo,
        "mora30_count": r[3] or 0,
        "mora30_saldo": mora30,
        "mora90_saldo": mora90,
        "icv_pct": round(mora30 / saldo * 100, 4) if saldo > 0 else 0.0,
        "npl_pct": round(mora90 / saldo * 100, 4) if saldo > 0 else 0.0,
        "valor_desembolsado_mes": float(r[6] or 0),
        "por_linea": _desglose("linea"),
        "por_calificacion": _desglose("calificacion"),
    }


def guardar_snapshot_cartera(conn, batch_id: int | None) -> dict | None:
    """Calcula la foto del lote `batch_id` y hace UPSERT del mes en curso.

    Devuelve la foto guardada (o None si no hay lote / el lote está vacío).
    No hace commit: usa la transacción de `conn` (get_db() confirma al salir).
    """
    if not batch_id:
        return None
    ahora = ahora_colombia()
    mes = ahora.strftime("%Y-%m")
    cifras = calcular_cifras_cartera(conn, batch_id, mes)
    if cifras is None:
        return None
    foto = {"mes": mes, "fecha_corte": ahora.strftime("%Y-%m-%d"), **cifras,
            "sync_batch_id": batch_id}
    conn.execute(
        "INSERT INTO cartera_snapshots (mes, fecha_corte, total, activos, saldo_activo, "
        "mora30_count, mora30_saldo, mora90_saldo, icv_pct, npl_pct, valor_desembolsado_mes, "
        "por_linea, por_calificacion, sync_batch_id, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(mes) DO UPDATE SET fecha_corte=excluded.fecha_corte, total=excluded.total, "
        "activos=excluded.activos, saldo_activo=excluded.saldo_activo, "
        "mora30_count=excluded.mora30_count, mora30_saldo=excluded.mora30_saldo, "
        "mora90_saldo=excluded.mora90_saldo, icv_pct=excluded.icv_pct, npl_pct=excluded.npl_pct, "
        "valor_desembolsado_mes=excluded.valor_desembolsado_mes, por_linea=excluded.por_linea, "
        "por_calificacion=excluded.por_calificacion, sync_batch_id=excluded.sync_batch_id, "
        "updated_at=excluded.updated_at",
        (foto["mes"], foto["fecha_corte"], foto["total"], foto["activos"], foto["saldo_activo"],
         foto["mora30_count"], foto["mora30_saldo"], foto["mora90_saldo"], foto["icv_pct"],
         foto["npl_pct"], foto["valor_desembolsado_mes"],
         json.dumps(foto["por_linea"], ensure_ascii=False),
         json.dumps(foto["por_calificacion"], ensure_ascii=False), batch_id)
    )
    return foto


def backfill_snapshot_cartera() -> None:
    """Al arrancar: si la tabla está vacía y hay cartera vigente, crea la foto
    del mes actual (backfill mínimo). Nunca tumba el arranque."""
    try:
        with get_db() as conn:
            if conn.execute("SELECT 1 FROM cartera_snapshots LIMIT 1").fetchone():
                return
            foto = guardar_snapshot_cartera(conn, lote_cartera_vigente(conn))
            if foto:
                _log.info("Foto mensual de cartera inicial creada (%s)", foto["mes"])
    except Exception:
        _log.exception("backfill de foto mensual de cartera falló (no fatal)")


def listar_snapshots(conn, meses: int = 24) -> list[dict]:
    """Últimos `meses` meses con foto, en orden ascendente, JSON ya parseado."""
    rows = conn.execute(
        "SELECT mes, fecha_corte, total, activos, saldo_activo, mora30_count, mora30_saldo, "
        "mora90_saldo, icv_pct, npl_pct, valor_desembolsado_mes, por_linea, por_calificacion, "
        "sync_batch_id, updated_at FROM cartera_snapshots ORDER BY mes DESC LIMIT ?", (meses,)
    ).fetchall()
    out = []
    for r in reversed(rows):
        d = dict(r)
        for k in ("por_linea", "por_calificacion"):
            try:
                d[k] = json.loads(d[k]) if d[k] else {}
            except ValueError:
                d[k] = {}
        out.append(d)
    return out
