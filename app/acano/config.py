"""Mapeo de columnas del Informe de Cartera, POR NOMBRE de encabezado.

Antes esto era un dict {índice_fijo: campo}. Problema (bug recurrente del
proyecto): la plataforma inserta o reordena columnas sin avisar, y con
índices fijos 'estado' pasaba a leer 'linea', el ICV se volvía basura y NO
había ningún error — el dashboard mostraba cifras equivocadas en silencio.
Ya migramos Recaudo y Saldo Cartera a detección por nombre; esto hace lo
mismo con Cartera, que es el módulo principal.

Cada campo lista uno o más nombres de encabezado posibles (sinónimos). Los
tres archivos de cartera (vieja, nueva, consolidado) comparten casi todos
los nombres, salvo:
  - valor_credito : 'Valor_credito'  vs 'VALOR CRÉDITO'   (tilde + espacio)
  - fecha_desembolso: 'Fecha_desembolso' vs 'FECHA DESEMBOLSO'
  - aliado        : 'Aliado_vendedor' vs 'INTERMEDIARIO'   (nombre distinto)
La normalización (minúsculas, sin tildes, '_'/espacios colapsados) absorbe
los dos primeros; el tercero se cubre con sinónimo explícito.

CAMPOS CRÍTICOS: si falta su columna, el upload se rechaza (el archivo no es
una cartera válida). CAMPOS OPCIONALES: si falta, ese dato queda en None y
se registra en el log, pero el upload continúa — no tiramos toda la carga
por una columna secundaria renombrada.
"""

# campo_interno -> (lista de nombres de header aceptados, crítico?)
# Los nombres se comparan ya normalizados por _norm_header (ver transformer).
CARTERA_COLUMNS = {
    'cuenta':            (['cuenta'], True),
    'solicitud':         (['solicitud'], False),
    'identificacion':    (['identificacion'], True),
    'cliente':           (['cliente'], True),
    'estado':            (['estado'], True),
    'linea':             (['linea'], False),
    'valor_credito':     (['valor credito'], False),
    'saldo_capital':     (['saldo capital'], True),
    'saldo_favor':       (['saldo favor'], False),
    'valor_cuota':       (['valor cuota'], False),
    'fecha_inicio':      (['fecha inicio'], False),
    'fecha_vencimiento': (['fecha vencimiento'], False),
    'fecha_ult_pago':    (['fecha ult pago', 'fecha ultimo pago'], False),
    'calificacion':      (['calificacion contable', 'calificacion'], False),
    'fecha_desembolso':  (['fecha desembolso'], False),
    'tasa_efectiva':     (['tasa efectiva'], False),
    'plazo':             (['plazo'], False),
    'cuotas_pactadas':   (['cuotas pactadas'], False),
    'cuotas_pagadas':    (['cuotas pagadas'], False),
    'dias_mora':         (['dias mora'], False),
    'maxima_mora':       (['maxima mora'], False),
    'ciudad':            (['ciudad'], False),
    'aliado':            (['aliado vendedor', 'intermediario', 'aliado'], False),
}

# Fallback de compatibilidad: los índices fijos históricos. Solo se usa si la
# resolución por nombre no encuentra NINGÚN encabezado conocido (ej. un export
# raro sin fila de títulos). Documentado, no es la ruta principal.
EXCEL_COL_MAP_FALLBACK = {
    0: 'cuenta', 1: 'solicitud', 2: 'identificacion', 3: 'cliente',
    4: 'estado', 5: 'linea', 6: 'valor_credito', 7: 'saldo_capital',
    8: 'saldo_favor', 9: 'valor_cuota', 10: 'fecha_inicio', 11: 'fecha_vencimiento',
    12: 'fecha_ult_pago', 13: 'calificacion', 16: 'fecha_desembolso',
    17: 'tasa_efectiva', 19: 'plazo', 27: 'cuotas_pactadas', 28: 'cuotas_pagadas',
    37: 'dias_mora', 38: 'maxima_mora', 50: 'ciudad', 52: 'aliado'
}
