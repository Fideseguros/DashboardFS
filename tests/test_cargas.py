"""Pruebas de la carga única (/api/cargas/*), el historial de cargas y la foto
mensual de la cartera. Todos los Excel son sintéticos (openpyxl en memoria);
los encabezados copian los de los reportes reales para que la detección sea
la misma que en producción."""
import io

import pytest
from openpyxl import Workbook

from app.routes.cargas import TIPOS, detectar_tipo
from app.snapshots import ahora_colombia

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# ----------------------------------------------------------------------------
#                       Encabezados de los reportes reales
# ----------------------------------------------------------------------------
H_CARTERA = [
    "Cuenta", "Solicitud", "Identificacion", "Cliente", "Estado", "Linea", "Valor_credito",
    "Saldo_capital", "Saldo_favor", "Valor_cuota", "Fecha_inicio", "Fecha_vencimiento",
    "Fecha_ult_pago", "Calificacion_contable", "Calificacion_modelo_referencia", "Sector_credito",
    "Fecha_desembolso", "Tasa_efectiva", "Tasa_nominal", "Plazo", "Digitochequeo",
    "Periodo_gracia", "Periodo_muerto", "Gastos_judiciales", "Honorarios", "Cuotas_alivio",
    "Fecha_cancelacion", "Cuotas_pactadas", "Cuotas_pagadas", "Tipo_producto", "Solicitud_icetex",
    "Valor_girado_icetex", "Descripcion_sublinea", "Referencia_pago", "Tipo_cartera", "Desc_endoso",
    "Entidad_endoso", "Dias_mora", "Maxima_mora", "Periodo_maxima_mora", "Clasificacion",
    "Emp_libranza", "Emails_libranza", "Dia_pago_libranza", "Telefono_fijo_libranza",
    "Metodo_pago_libranza", "Tipo_moneda", "Telefono_fijo", "Telefono_oficina", "Telefono_celular",
    "Ciudad", "Direccion", "Aliado_vendedor", "Aliado_asesor",
]
H_RECAUDO = [
    "Entidad", "Linea Credito", "Fecha Movimiento", "Fecha Documento", "Identificacion", "Cliente",
    "Cuenta", "Solicitud", "Aliado Vendedor", "Tipo Mvto", "Tipo Documento", "Documento", "Usuario",
    "Ciclo Cobro", "Capital Prestamos", "Interes Corriente", "Interes De Mora", "I.v.a",
    "Retencion En La Fuente", "Saldo Favor", "Gastos Prejuridico - Juridico", "Deudores Varios",
    "Cargos Administrativos", "Total", "Total Cheque", "Total Efectivo", "Total Tarjeta",
    "Total Interno", "Autorizacion", "Observaciones",
]
H_RECAUDO_LEGACY = [
    "Código", "Id préstamo", "Id solicitud", "Número de identificación", "Nombre completo",
    "Id transacción", "Fecha de creación", "Fecha valor", "Método de pago", "Estado",
    "Número de cuenta bancaria relacionada", "Nombre del banco de cuenta relacionada",
    "Cuenta Contable de la Cuenta Bancaria", "Cuenta de Ingresos", "Reversado?", "Tipo de pago",
    "Descripción", "Valor de pago", "Usuario de creación", "Préstamo Cancelado", "Iva pagado",
    "Cargos netos pagados", "Fecha de pago",
]
H_SOLIC = [
    "Solicitud Origen", "Solicitud", "Hoja Ruta", "Línea Crédito", "Identificación", "Solicitante",
    "Tipo Moneda", "Valor", "Paso Ruta", "Responsable", "Estado", "SubEstado", "Empresa", "Oficina",
    "Fecha Solicitud", "Periodo Convocatoria", "¿Auxilio?", "Usuario", "Ver Perfil", "Ver Detalle",
]
H_SOLIC_LEGACY = [
    "Id de la solicitud", "Id Entidad", "Fecha de solicitud", "Tipo de identificación",
    "Número de identificación", "Fecha de expedición", "Nombre completo", "Originador",
    "Nombre del producto", "Fecha de nacimiento", "Estado de Solicitud",
    "Estado de precalificación de La solicitud", "Fecha de desembolso", "Monto de la solicitud",
    "Plazo de pago (días)", "Número de cuotas", "Frecuencia de pagos", "Fecha inicio pagos",
    "Tasa de interés", "Canal", "Género", "Edad", "Departamento nacimiento", "Ciudad nacimiento",
    "Dirección departamento", "Dirección ciudad", "Dirección", "Nombre del banco",
    "Número de cuenta bancaria", "Correo Electrónico", "Número Teléfonico", "Código Email",
    "Código SMS", "Fecha de confirmación de códigos", "Dirección IP de confirmación de códigos",
    "Solicitud Asociada", "Tipo de Solicitud", "Nombre asesor comercial", "Decisión Del Modelo",
    "Cliente recurrente",
]
H_JURIDICO = [
    "ID", "Nombre", "Naturaleza del litigio o demanda.", "Avance a la fecha.",
    "Respuesta de la Compañía o sus intenciones", "Evaluación de la probabilidad de un resultado",
    "Medida Cautelar", "Juzgado",
]
H_SALDO = [
    "Numero Cuenta", "Identificacion", "Cliente", "Linea", "Estado Cobro", "Saldo Vigente",
    "Dias Mora", "Fecha Corte", "Capital Prestamos", "Interes Corriente", "Interes De Mora",
    "Cargos Administrativos", "Deudores Varios", "Retencion En La Fuente", "Saldo Favor", "Otros",
    "Total",
]
MESES = ["ENERO", "FEBRERO", "MARZO", "ABRIL", "MAYO", "JUNIO", "JULIO", "AGOSTO", "SEPTIEMBRE",
         "OCTUBRE", "NOVIEMBRE", "DICIEMBRE"]


