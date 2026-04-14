"""
ACANO field mapping and configuration.
When ACANO renames fields or changes endpoints, edit ONLY this file.
"""

# Map ACANO API field names -> our internal schema field names.
# Update this dict when ACANO changes its response structure.
FIELD_MAP = {
    "NumeroCuenta": "cuenta",
    "NumeroSolicitud": "solicitud",
    "Identificacion": "identificacion",
    "NombreCliente": "cliente",
    "EstadoCredito": "estado",
    "LineaProducto": "linea",
    "ValorCredito": "valor_credito",
    "SaldoCapital": "saldo_capital",
    "SaldoFavor": "saldo_favor",
    "ValorCuota": "valor_cuota",
    "FechaInicio": "fecha_inicio",
    "FechaVencimiento": "fecha_vencimiento",
    "FechaUltimoPago": "fecha_ult_pago",
    "Calificacion": "calificacion",
    "FechaDesembolso": "fecha_desembolso",
    "TasaEfectiva": "tasa_efectiva",
    "Plazo": "plazo",
    "CuotasPactadas": "cuotas_pactadas",
    "CuotasPagadas": "cuotas_pagadas",
    "DiasMora": "dias_mora",
    "MaximaMora": "maxima_mora",
    "Ciudad": "ciudad",
    "Aliado": "aliado",
}

# Excel column index -> our internal field name (for upload fallback)
EXCEL_COL_MAP = {
    0: 'cuenta', 1: 'solicitud', 2: 'identificacion', 3: 'cliente',
    4: 'estado', 5: 'linea', 6: 'valor_credito', 7: 'saldo_capital',
    8: 'saldo_favor', 9: 'valor_cuota', 10: 'fecha_inicio', 11: 'fecha_vencimiento',
    12: 'fecha_ult_pago', 13: 'calificacion', 16: 'fecha_desembolso',
    17: 'tasa_efectiva', 19: 'plazo', 27: 'cuotas_pactadas', 28: 'cuotas_pagadas',
    37: 'dias_mora', 38: 'maxima_mora', 50: 'ciudad', 52: 'aliado'
}
