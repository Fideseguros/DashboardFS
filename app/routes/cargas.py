"""Carga única de archivos: detección del tipo, validación previa e historial.

POR QUÉ EXISTE
--------------
Había 9 botones de carga, uno por reporte, y la gerente debía acertar el botón
de cada Excel. Aquí hay UNA sola puerta:

  POST /api/cargas/analizar  → dice qué archivo es y si trae algo raro. NO escribe.
  POST /api/cargas/aplicar   → ejecuta LA MISMA lógica del botón específico
                               (las funciones cargar_* / ejecutar_* de cada módulo).
  GET  /api/cargas/historial → bitácora de cargas (quién, cuándo, cuánto, variación).
  GET  /api/cargas/estado    → última carga exitosa por tipo (semáforo de frescura).
  GET  /api/cartera/snapshots→ foto mensual de la cartera (ver app/snapshots.py).

La detección es por ENCABEZADOS, nunca por el nombre del archivo. La única
excepción es la cartera: los informes de la plataforma vieja y de la nueva
traen exactamente las mismas columnas, así que ahí se mira el contenido (las
cuentas de 10+ dígitos solo existen en la plataforma nueva) y el nombre queda
como desempate.

El parseo con openpyxl es bloqueante: en estos endpoints corre con
run_in_threadpool para no congelar la app mientras se lee un Excel de 17 MB.
"""
import io
import logging
from datetime import datetime
from types import SimpleNamespace

import openpyxl
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.audit import get_client_ip, log_audit
from app.auth.middleware import require_auth, require_superadmin
from app.database import get_connection
from app.snapshots import listar_snapshots, lote_cartera_vigente
from app.sync.job import CARTERA_REQUIRE
from app.sync.upload_helpers import (
    _check_upload_file, _norm_header_cell, _parse_with_zip_xml, upload_session,
)
from app.routes import extras, financieros, saldo_cartera, sync as sync_routes

router = APIRouter(prefix="/api/cargas", tags=["cargas"])
router_cartera = APIRouter(prefix="/api/cartera", tags=["cartera"])
_log = logging.getLogger("fide.cargas")

_MSG_LEGACY = "No se pudo importar el archivo histórico. Revisa el formato y vuelve a intentarlo."

# tipo -> configuración. `source` es el de sync_logs; las dos carteras comparten
# 'manual_upload' (así consulta el dashboard el lote vigente) y se distinguen
# por sync_logs.tipo_carga. `cargar` es la función del módulo dueño del reporte.
TIPOS = {
    "cartera_vieja": {
        "etiqueta": "Cartera · base (plataforma vieja)", "source": "manual_upload",
        "max_mb": sync_routes.MAX_UPLOAD_MB, "endpoint": "/api/sync/upload-excel",
    },
    "cartera_nueva": {
        "etiqueta": "Cartera · plataforma nueva", "source": "manual_upload",
        "max_mb": sync_routes.MAX_UPLOAD_MB, "endpoint": "/api/sync/update-incremental",
    },
    "recaudo": {
        "etiqueta": "Recaudo · plataforma nueva", "source": "recaudo_upload",
        "max_mb": extras.MAX_UPLOAD_MB, "endpoint": "/api/recaudo/upload",
        "cargar": extras.cargar_recaudo, "max_filas": extras.MAX_ROWS,
    },
    "recaudo_legacy": {
        "etiqueta": "Recaudo · histórico (plataforma anterior)", "source": "recaudo_legacy_upload",
        "max_mb": extras.MAX_LEGACY_MB, "endpoint": "/api/recaudo/upload-legacy",
        "cargar": extras.cargar_recaudo_legacy, "msg_error": _MSG_LEGACY,
        "max_filas": extras.MAX_LEGACY_ROWS,
    },
    "solicitudes": {
        "etiqueta": "Solicitudes · plataforma nueva", "source": "solicitudes_upload",
        "max_mb": extras.MAX_UPLOAD_MB, "endpoint": "/api/solicitudes/upload",
        "cargar": extras.cargar_solicitudes, "max_filas": extras.MAX_ROWS,
    },
    "solicitudes_legacy": {
        "etiqueta": "Solicitudes · histórico (plataforma anterior)",
        "source": "solicitudes_legacy_upload",
        "max_mb": extras.MAX_LEGACY_MB, "endpoint": "/api/solicitudes/upload-legacy",
        "cargar": extras.cargar_solicitudes_legacy, "msg_error": _MSG_LEGACY,
        "max_filas": extras.MAX_LEGACY_ROWS,
    },
    "juridico": {
        "etiqueta": "Procesos judiciales", "source": "juridico_upload",
        "max_mb": extras.MAX_UPLOAD_MB, "endpoint": "/api/juridico/upload",
        "cargar": extras.cargar_juridico,
    },
    "financieros": {
        "etiqueta": "Estado de resultados mensual", "source": "financieros_upload",
        "max_mb": financieros.MAX_UPLOAD_MB, "endpoint": "/api/estados-financieros/upload",
        "cargar": financieros.cargar_financieros,
        "msg_error": "No se pudo importar el archivo. Revisa el formato del Estado de Resultados.",
    },
    "saldo_cartera": {
        "etiqueta": "Saldo de cartera (Resumen Estado Cuenta)", "source": "saldo_cartera_upload",
        "max_mb": saldo_cartera.MAX_UPLOAD_MB, "endpoint": "/api/saldo-cartera/upload",
        "cargar": saldo_cartera.cargar_saldo_cartera,
        "msg_error": "No se pudo importar el archivo. Revisa el formato del Resumen Estado Cuenta.",
    },
}
_MAX_MB_GLOBAL = max(t["max_mb"] for t in TIPOS.values())
_SOURCE_A_TIPO = {t["source"]: k for k, t in TIPOS.items() if t["source"] != "manual_upload"}