# ----------------------------------------------------------------------------
#                         Constructores de Excel sintético
# ----------------------------------------------------------------------------
def _xlsx(filas, hoja="Hoja1") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = hoja
    for f in filas:
        ws.append(list(f))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _fila(headers, **valores):
    """Fila alineada con `headers` a partir de {encabezado: valor}."""
    faltan = set(valores) - set(headers)
    assert not faltan, faltan
    return [valores.get(h) for h in headers]


def x_cartera(n=10, nueva=False, saldo=1_000_000, base_cuenta=1000) -> bytes:
    """n créditos: los 2 últimos CANCELADO; entre los ACTIVO, el 1.º y 2.º con
    45 días de mora y el 3.º con 120. Los 2 primeros desembolsados este mes."""
    mes = ahora_colombia().strftime("%Y-%m")
    filas = [H_CARTERA]
    for i in range(n):
        activo = i < n - 2
        mora = {0: 45, 1: 45, 2: 120}.get(i, 0) if activo else 0
        cuenta = (20250000000000 + i) if nueva else (base_cuenta + i)
        filas.append(_fila(
            H_CARTERA,
            Cuenta=cuenta, Solicitud=5000 + i, Identificacion=f"90000{i:03d}",
            Cliente=f"CLIENTE PRUEBA {i}", Estado="ACTIVO" if activo else "CANCELADO",
            Linea="CONSUMO" if i % 2 == 0 else "POLIZAS", Valor_credito=5_000_000,
            Saldo_capital=saldo if activo else 0, Saldo_favor=0, Valor_cuota=250_000,
            Fecha_inicio="2025-01-15", Fecha_vencimiento="2027-01-15", Fecha_ult_pago="2026-05-10",
            Calificacion_contable="A" if mora <= 30 else "C",
            Fecha_desembolso=f"{mes}-05" if i < 2 else "2025-01-10",
            Tasa_efectiva=24.5, Plazo=24, Cuotas_pactadas=24, Cuotas_pagadas=6,
            Dias_mora=mora, Maxima_mora=mora, Ciudad="PEREIRA", Aliado_vendedor="ALIADO UNO",
        ))
    return _xlsx(filas)


def x_recaudo(n=10, fecha="2026-06-15") -> bytes:
    filas = [H_RECAUDO]
    for i in range(n):
        filas.append(_fila(
            H_RECAUDO, **{
                "Entidad": "FIDE", "Linea Credito": "CONSUMO", "Fecha Movimiento": fecha,
                "Fecha Documento": fecha, "Identificacion": f"80000{i:03d}",
                "Cliente": f"PAGADOR {i}", "Cuenta": 1000 + i, "Solicitud": 5000 + i,
                "Aliado Vendedor": "ALIADO UNO", "Tipo Mvto": "Pago Normal",
                "Tipo Documento": "RC", "Documento": f"RC-{i}", "Usuario": "caja",
                "Capital Prestamos": 200_000, "Interes Corriente": 40_000, "Interes De Mora": 0,
                "I.v.a": 0, "Saldo Favor": 0, "Cargos Administrativos": 10_000, "Total": 250_000,
            }))
    return _xlsx(filas, hoja="Worksheet")


def x_recaudo_legacy(n=6, fecha="2025-03-20") -> bytes:
    filas = [H_RECAUDO_LEGACY]
    for i in range(n):
        for tipo in ("Capital", "Interes Mora"):   # 2 componentes por préstamo
            filas.append(_fila(
                H_RECAUDO_LEGACY, **{
                    "Código": f"P{i}", "Id préstamo": 700 + i, "Id solicitud": 300 + i,
                    "Número de identificación": f"70000{i:03d}", "Nombre completo": f"DEUDOR {i}",
                    "Id transacción": f"T{i}{tipo[0]}", "Fecha de creación": fecha,
                    "Fecha valor": fecha, "Método de pago": "PSE", "Estado": "Aplicado",
                    "Reversado?": "No", "Tipo de pago": tipo, "Descripción": "pago",
                    "Valor de pago": 100_000, "Usuario de creación": "sistema",
                    "Préstamo Cancelado": "No", "Iva pagado": 0, "Cargos netos pagados": 0,
                    "Fecha de pago": fecha,
                }))
    return _xlsx(filas, hoja="Datos")


