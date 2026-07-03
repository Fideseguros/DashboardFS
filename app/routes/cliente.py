"""Ficha Cliente 360: reúne en una vista toda la información de un cliente
por su identificación — crédito(s), pagos (nuevo + legacy), solicitud y
proceso jurídico. Elimina la fricción de buscar la misma cédula en 4-5
pestañas distintas.

La identificación está cifrada con IV random en todas las tablas, así que
no hay WHERE indexed: se descifra y compara normalizada (igual que
habeas_data). Es una búsqueda puntual bajo demanda.
"""
import re
import logging
from fastapi import APIRouter, Depends, Query, Request, HTTPException
from app.database import get_connection
from app.auth.middleware import require_auth
from app.crypto import decrypt, mask_identificacion, mask_cliente
from app.audit import log_audit, get_client_ip

router = APIRouter(prefix="/api/cliente", tags=["cliente"])
_log = logging.getLogger("fide.cliente")


def _norm_id(s):
    """Solo dígitos, sin ceros a la izquierda (para cruzar variantes)."""
    if not s:
        return ""
    digits = re.sub(r'\D', '', str(s))
    return digits.lstrip('0') or digits


def _active_batch(conn, source):
    r = conn.execute(
        "SELECT id FROM sync_logs WHERE source=? AND status='success' "
        "ORDER BY id DESC LIMIT 1", (source,)
    ).fetchone()
    return r["id"] if r else None


@router.get("/ficha")
def ficha_cliente(request: Request, identificacion: str = Query(..., min_length=3),
                  user=Depends(require_auth)):
    """Devuelve la ficha 360 de un cliente buscando por identificación.

    Roles: superadmin ve identificación/nombre en plaintext; otros roles
    enmascarado. Se audita la consulta (acceso a PII consolidada)."""
    is_super = user.get("role") == "superadmin"
    target = _norm_id(identificacion)
    if not target:
        raise HTTPException(status_code=400, detail="Identificación inválida.")

    conn = get_connection()
    try:
        b_cart = _active_batch(conn, "manual_upload")
        b_rec = _active_batch(conn, "recaudo_upload")
        b_sol = _active_batch(conn, "solicitudes_upload")
        b_jur = _active_batch(conn, "juridico_upload")

        nombre_real = None  # se toma del primer registro con match

        # ---------- Créditos (cartera activa) ----------
        creditos = []
        if b_cart:
            for r in conn.execute(
                "SELECT identificacion, cliente, cuenta, estado, linea, valor_credito, "
                "saldo_capital, valor_cuota, dias_mora, maxima_mora, calificacion, "
                "tasa_efectiva, cuotas_pagadas, cuotas_pactadas, fecha_desembolso, "
                "fecha_vencimiento, fecha_ult_pago, aliado, ciudad "
                "FROM credits WHERE sync_batch_id=?", (b_cart,)
            ):
                if _norm_id(decrypt(r["identificacion"])) != target:
                    continue
                if nombre_real is None:
                    nombre_real = decrypt(r["cliente"])
                d = dict(r)
                d.pop("identificacion", None); d.pop("cliente", None)
                creditos.append(d)

        # ---------- Pagos nuevos (recaudo) ----------
        pagos = []
        pagos_resumen = {"n": 0, "capital": 0.0, "interes": 0.0, "mora": 0.0, "total": 0.0}
        if b_rec:
            for r in conn.execute(
                "SELECT identificacion, cliente, fecha_movimiento, tipo_mvto, cuenta, "
                "capital, interes_corriente, interes_mora, total "
                "FROM pagos WHERE sync_batch_id=?", (b_rec,)
            ):
                if _norm_id(decrypt(r["identificacion"])) != target:
                    continue
                if nombre_real is None:
                    nombre_real = decrypt(r["cliente"])
                pagos_resumen["n"] += 1
                pagos_resumen["capital"] += r["capital"] or 0
                pagos_resumen["interes"] += r["interes_corriente"] or 0
                pagos_resumen["mora"] += r["interes_mora"] or 0
                pagos_resumen["total"] += r["total"] or 0
                d = dict(r); d.pop("identificacion", None); d.pop("cliente", None)
                pagos.append(d)
            # Últimos 20 movimientos por fecha desc
            pagos.sort(key=lambda x: x.get("fecha_movimiento") or "", reverse=True)
            pagos = pagos[:20]

        # ---------- Pagos legacy (histórico) ----------
        pagos_legacy = []
        for r in conn.execute(
            "SELECT identificacion, nombre, id_prestamo, num_pagos, valor_pago_total, "
            "interes_mora_total, fecha_primer_pago, fecha_ultimo_pago, prestamo_cancelado "
            "FROM pagos_legacy"
        ):
            if _norm_id(decrypt(r["identificacion"])) != target:
                continue
            if nombre_real is None:
                nombre_real = decrypt(r["nombre"])
            d = dict(r); d.pop("identificacion", None); d.pop("nombre", None)
            pagos_legacy.append(d)

        # ---------- Solicitudes (nueva) ----------
        solicitudes = []
        if b_sol:
            for r in conn.execute(
                "SELECT identificacion, solicitante, solicitud, linea, valor, estado, "
                "paso_ruta, oficina, fecha_solicitud "
                "FROM solicitudes WHERE sync_batch_id=?", (b_sol,)
            ):
                if _norm_id(decrypt(r["identificacion"])) != target:
                    continue
                if nombre_real is None:
                    nombre_real = decrypt(r["solicitante"])
                d = dict(r); d.pop("identificacion", None); d.pop("solicitante", None)
                solicitudes.append(d)

        # ---------- Solicitudes legacy ----------
        for r in conn.execute(
            "SELECT identificacion, nombre_completo, id_solicitud, producto, estado, "
            "monto, fecha_solicitud FROM solicitudes_legacy"
        ):
            if _norm_id(decrypt(r["identificacion"])) != target:
                continue
            if nombre_real is None:
                nombre_real = decrypt(r["nombre_completo"])
            d = dict(r); d.pop("identificacion", None); d.pop("nombre_completo", None)
            d["_legacy"] = True
            solicitudes.append(d)

        # ---------- Proceso jurídico ----------
        juridico = []
        if b_jur:
            for r in conn.execute(
                "SELECT identificacion, nombre, naturaleza_litigio, avance, "
                "probabilidad, medida_cautelar, juzgado "
                "FROM procesos_juridicos WHERE sync_batch_id=?", (b_jur,)
            ):
                if _norm_id(decrypt(r["identificacion"])) != target:
                    continue
                if nombre_real is None:
                    nombre_real = decrypt(r["nombre"])
                d = dict(r); d.pop("identificacion", None); d.pop("nombre", None)
                juridico.append(d)

        encontrado = bool(creditos or pagos or pagos_legacy or solicitudes or juridico)

        # Audit: consulta de ficha consolidada (acceso a PII de un titular)
        ip = get_client_ip(request) or "unknown"
        log_audit(user["user_id"], user["username"], "cliente_ficha",
                  f"id={identificacion} encontrado={encontrado}", ip)

        nombre_out = (nombre_real or "") if is_super else mask_cliente(nombre_real or "")
        ident_out = identificacion if is_super else mask_identificacion(identificacion)

        return {
            "encontrado": encontrado,
            "identificacion": ident_out,
            "nombre": nombre_out,
            "creditos": creditos,
            "pagos": pagos,
            "pagos_resumen": pagos_resumen,
            "pagos_legacy": pagos_legacy,
            "solicitudes": solicitudes,
            "juridico": juridico,
        }
    finally:
        conn.close()