MSG_NO_RECONOCIDO = (
    "No reconozco este archivo por sus columnas. Acepto: Informe de cartera "
    "(plataforma vieja o nueva), Informe de pagos / recaudo (plataforma nueva "
    "o anterior), Informe de solicitudes (plataforma nueva o anterior), "
    "Informe de procesos judiciales, Estado de resultados integral mensual y "
    "Resumen Estado Cuenta (saldo de cartera). Verifica que sea el reporte "
    "original exportado de la plataforma, sin columnas borradas."
)


def _tipos_aceptados() -> list[dict]:
    return [{"tipo": k, "etiqueta": t["etiqueta"]} for k, t in TIPOS.items()]


# ============================================================
#                        DETECCIÓN
# ============================================================
# Firmas por encabezado: (tipo base, frases que deben estar TODAS, filas a
# escanear). Se comparan como check_file_signature: normalizadas y por
# 'contains'. scan=1 donde el cargador asume el encabezado en la fila 1.
# El orden importa: recaudo antes que saldo_cartera (comparten columnas de
# montos; al Resumen Estado Cuenta lo delatan sus columnas propias).
_FIRMAS = (
    ("recaudo", ("fecha movimiento", "tipo mvto", "capital prestamos"), 1),
    ("solicitudes", ("solicitud origen", "paso ruta", "hoja ruta", "solicitante"), 3),
    ("solicitudes_legacy", ("id de la solicitud", "estado de solicitud",
                            "monto de la solicitud", "originador"), 1),
    ("recaudo_legacy", ("id prestamo", "id transaccion", "valor de pago", "reversado"), 1),
    ("juridico", ("naturaleza del litigio", "juzgado", "medida cautelar"), 3),
    ("cartera", CARTERA_REQUIRE, 3),
)
_MESES_ER = ("enero", "febrero", "marzo")

# Reportes que el cargador lee POR POSICIÓN de columna. Si la plataforma
# inserta o mueve una columna, el cargador leería datos corridos sin error;
# aquí se compara el encabezado contra la posición esperada para avisar.
_POSICIONES = {
    "solicitudes": {0: "solicitud origen", 1: "solicitud", 4: "identificacion",
                    5: "solicitante", 7: "valor", 10: "estado", 14: "fecha solicitud"},
    "solicitudes_legacy": {0: "id de la solicitud", 2: "fecha de solicitud",
                           4: "numero de identificacion", 6: "nombre completo",
                           10: "estado de solicitud", 12: "fecha de desembolso",
                           13: "monto de la solicitud"},
    "recaudo_legacy": {1: "id prestamo", 3: "numero de identificacion", 14: "reversado",
                       15: "tipo de pago", 17: "valor de pago", 22: "fecha de pago"},
    "juridico": {0: "id", 1: "nombre", 5: "probabilidad", 6: "medida cautelar", 7: "juzgado"},
}


def _hoja_financieros(nombres: list[str]) -> str:
    """La misma hoja que elige financieros.parsear_estado_resultados."""
    return next((s for s in nombres if "mes" in s.lower() or "res" in s.lower()), nombres[0])


def _hojas_candidatas(content: bytes):
    """Genera (nombre_hoja, filas, es_hoja_financieros) de las hojas donde puede
    estar el reporte, en el orden en que las leerían los cargadores:
    'Cartera_Consolidado' (la que prefiere la carga de cartera), la hoja activa
    (la que leen los demás) y la hoja del Estado de Resultados.

    Si openpyxl revienta (estilos corruptos: defecto conocido del export de
    solicitudes), cae al parser XML directo, igual que read_excel()."""
    entregadas = 0
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
        try:
            nombres = wb.sheetnames
            hoja_er = _hoja_financieros(nombres)
            activa = wb.active.title if wb.active is not None else nombres[0]
            orden = []
            for n in (("Cartera_Consolidado",) if "Cartera_Consolidado" in nombres else ()) + (activa, hoja_er):
                if n not in orden:
                    orden.append(n)
            for n in orden:
                filas = list(wb[n].iter_rows(values_only=True))
                entregadas += 1
                yield n, filas, n == hoja_er
        finally:
            wb.close()
    except Exception as e:
        if entregadas:
            raise
        _log.warning("openpyxl falló (%s), usando parser XML directo", type(e).__name__)
        yield "(hoja 1, parser XML)", _parse_with_zip_xml(content), False