def x_solicitudes(n=8, fecha="2026-06-10") -> bytes:
    filas = [H_SOLIC]
    for i in range(n):
        filas.append(_fila(
            H_SOLIC, **{
                "Solicitud Origen": 9000 + i, "Solicitud": 9100 + i, "Hoja Ruta": "Crédito",
                "Línea Crédito": "CONSUMO", "Identificación": f"60000{i:03d}",
                "Solicitante": f"SOLICITANTE {i}", "Tipo Moneda": "COP", "Valor": 3_000_000,
                "Paso Ruta": "Decisión Solicitud", "Responsable": "analista",
                "Estado": "DESEMBOLSADA" if i % 2 else "NEGADA", "Empresa": "FIDE",
                "Oficina": "PRINCIPAL", "Fecha Solicitud": fecha, "Usuario": "analista",
            }))
    return _xlsx(filas, hoja="Worksheet")


def x_solicitudes_legacy(n=7, fecha="2024-11-05") -> bytes:
    filas = [H_SOLIC_LEGACY]
    for i in range(n):
        filas.append(_fila(
            H_SOLIC_LEGACY, **{
                "Id de la solicitud": 400 + i, "Id Entidad": 1, "Fecha de solicitud": fecha,
                "Tipo de identificación": "CC", "Número de identificación": f"50000{i:03d}",
                "Nombre completo": f"ANTIGUO {i}", "Originador": "WEB",
                "Nombre del producto": "Póliza", "Estado de Solicitud": "Aprobada",
                "Fecha de desembolso": fecha, "Monto de la solicitud": 2_000_000,
                "Plazo de pago (días)": 360, "Número de cuotas": 12, "Canal": "Digital",
            }))
    return _xlsx(filas, hoja="Datos")


def x_juridico(n=5) -> bytes:
    filas = [["PROCESOS JUDICIALES"], H_JURIDICO]
    for i in range(n):
        filas.append([f"40000{i:03d}", f"DEMANDADO {i}", "Ejecutivo singular", "Mandamiento de pago",
                      "Continuar", "Probable", "Embargo", f"Juzgado {i + 1} Civil Municipal"])
    return _xlsx(filas)


def x_financieros(year=2026, valor=1_000_000) -> bytes:
    hdr = [None, "CUENTA", None]
    for m in MESES:
        hdr += [m, None]

    def fila(texto, ene=None, feb=None):
        return [None, texto, None, ene, None, feb]

    filas = [
        [None, "FIDE SEGUROS"],
        [None, f"ESTADO DE RESULTADO INTEGRAL - MENSUALES DEL {year}"],
        [],
        hdr,
        fila("4 INGRESOS"),
        fila("41502001 INTERESES FINANCIACION POLIZAS", valor, valor * 2),
        fila("Total 4 INGRESOS", valor, valor * 2),
        fila("5 GASTOS"),
        fila("51050601 SUELDOS", valor / 2, valor / 2),
        fila("Total 51 OPERACIONALES DE ADMINISTRACION", valor / 2, valor / 2),
        fila("UTILIDAD (PERDIDA) DEL EJERCICIO", valor / 2, valor * 1.5),
    ]
    return _xlsx(filas, hoja="EST. DE RES. MES A MES ")


def x_saldo(n=9) -> bytes:
    filas = [H_SALDO]
    for i in range(n):
        filas.append(_fila(
            H_SALDO, **{
                "Numero Cuenta": 1000 + i, "Identificacion": f"30000{i:03d}",
                "Cliente": f"TITULAR {i}", "Estado Cobro": "Al día", "Saldo Vigente": 900_000,
                "Capital Prestamos": 900_000, "Interes Corriente": 50_000, "Interes De Mora": 10_000,
                "Cargos Administrativos": 5_000, "Deudores Varios": 0,
                "Retencion En La Fuente": 0, "Total": 965_000,
            }))
    return _xlsx(filas, hoja="Worksheet")


# tipo -> (nombre de archivo, constructor). El nombre solo importa para cartera.
MUESTRAS = {
    "cartera_vieja": ("Informe cartera plataforma vieja.xlsx", lambda: x_cartera()),
    "cartera_nueva": ("Informe cartera nueva plataforma.xlsx", lambda: x_cartera(nueva=True)),
    "recaudo": ("pagos.xlsx", x_recaudo),
    "recaudo_legacy": ("historico.xlsx", x_recaudo_legacy),
    "solicitudes": ("listado.xlsx", x_solicitudes),
    "solicitudes_legacy": ("antiguas.xlsx", x_solicitudes_legacy),
    "juridico": ("procesos.xlsx", x_juridico),
    "financieros": ("resultados.xlsx", x_financieros),
    "saldo_cartera": ("Resumen Estado Cuenta 20260604.xlsx", x_saldo),
}


def _subir(client, url, nombre, contenido, **data):
    return client.post(url, files={"file": (nombre, contenido, XLSX)}, data=data)


def _analizar(client, nombre, contenido, **data):
    return _subir(client, "/api/cargas/analizar", nombre, contenido, **data)