def _fila_firma(rows, require, scan) -> int | None:
    for i, row in enumerate(rows[:scan]):
        if not row:
            continue
        norm = [_norm_header_cell(c) for c in row if c]
        if all(any(f in h for h in norm) for f in require):
            return i
    return None


def _clasificar(rows, es_hoja_er: bool) -> tuple[str | None, int | None]:
    """(tipo base, índice de la fila de encabezados). 'cartera' aún sin subtipo."""
    if not rows:
        return None, None
    for base, require, scan in _FIRMAS:
        i = _fila_firma(rows, require, scan)
        if i is not None:
            return base, i
    # Resumen Estado Cuenta: columnas propias + montos, y NO es de movimientos.
    norm0 = [_norm_header_cell(c) for c in (rows[0] or ()) if c]
    if (any(any(s in h for h in norm0) for s in saldo_cartera.SIGNATURE_HEADERS)
            and any("capital prestamos" in h for h in norm0)
            and not any(any(m in h for h in norm0) for m in saldo_cartera.MOVEMENT_HEADERS)):
        return "saldo_cartera", 0
    # Estado de Resultados: fila con CUENTA + meses, solo en la hoja que el
    # cargador realmente lee (el libro trae otras hojas con 'CUENTA').
    if es_hoja_er:
        for i, row in enumerate(rows[:12]):
            norm = [_norm_header_cell(c) for c in (row or ()) if c]
            if "cuenta" in norm and all(m in norm for m in _MESES_ER):
                return "financieros", i
    return None, None


def _subtipo_cartera(nombre: str, rows, hdr_idx: int, hoja: str) -> dict:
    """Vieja (carga base, reemplaza todo) vs nueva (incremental).

    Los dos informes traen las MISMAS columnas, así que el encabezado no los
    distingue. Señales, de más a menos fuertes:
      1. Consolidado (hoja 'Cartera_Consolidado' o columna INTERMEDIARIO) → base.
      2. Contenido: la plataforma nueva numera cuentas con 10+ dígitos; la
         vieja nunca pasó de 4. Encontrarlas prueba que es la nueva; no
         encontrarlas NO prueba que sea la vieja.
      3. Nombre del archivo: solo desempate.
    """
    from app.acano.transformer import _norm_header
    hdr = [_norm_header(c) for c in rows[hdr_idx]]
    n = _norm_header_cell(nombre)
    if "nueva" in n or "nuevo" in n:
        por_nombre = "cartera_nueva"
    elif any(k in n for k in ("vieja", "viejo", "anti", "consolid", "base", "mundosoft")):
        por_nombre = "cartera_vieja"
    else:
        por_nombre = None

    if hoja == "Cartera_Consolidado" or "intermediario" in hdr:
        return {"tipo": "cartera_vieja", "confianza": "alta", "avisos": [],
                "motivo": "Columnas del Informe de Cartera en formato consolidado "
                          "(hoja «Cartera_Consolidado» / columna INTERMEDIARIO): es la cartera base."}

    i_cta = hdr.index("cuenta") if "cuenta" in hdr else 0
    total = largas = 0
    for row in rows[hdr_idx + 1:]:
        v = row[i_cta] if row and i_cta < len(row) else None
        if v is None or str(v).strip() == "":
            continue
        total += 1
        s = str(v).strip()
        if s.endswith(".0"):
            s = s[:-2]
        if s.isdigit() and len(s) >= 10:
            largas += 1
    hay_largas = total > 0 and largas / total >= 0.02

    avisos = []
    if hay_largas:
        tipo = "cartera_nueva"
        motivo = (f"Columnas del Informe de Cartera y {largas} cuentas con numeración de la "
                  "plataforma nueva (10+ dígitos): es la actualización de la plataforma nueva.")
        confianza = "alta"
        if por_nombre == "cartera_vieja":
            confianza = "media"
            avisos.append("El nombre del archivo sugiere la cartera de la plataforma vieja, pero "
                          "el contenido trae cuentas de la plataforma nueva. Confirma el tipo antes de cargar.")
    elif por_nombre == "cartera_nueva":
        tipo, confianza = "cartera_nueva", "media"
        motivo = ("Columnas del Informe de Cartera. El contenido no permite distinguir la "
                  "plataforma; se tomó «nueva» por el nombre del archivo.")
        avisos.append("No pude confirmar por el contenido que sea la cartera de la plataforma nueva "
                      "(se decidió por el nombre del archivo). Confirma el tipo antes de cargar.")
    elif por_nombre == "cartera_vieja":
        tipo, confianza = "cartera_vieja", "alta"
        motivo = ("Columnas del Informe de Cartera, sin cuentas de la plataforma nueva, y el "
                  "nombre del archivo coincide: es la cartera base (plataforma vieja).")
    else:
        tipo, confianza = "cartera_vieja", "media"
        motivo = ("Columnas del Informe de Cartera, sin cuentas de la plataforma nueva: "
                  "parece la cartera base (plataforma vieja).")
        avisos.append("Los informes de cartera de la plataforma vieja y de la nueva traen las mismas "
                      "columnas y no pude distinguirlos con certeza. Si es la cartera BASE reemplaza "
                      "toda la cartera; confirma el tipo antes de cargar.")
    return {"tipo": tipo, "confianza": confianza, "avisos": avisos, "motivo": motivo}


def _filas_con_datos(rows, hdr_idx: int) -> int:
    return sum(1 for r in rows[hdr_idx + 1:] if r and any(c is not None and c != "" for c in r))


def _no_reconocido(motivo: str = MSG_NO_RECONOCIDO, filas: int = 0) -> dict:
    return {"tipo": None, "etiqueta": None, "confianza": None, "filas": filas,
            "motivo": motivo, "_rows": None, "_hdr_idx": None, "_avisos": []}


def _detectar(nombre_archivo: str, contenido_bytes: bytes, forzar_base: str | None = None) -> dict:
    """Detección completa. Devuelve el dict público + claves internas (_rows,
    _hdr_idx, _avisos) para no volver a parsear el Excel.

    `forzar_base`: tipo base que pidió el usuario; se busca esa firma en las
    hojas candidatas en vez de quedarse con la primera reconocida."""
    _check_upload_file(SimpleNamespace(filename=nombre_archivo), contenido_bytes, _MAX_MB_GLOBAL)
    primera = None
    try:
        for hoja, rows, es_er in _hojas_candidatas(contenido_bytes):
            base, hdr_idx = _clasificar(rows, es_er)
            if primera is None:
                primera = len(rows)
            if base is None or (forzar_base and base != forzar_base):
                continue
            det = {"filas": _filas_con_datos(rows, hdr_idx), "_rows": rows,
                   "_hdr_idx": hdr_idx, "_avisos": [], "_hoja": hoja}
            if base == "cartera":
                sub = _subtipo_cartera(nombre_archivo or "", rows, hdr_idx, hoja)
                det.update(tipo=sub["tipo"], confianza=sub["confianza"], motivo=sub["motivo"])
                det["_avisos"] = sub["avisos"]
            else:
                det.update(tipo=base, confianza="alta",
                           motivo=f"Encabezados de «{TIPOS[base]['etiqueta']}» encontrados en la "
                                  f"fila {hdr_idx + 1} de la hoja «{hoja}».")
                esperadas = _POSICIONES.get(base)
                if esperadas:
                    hdr = [_norm_header_cell(c) for c in rows[hdr_idx]]
                    corridas = [f"«{frase}»" for i, frase in esperadas.items()
                                if i >= len(hdr) or frase not in hdr[i]]
                    if corridas:
                        det["confianza"] = "media"
                        det["_avisos"].append(
                            "Este reporte se lee por posición de columna y estas columnas no "
                            "están donde se esperaba: " + ", ".join(corridas) + ". La plataforma "
                            "pudo cambiar el formato; si lo cargas así los datos pueden quedar corridos.")
            det["etiqueta"] = TIPOS[det["tipo"]]["etiqueta"]
            return det
    except HTTPException:
        raise
    except Exception as e:
        _log.warning("No se pudo leer el Excel para detectar el tipo: %s", type(e).__name__)
        return _no_reconocido(
            "No pude leer el archivo como Excel (.xlsx). Si es un .xls antiguo o está "
            "dañado, ábrelo en Excel y guárdalo como .xlsx. " + MSG_NO_RECONOCIDO)
    return _no_reconocido(filas=max((primera or 1) - 1, 0))


def detectar_tipo(nombre_archivo: str, contenido_bytes: bytes) -> dict:
    """Decide qué reporte es un Excel mirando sus ENCABEZADOS.

    Devuelve {tipo, etiqueta, confianza ('alta'|'media'), filas, motivo}. Si no
    lo reconoce: tipo=None y `motivo` lista los archivos aceptados.
    Lanza HTTPException si el archivo no pasa la validación básica (extensión,
    tamaño, magic bytes, bomba zip) de upload_helpers._check_upload_file.
    """
    det = _detectar(nombre_archivo, contenido_bytes)
    return {k: v for k, v in det.items() if not k.startswith("_")}