def _aplicar(client, nombre, contenido, **data):
    return _subir(client, "/api/cargas/aplicar", nombre, contenido, **data)


def _n(db, tabla):
    return db.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]


# ----------------------------------------------------------------------------
#                                 Detección
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("tipo", list(MUESTRAS))
def test_detecta_cada_tipo(tipo):
    nombre, construir = MUESTRAS[tipo]
    d = detectar_tipo(nombre, construir())
    assert d["tipo"] == tipo, d
    assert d["etiqueta"] == TIPOS[tipo]["etiqueta"]
    assert d["confianza"] == "alta"
    assert d["filas"] > 0 and d["motivo"]
    assert set(d) == {"tipo", "etiqueta", "confianza", "filas", "motivo"}


def test_deteccion_no_depende_del_nombre():
    """Un nombre engañoso no cambia el tipo: mandan los encabezados."""
    assert detectar_tipo("Informe cartera vieja.xlsx", x_recaudo())["tipo"] == "recaudo"
    assert detectar_tipo("pagos.xlsx", x_solicitudes())["tipo"] == "solicitudes"
    assert detectar_tipo("solicitudes.xlsx", x_juridico())["tipo"] == "juridico"


def test_cartera_vieja_vs_nueva():
    """Mismas columnas: decide el contenido (cuentas de 10+ dígitos = nueva) y
    el nombre solo desempata o baja la confianza."""
    vieja, nueva = x_cartera(), x_cartera(nueva=True)
    d = detectar_tipo("archivo.xlsx", nueva)
    assert (d["tipo"], d["confianza"]) == ("cartera_nueva", "alta")
    d = detectar_tipo("archivo.xlsx", vieja)
    assert (d["tipo"], d["confianza"]) == ("cartera_vieja", "media")
    d = detectar_tipo("cartera nueva junio.xlsx", vieja)        # desempate por nombre
    assert (d["tipo"], d["confianza"]) == ("cartera_nueva", "media")
    d = detectar_tipo("cartera vieja.xlsx", nueva)              # el contenido manda
    assert (d["tipo"], d["confianza"]) == ("cartera_nueva", "media")


def test_cartera_consolidado_es_base():
    filas = [[("INTERMEDIARIO" if h == "Aliado_vendedor" else h) for h in H_CARTERA],
             _fila(H_CARTERA, Cuenta=1, Identificacion="123", Cliente="X", Estado="ACTIVO",
                   Saldo_capital=10, Valor_cuota=1)]
    d = detectar_tipo("archivo.xlsx", _xlsx(filas, hoja="Cartera_Consolidado"))
    assert (d["tipo"], d["confianza"]) == ("cartera_vieja", "alta")


def test_archivo_desconocido():
    d = detectar_tipo("ventas.xlsx", _xlsx([["Producto", "Cantidad", "Precio"], ["A", 1, 2]]))
    assert d["tipo"] is None and d["etiqueta"] is None
    for palabra in ("cartera", "recaudo", "solicitudes", "procesos judiciales",
                    "Estado de resultados", "Resumen Estado Cuenta"):
        assert palabra in d["motivo"]


def test_columnas_corridas_bajan_confianza(admin_client):
    """Solicitudes se lee por posición: si insertan una columna, se avisa."""
    hdr = H_SOLIC[:4] + ["Columna Nueva"] + H_SOLIC[4:]
    fila = [9000, 9100, "Crédito", "CONSUMO", "x", "600001", "ALGUIEN", "COP", 1000,
            "Decisión", "analista", "NEGADA"]
    res = _analizar(admin_client, "listado.xlsx", _xlsx([hdr, fila]))
    j = res.json()
    assert j["tipo"] == "solicitudes" and j["confianza"] == "media"
    assert j["requiere_confirmacion"] and "posición" in j["validacion"]["alertas"][0]


def test_archivo_que_no_es_excel(admin_client):
    res = _analizar(admin_client, "falso.xlsx", b"esto no es un excel de verdad")
    assert res.status_code == 400
    res = _analizar(admin_client, "notas.txt", b"hola")
    assert res.status_code == 400


# ----------------------------------------------------------------------------
#                            analizar / aplicar
# ----------------------------------------------------------------------------
def test_analizar_no_escribe(admin_client, db):
    for tipo, (nombre, construir) in MUESTRAS.items():
        res = _analizar(admin_client, nombre, construir())
        assert res.status_code == 200, res.text
        j = res.json()
        assert j["tipo"] == tipo and j["archivo"] == nombre
        assert j["validacion"]["registros_nuevo"] > 0
        assert j["validacion"]["registros_anterior"] is None
        assert j["validacion"]["variacion_pct"] is None
        assert j["validacion"]["alertas"] == [] and j["requiere_confirmacion"] is False
        assert j["error"] is None
    for tabla in ("sync_logs", "credits", "pagos", "pagos_legacy", "solicitudes",
                  "solicitudes_legacy", "procesos_juridicos", "estados_financieros",
                  "saldo_cartera_snapshots", "cartera_snapshots"):
        assert _n(db, tabla) == 0, tabla
    assert _n(db, "audit_logs WHERE action LIKE 'carga%'") == 0