# ============================================================
#                        VALIDACIÓN
# ============================================================
def _ultimo_log(conn, tipo: str):
    """Último sync_log exitoso de ese tipo."""
    if tipo in ("cartera_vieja", "cartera_nueva"):
        return conn.execute(
            "SELECT * FROM sync_logs WHERE source='manual_upload' AND status='success' "
            "AND tipo_carga=? ORDER BY id DESC LIMIT 1", (tipo,)).fetchone()
    return conn.execute(
        "SELECT * FROM sync_logs WHERE source=? AND status='success' ORDER BY id DESC LIMIT 1",
        (TIPOS[tipo]["source"],)).fetchone()


def _registros_log(row) -> int | None:
    if not row:
        return None
    return row["records_inserted"] or row["records_fetched"] or 0


def _var_pct(nuevo, anterior) -> float | None:
    if not anterior:
        return None
    return round((nuevo - anterior) / anterior * 100, 1)


def _miles(n) -> str:
    return f"{int(round(n)):,}".replace(",", ".")


def _medir(tipo: str, contenido: bytes, rows, hdr_idx: int, conn) -> dict:
    """Corrida en seco: aplica los MISMOS transformadores que la carga real,
    sin escribir. Devuelve {nuevo, anterior, alertas}. ValueError = el archivo
    no se podría cargar (mismo mensaje que daría el botón específico)."""
    alertas = []
    max_filas = TIPOS[tipo].get("max_filas")
    if max_filas and len(rows) > max_filas:
        raise ValueError(f"Excede {max_filas} filas")
    anterior = _registros_log(_ultimo_log(conn, tipo))

    def _alerta_fecha(f_archivo, f_cargada):
        if f_archivo and f_cargada and f_archivo < f_cargada:
            alertas.append(
                f"La fecha más reciente del archivo ({f_archivo}) es anterior a la más reciente "
                f"ya cargada ({f_cargada}): parece un archivo viejo.")

    if tipo in ("cartera_vieja", "cartera_nueva"):
        from app.acano.transformer import transform_excel_batch
        from app.sync.job import MAX_ROWS as _MAX_CARTERA
        datos = rows[hdr_idx + 1:]
        if len(datos) > _MAX_CARTERA:
            raise ValueError(f"Archivo excede el límite de {_MAX_CARTERA} filas")
        recs = transform_excel_batch(datos, rows[hdr_idx])
        if tipo == "cartera_vieja":
            recs = [r for r in recs if r.get("identificacion") and r.get("cliente") and r.get("estado")]
        else:
            recs = [r for r in recs if r.get("cuenta") and r.get("identificacion")]
        nuevo = len(recs)

        def _num(v):
            return float(v) if isinstance(v, (int, float)) else 0.0

        vigente = lote_cartera_vigente(conn)
        saldo_vig = saldo_nuevo = None
        if vigente:
            actuales = conn.execute(
                "SELECT cuenta, saldo_capital FROM credits WHERE sync_batch_id=?", (vigente,)).fetchall()
            saldo_vig = sum(_num(r["saldo_capital"]) for r in actuales)
            if tipo == "cartera_vieja":
                # Carga base: el lote vigente completo es lo que se reemplaza.
                anterior = len(actuales) or None
                saldo_nuevo = sum(_num(r.get("saldo_capital")) for r in recs)
            else:
                # Incremental: saldo proyectado = vigente, con el saldo de las
                # cuentas del archivo pisado/agregado (misma regla que la carga:
                # un saldo vacío en el archivo no pisa el existente).
                por_cuenta = {str(r["cuenta"]): _num(r["saldo_capital"]) for r in actuales}
                for r in recs:
                    if r.get("saldo_capital") is None and str(r["cuenta"]) in por_cuenta:
                        continue
                    por_cuenta[str(r["cuenta"])] = _num(r.get("saldo_capital"))
                saldo_nuevo = sum(por_cuenta.values())
        if saldo_vig and saldo_nuevo is not None:
            cambio = (saldo_nuevo - saldo_vig) / saldo_vig * 100
            if abs(cambio) > 30:
                alertas.append(
                    f"El saldo de capital total cambiaría {cambio:+.1f} % frente a la cartera "
                    f"vigente (de ${_miles(saldo_vig)} a ${_miles(saldo_nuevo)}).")
        return {"nuevo": nuevo, "anterior": anterior, "alertas": alertas}

    if tipo == "recaudo":
        if len(rows) < 2:
            raise ValueError("Archivo vacío o sin filas de datos")
        col_map = extras._resolve_pago_columns(rows[0])
        recs = [r for r in (extras._transform_pago_row(row, col_map) for row in rows[1:]) if r]
        f_arch = max((r["fecha_movimiento"] for r in recs if r.get("fecha_movimiento")), default=None)
        f_bd = conn.execute(
            "SELECT MAX(fecha_movimiento) FROM pagos WHERE sync_batch_id = (SELECT id FROM sync_logs "
            "WHERE source='recaudo_upload' AND status='success' ORDER BY id DESC LIMIT 1)").fetchone()[0]
        _alerta_fecha(f_arch, f_bd)
        return {"nuevo": len(recs), "anterior": anterior, "alertas": alertas}

    if tipo == "recaudo_legacy":
        recs = extras._aggregate_pagos_legacy(rows)
        f_arch = max((r["fecha_ultimo_pago"] for r in recs if r.get("fecha_ultimo_pago")), default=None)
        f_bd = conn.execute("SELECT MAX(fecha_ultimo_pago) FROM pagos_legacy").fetchone()[0]
        _alerta_fecha(f_arch, f_bd)
        return {"nuevo": len(recs), "anterior": anterior, "alertas": alertas}

    if tipo == "solicitudes":
        recs = [r for r in (extras._transform_solic_row(row) for row in rows[hdr_idx + 1:]) if r]
        f_arch = max((r["fecha_solicitud"] for r in recs if r.get("fecha_solicitud")), default=None)
        f_bd = conn.execute(
            "SELECT MAX(fecha_solicitud) FROM solicitudes WHERE sync_batch_id = (SELECT id FROM sync_logs "
            "WHERE source='solicitudes_upload' AND status='success' ORDER BY id DESC LIMIT 1)").fetchone()[0]
        _alerta_fecha(f_arch, f_bd)
        return {"nuevo": len(recs), "anterior": anterior, "alertas": alertas}

    if tipo == "solicitudes_legacy":
        recs = [r for r in (extras._transform_solic_legacy_row(row) for row in rows[1:]) if r]
        f_arch = max((r["fecha_solicitud"] for r in recs if r.get("fecha_solicitud")), default=None)
        f_bd = conn.execute("SELECT MAX(fecha_solicitud) FROM solicitudes_legacy").fetchone()[0]
        _alerta_fecha(f_arch, f_bd)
        return {"nuevo": len(recs), "anterior": anterior, "alertas": alertas}

    if tipo == "juridico":
        recs = [r for r in (extras._transform_juridico_row(row) for row in rows[hdr_idx + 1:]) if r]
        return {"nuevo": len(recs), "anterior": anterior, "alertas": alertas}

    if tipo == "financieros":
        p = financieros.parsear_estado_resultados(contenido)
        meses = p["months_in_file"]
        # Lo comparable es lo que este archivo REEMPLAZA (mismo año y meses),
        # no la última carga: cada año llega en su propio archivo.
        anterior = None
        if meses:
            marcas = ",".join("?" * len(meses))
            n = conn.execute(
                f"SELECT COUNT(*) FROM estados_financieros WHERE year=? AND month IN ({marcas})",
                [p["year"], *meses]).fetchone()[0]
            anterior = n or None
        return {"nuevo": len(p["records"]), "anterior": anterior, "alertas": alertas,
                "detalle": {"year": p["year"], "meses": meses}}

    if tipo == "saldo_cartera":
        a = saldo_cartera.agregar_resumen_estado_cuenta(rows)
        r = conn.execute("SELECT n_cuentas FROM saldo_cartera_snapshots "
                         "ORDER BY snapshot_date DESC, id DESC LIMIT 1").fetchone()
        return {"nuevo": a["n"], "anterior": (r[0] or None) if r else None, "alertas": alertas}

    raise ValueError(f"Tipo de carga desconocido: {tipo}")


def _analizar(nombre: str, contenido: bytes, tipo_forzado: str | None = None) -> dict:
    """Detección + validación previa. NO escribe en la BD: los transformadores
    corren en memoria y la conexión solo se usa para leer lo ya cargado."""
    if tipo_forzado and tipo_forzado not in TIPOS:
        raise HTTPException(status_code=400, detail=(
            f"Tipo de carga desconocido: «{tipo_forzado}». Tipos válidos: " + ", ".join(TIPOS)))

    det = _detectar(nombre, contenido)
    if tipo_forzado and det["tipo"] != tipo_forzado:
        base = "cartera" if tipo_forzado.startswith("cartera_") else tipo_forzado
        det_base = None if det["tipo"] is None else (
            "cartera" if det["tipo"].startswith("cartera_") else det["tipo"])
        if det_base != base:
            # ¿Está esa firma en otra hoja del libro?
            det = _detectar(nombre, contenido, forzar_base=base)
        if det["tipo"] is None:
            raise HTTPException(status_code=400, detail=(
                f"Este archivo no tiene las columnas de «{TIPOS[tipo_forzado]['etiqueta']}». "
                "Verifica que sea el reporte correcto."))
        det.update(tipo=tipo_forzado, etiqueta=TIPOS[tipo_forzado]["etiqueta"], confianza="media",
                   motivo="Tipo elegido manualmente por el usuario.")
        # Los avisos de "no pude distinguir vieja/nueva" sobran: el usuario ya eligió.
        if base == "cartera":
            det["_avisos"] = []

    out = {
        "archivo": nombre, "tipo": det["tipo"], "etiqueta": det["etiqueta"],
        "confianza": det["confianza"], "filas": det["filas"], "motivo": det["motivo"],
        "validacion": None, "requiere_confirmacion": False, "error": None,
        "tipos_aceptados": _tipos_aceptados(), "_rows": det["_rows"],
    }
    tipo = det["tipo"]
    if tipo is None:
        return out
    out["endpoint_equivalente"] = TIPOS[tipo]["endpoint"]

    # Límite de tamaño propio del tipo (los históricos admiten más que el resto).
    _check_upload_file(SimpleNamespace(filename=nombre), contenido, TIPOS[tipo]["max_mb"])

    alertas = list(det["_avisos"])
    nuevo, anterior, detalle = 0, None, None
    conn = get_connection()
    try:
        try:
            m = _medir(tipo, contenido, det["_rows"], det["_hdr_idx"], conn)
            nuevo, anterior, detalle = m["nuevo"], m["anterior"], m.get("detalle")
            alertas += m["alertas"]
        except ValueError as e:
            # El cargador rechazaría este archivo: se informa aquí con el mismo
            # mensaje, en vez de dejar que falle al aplicar.
            out["error"] = str(e)[:400]
            alertas.append(out["error"])
    finally:
        conn.close()

    if out["error"] is None:
        if nuevo == 0:
            alertas.append("El archivo no trae filas útiles (0 registros válidos).")
        elif anterior and nuevo < anterior * 0.5:
            alertas.append(
                f"El archivo trae {_miles(nuevo)} registros, menos de la mitad de los "
                f"{_miles(anterior)} de la carga anterior.")
    out["validacion"] = {
        "registros_nuevo": nuevo, "registros_anterior": anterior,
        "variacion_pct": _var_pct(nuevo, anterior), "alertas": alertas,
    }
    if detalle:
        out["validacion"]["detalle"] = detalle
    out["requiere_confirmacion"] = bool(alertas)
    return out