def test_analizar_desconocido_responde_200_sin_tipo(admin_client):
    res = _analizar(admin_client, "ventas.xlsx", _xlsx([["Producto", "Precio"], ["A", 2]]))
    assert res.status_code == 200
    j = res.json()
    assert j["tipo"] is None and j["validacion"] is None and "Acepto" in j["motivo"]
    assert len(j["tipos_aceptados"]) == 9
    res = _aplicar(admin_client, "ventas.xlsx", _xlsx([["Producto", "Precio"], ["A", 2]]))
    assert res.status_code == 400 and "Acepto" in res.json()["detail"]


@pytest.mark.parametrize("tipo", list(MUESTRAS))
def test_aplicar_cada_tipo_registra_usuario_y_conteos(admin_client, db, tipo):
    nombre, construir = MUESTRAS[tipo]
    res = _aplicar(admin_client, nombre, construir())
    assert res.status_code == 200, res.text
    j = res.json()
    assert j["status"] == "success"
    assert j["tipo"] == tipo and j["etiqueta"] == TIPOS[tipo]["etiqueta"]

    log = db.execute("SELECT * FROM sync_logs ORDER BY id DESC LIMIT 1").fetchone()
    admin_id = db.execute("SELECT id FROM users WHERE username='admin_test'").fetchone()[0]
    assert log["source"] == TIPOS[tipo]["source"] and log["status"] == "success"
    assert log["uploaded_by"] == admin_id
    assert log["records_fetched"] > 0 and log["records_inserted"] > 0
    assert log["duration_seconds"] is not None and log["completed_at"]
    if tipo.startswith("cartera_"):
        assert log["tipo_carga"] == tipo

    aud = db.execute("SELECT details FROM audit_logs WHERE action='carga_unica'").fetchone()
    # (el nombre se compara por su inicio: la auditoría anonimiza secuencias de dígitos)
    assert aud and f"tipo={tipo}" in aud["details"] and f"file={nombre[:12]}" in aud["details"]


def test_aplicar_devuelve_lo_mismo_que_el_endpoint_especifico(admin_client, db):
    """Mismas claves y mismos conteos que el botón de cada módulo."""
    casos = [
        ("recaudo", "/api/recaudo/upload", x_recaudo()),
        ("recaudo_legacy", "/api/recaudo/upload-legacy", x_recaudo_legacy()),
        ("solicitudes", "/api/solicitudes/upload", x_solicitudes()),
        ("solicitudes_legacy", "/api/solicitudes/upload-legacy", x_solicitudes_legacy()),
        ("juridico", "/api/juridico/upload", x_juridico()),
        ("financieros", "/api/estados-financieros/upload", x_financieros()),
        ("saldo_cartera", "/api/saldo-cartera/upload", x_saldo()),
        ("cartera_vieja", "/api/sync/upload-excel", x_cartera()),
        ("cartera_nueva", "/api/sync/update-incremental", x_cartera(nueva=True)),
    ]
    for tipo, url, contenido in casos:
        nombre = MUESTRAS[tipo][0]
        viejo = _subir(admin_client, url, nombre, contenido)
        assert viejo.status_code == 200, (url, viejo.text)
        nuevo = _aplicar(admin_client, nombre, contenido, confirmar="1")
        assert nuevo.status_code == 200, (tipo, nuevo.text)
        a = {k: v for k, v in viejo.json().items() if k != "duration"}
        b = {k: v for k, v in nuevo.json().items() if k not in ("duration", "tipo", "etiqueta")}
        if tipo == "cartera_nueva":
            # La 2.ª pasada ya encuentra las cuentas: lo que era "nuevo" es "actualizado".
            assert set(a) == set(b) and b["actualizados"] == a["nuevos"] and b["nuevos"] == 0
        else:
            assert a == b, tipo
    # El endpoint viejo también deja usuario, conteos y duración en sync_logs.
    malos = db.execute(
        "SELECT COUNT(*) FROM sync_logs WHERE uploaded_by IS NULL OR records_inserted = 0 "
        "OR duration_seconds IS NULL OR status != 'success'").fetchone()[0]
    assert malos == 0 and _n(db, "sync_logs") == len(casos) * 2


def test_alerta_por_caida_de_registros_y_409_sin_confirmar(admin_client, db):
    assert _aplicar(admin_client, "pagos.xlsx", x_recaudo(10)).status_code == 200
    chico = x_recaudo(3)

    j = _analizar(admin_client, "pagos.xlsx", chico).json()
    v = j["validacion"]
    assert (v["registros_nuevo"], v["registros_anterior"], v["variacion_pct"]) == (3, 10, -70.0)
    assert j["requiere_confirmacion"] is True
    assert len(v["alertas"]) == 1 and "menos de la mitad" in v["alertas"][0]

    res = _aplicar(admin_client, "pagos.xlsx", chico)
    assert res.status_code == 409
    j = res.json()
    assert isinstance(j["detail"], str) and j["requiere_confirmacion"] is True
    assert "menos de la mitad" in j["validacion"]["alertas"][0] and j["tipo"] == "recaudo"
    assert _n(db, "pagos") == 10 and _n(db, "sync_logs") == 1      # no se tocó nada

    res = _aplicar(admin_client, "pagos.xlsx", chico, confirmar="1")
    assert res.status_code == 200 and res.json()["records"] == 3
    assert _n(db, "pagos") == 3
    aud = db.execute("SELECT details FROM audit_logs WHERE action='carga_unica' "
                     "ORDER BY id DESC LIMIT 1").fetchone()["details"]
    assert "confirmado=si" in aud and "alertas=1" in aud


def test_alerta_archivo_viejo(admin_client):
    assert _aplicar(admin_client, "pagos.xlsx", x_recaudo(10, fecha="2026-06-15")).status_code == 200
    j = _analizar(admin_client, "pagos.xlsx", x_recaudo(10, fecha="2026-05-31")).json()
    assert j["requiere_confirmacion"] is True
    assert any("2026-05-31" in a and "2026-06-15" in a and "archivo viejo" in a
               for a in j["validacion"]["alertas"])
    # Misma fecha o más reciente: sin alerta.
    j = _analizar(admin_client, "pagos.xlsx", x_recaudo(10, fecha="2026-06-15")).json()
    assert j["validacion"]["alertas"] == []

    assert _aplicar(admin_client, "listado.xlsx", x_solicitudes(8, fecha="2026-06-10")).status_code == 200
    j = _analizar(admin_client, "listado.xlsx", x_solicitudes(8, fecha="2026-01-10")).json()
    assert any("archivo viejo" in a for a in j["validacion"]["alertas"])


def test_alerta_cero_filas_utiles(admin_client):
    j = _analizar(admin_client, "pagos.xlsx", _xlsx([H_RECAUDO, [None] * len(H_RECAUDO)])).json()
    assert j["tipo"] == "recaudo" and j["validacion"]["registros_nuevo"] == 0
    assert j["requiere_confirmacion"] is True
    assert any("0 registros" in a for a in j["validacion"]["alertas"])


def test_alerta_cambio_de_saldo_en_cartera(admin_client):
    nombre = MUESTRAS["cartera_vieja"][0]
    assert _aplicar(admin_client, nombre, x_cartera(saldo=1_000_000)).status_code == 200
    j = _analizar(admin_client, nombre, x_cartera(saldo=1_500_000)).json()
    assert j["validacion"]["registros_anterior"] == 10 and j["validacion"]["variacion_pct"] == 0.0
    assert any("saldo de capital" in a and "+50.0 %" in a for a in j["validacion"]["alertas"])
    j = _analizar(admin_client, nombre, x_cartera(saldo=1_100_000)).json()   # +10 %: sin alerta
    assert j["validacion"]["alertas"] == []
    # Incremental: se proyecta el saldo resultante (pisa las cuentas que coinciden).
    # 2 ACTIVO a 4M y 2 CANCELADO en 0 pisan 4 cuentas de 1M: 8M → 12M (+50 %).
    inc = x_cartera(n=4, saldo=4_000_000)
    j = _analizar(admin_client, "cartera.xlsx", inc, tipo="cartera_nueva").json()
    assert j["tipo"] == "cartera_nueva"
    assert any("+50.0 %" in a and "12.000.000" in a for a in j["validacion"]["alertas"])


def test_cartera_ambigua_exige_confirmar(admin_client, db):
    res = _aplicar(admin_client, "archivo.xlsx", x_cartera())
    assert res.status_code == 409 and "no pude distinguirlos" in res.json()["validacion"]["alertas"][0]
    assert _n(db, "credits") == 0
    assert _aplicar(admin_client, "archivo.xlsx", x_cartera(), confirmar="1").status_code == 200
    assert _n(db, "credits") == 10


def test_tipo_forzado(admin_client, db):
    nombre = MUESTRAS["cartera_vieja"][0]
    assert _aplicar(admin_client, nombre, x_cartera()).status_code == 200
    res = _aplicar(admin_client, nombre, x_cartera(), tipo="cartera_nueva")
    assert res.status_code == 200, res.text
    j = res.json()
    assert j["tipo"] == "cartera_nueva" and j["actualizados"] == 10 and j["nuevos"] == 0
    # Forzar un tipo cuyas columnas no están → 400 claro, sin tocar nada.
    res = _aplicar(admin_client, "pagos.xlsx", x_recaudo(), tipo="juridico", confirmar="1")
    assert res.status_code == 400 and "Procesos judiciales" in res.json()["detail"]
    res = _aplicar(admin_client, "pagos.xlsx", x_recaudo(), tipo="inventado")
    assert res.status_code == 400
    assert _n(db, "pagos") == 0