def _publico(an: dict) -> dict:
    return {k: v for k, v in an.items() if not k.startswith("_")}


# ============================================================
#                        ENDPOINTS
# ============================================================
@router.post("/analizar")
async def analizar(user=Depends(require_superadmin), file: UploadFile = File(...),
                   tipo: str | None = Form(default=None)):
    """Detecta el tipo de archivo y lo valida contra lo ya cargado. NO escribe."""
    contenido = await file.read()
    an = await run_in_threadpool(_analizar, file.filename or "", contenido, (tipo or "").strip() or None)
    return _publico(an)


@router.post("/aplicar")
async def aplicar(request: Request, user=Depends(require_superadmin),
                  file: UploadFile = File(...),
                  tipo: str | None = Form(default=None),
                  confirmar: str | None = Form(default=None)):
    """Carga el archivo con la lógica del endpoint específico de su tipo.

    Vuelve a detectar y validar (el archivo pudo cambiar desde /analizar). Con
    alertas exige confirmar=1; sin eso responde 409 con las alertas."""
    contenido = await file.read()
    nombre = file.filename or ""
    an = await run_in_threadpool(_analizar, nombre, contenido, (tipo or "").strip() or None)
    tipo_final = an["tipo"]
    if tipo_final is None:
        raise HTTPException(status_code=400, detail=an["motivo"])
    if an["error"]:
        raise HTTPException(status_code=400, detail=an["error"])
    confirmado = (confirmar or "").strip() == "1"
    if an["requiere_confirmacion"] and not confirmado:
        return JSONResponse(status_code=409, content={
            "detail": "El archivo tiene alertas. Revísalas y confirma para cargarlo.",
            **_publico(an),
        })

    cfg = TIPOS[tipo_final]
    ip = get_client_ip(request) or "unknown"
    if tipo_final == "cartera_vieja":
        resultado = await run_in_threadpool(sync_routes.ejecutar_carga_base, user, nombre, contenido, ip)
    elif tipo_final == "cartera_nueva":
        resultado = await run_in_threadpool(
            sync_routes.ejecutar_actualizacion_incremental, user, nombre, contenido, ip)
    else:
        kw = {"generic_error_msg": cfg["msg_error"]} if cfg.get("msg_error") else {}
        async with upload_session(request, user, file, source=cfg["source"],
                                  max_mb=cfg["max_mb"], content=contenido, **kw) as ctx:
            resultado = await run_in_threadpool(cfg["cargar"], ctx, an["_rows"])

    filas = an["validacion"]["registros_nuevo"]
    log_audit(user["user_id"], user["username"], "carga_unica",
              f"tipo={tipo_final} file={nombre} filas={_miles(filas)} "
              f"confirmado={'si' if confirmado else 'no'} alertas={len(an['validacion']['alertas'])}", ip)
    return {**resultado, "tipo": tipo_final, "etiqueta": cfg["etiqueta"]}