def test_error_del_cargador_se_informa_al_analizar(admin_client):
    """Reconocido como recaudo pero sin una columna obligatoria → error claro."""
    hdr = [h for h in H_RECAUDO if h != "Total"]
    contenido = _xlsx([hdr, ["x"] * len(hdr)])
    j = _analizar(admin_client, "pagos.xlsx", contenido).json()
    assert j["tipo"] == "recaudo" and "«total»" in j["error"]
    res = _aplicar(admin_client, "pagos.xlsx", contenido, confirmar="1")
    assert res.status_code == 400 and "«total»" in res.json()["detail"]


# ----------------------------------------------------------------------------
#                            Historial y estado
# ----------------------------------------------------------------------------
def test_historial_con_usuario_y_variacion(admin_client):
    _aplicar(admin_client, "pagos.xlsx", x_recaudo(10))
    _aplicar(admin_client, "procesos.xlsx", x_juridico(5))
    _aplicar(admin_client, "pagos.xlsx", x_recaudo(4), confirmar="1")
    _subir(admin_client, "/api/recaudo/upload", "malo.xlsx", x_juridico())      # falla: 400

    res = admin_client.get("/api/cargas/historial")
    assert res.status_code == 200
    h = res.json()
    assert [x["status"] for x in h] == ["failed", "success", "success", "success"]
    fallo, rec2, jur, rec1 = h
    assert rec2["tipo"] == "recaudo" and rec2["etiqueta"] == "Recaudo · plataforma nueva"
    assert rec2["usuario"] == "admin_test" and rec2["usuario_nombre"] == "Admin Test"
    assert rec2["registros"] == 4 and rec2["variacion_pct"] == -60.0
    assert rec1["registros"] == 10 and rec1["variacion_pct"] is None
    assert jur["tipo"] == "juridico" and jur["variacion_pct"] is None      # otro tipo: no se cruza
    assert fallo["error"] and fallo["variacion_pct"] is None and fallo["usuario"] == "admin_test"
    for x in h:
        assert x["started_at"] and x["duracion_seg"] is not None and x["source"]
    assert len(admin_client.get("/api/cargas/historial?limit=2").json()) == 2


def test_historial_distingue_cartera_base_de_incremental(admin_client):
    _aplicar(admin_client, MUESTRAS["cartera_vieja"][0], x_cartera())
    _aplicar(admin_client, MUESTRAS["cartera_nueva"][0], x_cartera(n=4, nueva=True))
    h = admin_client.get("/api/cargas/historial").json()
    assert [x["tipo"] for x in h] == ["cartera_nueva", "cartera_vieja"]
    assert h[0]["etiqueta"] == "Cartera · plataforma nueva"
    assert h[0]["variacion_pct"] is None        # no se compara incremental contra base


def test_estado_por_tipo(admin_client):
    e = admin_client.get("/api/cargas/estado").json()
    assert [x["tipo"] for x in e] == list(TIPOS)
    assert all(x["ultima_carga"] is None and x["dias"] is None for x in e)

    _aplicar(admin_client, "pagos.xlsx", x_recaudo(10))
    _aplicar(admin_client, MUESTRAS["cartera_vieja"][0], x_cartera())
    e = {x["tipo"]: x for x in admin_client.get("/api/cargas/estado").json()}
    assert e["recaudo"]["registros"] == 10 and e["recaudo"]["usuario"] == "admin_test"
    assert e["recaudo"]["dias"] == 0 and e["recaudo"]["ultima_carga"]
    assert e["recaudo"]["etiqueta"] == "Recaudo · plataforma nueva"
    assert e["cartera_vieja"]["registros"] == 10 and e["cartera_vieja"]["dias"] == 0
    assert e["cartera_nueva"]["ultima_carga"] is None
    assert e["juridico"]["ultima_carga"] is None


# ----------------------------------------------------------------------------
#                        Foto mensual de la cartera
# ----------------------------------------------------------------------------
def test_snapshot_tras_cargar_cartera_y_upsert_del_mes(admin_client, db):
    nombre = MUESTRAS["cartera_vieja"][0]
    assert _aplicar(admin_client, nombre, x_cartera(saldo=1_000_000)).status_code == 200
    mes = ahora_colombia().strftime("%Y-%m")
    s = db.execute("SELECT * FROM cartera_snapshots").fetchall()
    assert len(s) == 1
    s = s[0]
    assert s["mes"] == mes and s["fecha_corte"].startswith(mes)
    assert (s["total"], s["activos"]) == (10, 8)
    assert s["saldo_activo"] == 8_000_000
    assert (s["mora30_count"], s["mora30_saldo"], s["mora90_saldo"]) == (3, 3_000_000, 1_000_000)
    assert s["icv_pct"] == 37.5 and s["npl_pct"] == 12.5
    assert s["valor_desembolsado_mes"] == 10_000_000
    lote1 = s["sync_batch_id"]

    # Mismas definiciones que el resumen ejecutivo.
    cart = admin_client.get("/api/resumen/ejecutivo").json()["cartera"]
    assert cart["icv_pct"] == 37.5 and cart["saldo_activo"] == s["saldo_activo"]
    assert cart["mora_count"] == s["mora30_count"] and cart["activos"] == s["activos"]

    # Segunda carga del mismo mes (por el endpoint viejo): UPSERT, sigue 1 fila.
    res = _subir(admin_client, "/api/sync/upload-excel", nombre, x_cartera(n=6, saldo=2_000_000))
    assert res.status_code == 200
    db.commit()
    s2 = db.execute("SELECT * FROM cartera_snapshots").fetchall()
    assert len(s2) == 1
    assert (s2[0]["total"], s2[0]["activos"], s2[0]["saldo_activo"]) == (6, 4, 8_000_000)
    assert s2[0]["icv_pct"] == 75.0 and s2[0]["sync_batch_id"] > lote1

    # La incremental también actualiza la foto.
    assert _aplicar(admin_client, MUESTRAS["cartera_nueva"][0],
                    x_cartera(n=3, nueva=True), confirmar="1").status_code == 200
    db.commit()
    s3 = db.execute("SELECT * FROM cartera_snapshots").fetchall()
    assert len(s3) == 1 and s3[0]["total"] == 9


def test_endpoint_snapshots(admin_client, db):
    assert admin_client.get("/api/cartera/snapshots").json() == []
    _aplicar(admin_client, MUESTRAS["cartera_vieja"][0], x_cartera())
    # Dos meses anteriores sembrados a mano: el orden es ascendente por mes.
    for mes, icv in (("2025-03", 4.0), ("2024-12", 6.5)):
        db.execute("INSERT INTO cartera_snapshots (mes, fecha_corte, total, activos, saldo_activo, "
                   "icv_pct, por_linea, por_calificacion) VALUES (?, ?, 5, 5, 100, ?, '{}', '{}')",
                   (mes, mes + "-28", icv))
    db.commit()
    res = admin_client.get("/api/cartera/snapshots")
    assert res.status_code == 200
    lista = res.json()
    mes = ahora_colombia().strftime("%Y-%m")
    assert [x["mes"] for x in lista] == ["2024-12", "2025-03", mes]
    actual = lista[-1]
    assert actual["icv_pct"] == 37.5 and actual["npl_pct"] == 12.5 and actual["saldo_activo"] == 8_000_000
    assert actual["por_linea"] == {
        "CONSUMO": {"activos": 4, "saldo": 4_000_000, "mora30_saldo": 2_000_000},
        "POLIZAS": {"activos": 4, "saldo": 4_000_000, "mora30_saldo": 1_000_000},
    }
    assert actual["por_calificacion"]["C"] == {"activos": 3, "saldo": 3_000_000,
                                               "mora30_saldo": 3_000_000}
    assert actual["por_calificacion"]["A"]["activos"] == 5
    assert [x["mes"] for x in admin_client.get("/api/cartera/snapshots?meses=2").json()] == ["2025-03", mes]


def test_backfill_de_snapshot_al_arrancar(admin_client, db):
    from app.snapshots import backfill_snapshot_cartera
    backfill_snapshot_cartera()                     # sin cartera: no crea nada
    assert _n(db, "cartera_snapshots") == 0
    _aplicar(admin_client, MUESTRAS["cartera_vieja"][0], x_cartera())
    db.execute("DELETE FROM cartera_snapshots")
    db.commit()
    backfill_snapshot_cartera()
    assert _n(db, "cartera_snapshots") == 1
    db.execute("UPDATE cartera_snapshots SET total = 999")
    db.commit()
    backfill_snapshot_cartera()                     # ya hay fotos: no pisa nada
    assert db.execute("SELECT total FROM cartera_snapshots").fetchone()[0] == 999


def test_fallo_del_snapshot_no_tumba_la_carga(admin_client, db, monkeypatch):
    import app.snapshots as snapshots

    def _revienta(conn, batch_id):
        raise RuntimeError("boom")
    monkeypatch.setattr(snapshots, "guardar_snapshot_cartera", _revienta)
    res = _aplicar(admin_client, MUESTRAS["cartera_vieja"][0], x_cartera())
    assert res.status_code == 200 and res.json()["records"] == 10
    assert _n(db, "credits") == 10 and _n(db, "cartera_snapshots") == 0


# ----------------------------------------------------------------------------
#                                 Permisos
# ----------------------------------------------------------------------------
def test_permisos_viewer(viewer_client):
    contenido = x_recaudo()
    assert _analizar(viewer_client, "pagos.xlsx", contenido).status_code == 403
    assert _aplicar(viewer_client, "pagos.xlsx", contenido, confirmar="1").status_code == 403
    assert viewer_client.get("/api/cargas/historial").status_code == 403
    assert viewer_client.get("/api/cargas/estado").status_code == 200
    assert viewer_client.get("/api/cartera/snapshots").status_code == 200


def test_permisos_anonimo(client):
    contenido = x_recaudo()
    assert _analizar(client, "pagos.xlsx", contenido).status_code == 401
    assert _aplicar(client, "pagos.xlsx", contenido).status_code == 401
    assert client.get("/api/cargas/historial").status_code == 401
    assert client.get("/api/cargas/estado").status_code == 401
    assert client.get("/api/cartera/snapshots").status_code == 401