def _tipo_de_log(source: str | None, tipo_carga: str | None) -> tuple[str | None, str]:
    """(tipo, etiqueta en español) de una fila de sync_logs."""
    if source == "manual_upload":
        if tipo_carga in TIPOS:
            return tipo_carga, TIPOS[tipo_carga]["etiqueta"]
        # Cargas anteriores a la columna tipo_carga: no se sabe si base o incremental.
        return "cartera", "Cartera"
    t = _SOURCE_A_TIPO.get(source or "")
    return (t, TIPOS[t]["etiqueta"]) if t else (None, source or "(desconocido)")


def _duracion(d: dict) -> float | None:
    if d.get("duration_seconds") is not None:
        return round(float(d["duration_seconds"]), 2)
    try:
        ini = datetime.fromisoformat(d["started_at"])
        fin = datetime.fromisoformat(d["completed_at"])
        return round((fin - ini).total_seconds(), 2)
    except (TypeError, ValueError):
        return None


@router.get("/historial")
def historial(limit: int = 50, _user=Depends(require_superadmin)):
    """Bitácora de cargas, de la más reciente a la más antigua.

    variacion_pct compara los registros de cada carga exitosa contra la carga
    exitosa anterior del mismo tipo. Las fechas van en UTC, como en sync_logs."""
    limit = max(1, min(int(limit), 500))
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT sl.id, sl.source, sl.tipo_carga, sl.started_at, sl.completed_at, sl.status, "
            "sl.records_fetched, sl.records_inserted, sl.error_message, sl.duration_seconds, "
            "u.username, u.display_name "
            "FROM sync_logs sl LEFT JOIN users u ON u.id = sl.uploaded_by ORDER BY sl.id ASC"
        ).fetchall()
    finally:
        conn.close()
    previo = {}   # clave del tipo -> registros de la última carga exitosa
    out = []
    for r in rows:
        d = dict(r)
        tipo, etiqueta = _tipo_de_log(d["source"], d["tipo_carga"])
        registros = d["records_inserted"] or d["records_fetched"] or 0
        variacion = None
        if d["status"] == "success":
            clave = tipo or d["source"]
            variacion = _var_pct(registros, previo.get(clave))
            previo[clave] = registros
        out.append({
            "id": d["id"], "tipo": tipo, "etiqueta": etiqueta, "source": d["source"],
            "usuario": d["username"], "usuario_nombre": d["display_name"],
            "started_at": d["started_at"], "completed_at": d["completed_at"],
            "duracion_seg": _duracion(d), "status": d["status"], "registros": registros,
            "error": (d["error_message"] or "")[:200] or None,
            "variacion_pct": variacion,
        })
    out.reverse()
    return out[:limit]


@router.get("/estado")
def estado(_user=Depends(require_auth)):
    """Última carga exitosa de cada tipo + días transcurridos (semáforo de frescura)."""
    ahora = datetime.utcnow()
    conn = get_connection()
    try:
        out = []
        for tipo, cfg in TIPOS.items():
            if cfg["source"] == "manual_upload":
                # tipo_carga NULL = carga previa a esa columna: cuenta para ambas
                # carteras (transitorio; esos logs se depuran a los 30 días).
                r = conn.execute(
                    "SELECT sl.*, u.username FROM sync_logs sl LEFT JOIN users u ON u.id = sl.uploaded_by "
                    "WHERE sl.source='manual_upload' AND sl.status='success' "
                    "AND (sl.tipo_carga = ? OR sl.tipo_carga IS NULL) ORDER BY sl.id DESC LIMIT 1",
                    (tipo,)).fetchone()
            else:
                r = conn.execute(
                    "SELECT sl.*, u.username FROM sync_logs sl LEFT JOIN users u ON u.id = sl.uploaded_by "
                    "WHERE sl.source=? AND sl.status='success' ORDER BY sl.id DESC LIMIT 1",
                    (cfg["source"],)).fetchone()
            item = {"tipo": tipo, "etiqueta": cfg["etiqueta"], "ultima_carga": None,
                    "usuario": None, "registros": None, "dias": None}
            if r:
                fecha = r["completed_at"] or r["started_at"]
                item.update(ultima_carga=fecha, usuario=r["username"],
                            registros=r["records_inserted"] or r["records_fetched"] or 0)
                try:
                    item["dias"] = max((ahora - datetime.fromisoformat(fecha)).days, 0)
                except (TypeError, ValueError):
                    pass
            out.append(item)
        return out
    finally:
        conn.close()


@router_cartera.get("/snapshots")
def cartera_snapshots(meses: int = 24, _user=Depends(require_auth)):
    """Foto mensual de la cartera (cierre de cada mes), orden ascendente por mes."""
    meses = max(1, min(int(meses), 120))
    conn = get_connection()
    try:
        return listar_snapshots(conn, meses)
    finally:
        conn.close()
