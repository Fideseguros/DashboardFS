"""Excel column mapping for Cartera_Consolidado upload."""

# Excel column index -> internal field name.
# Matches the structure of "Cartera Total Mundosoft.xlsx" / sheet "Cartera_Consolidado".
EXCEL_COL_MAP = {
    0: 'cuenta', 1: 'solicitud', 2: 'identificacion', 3: 'cliente',
    4: 'estado', 5: 'linea', 6: 'valor_credito', 7: 'saldo_capital',
    8: 'saldo_favor', 9: 'valor_cuota', 10: 'fecha_inicio', 11: 'fecha_vencimiento',
    12: 'fecha_ult_pago', 13: 'calificacion', 16: 'fecha_desembolso',
    17: 'tasa_efectiva', 19: 'plazo', 27: 'cuotas_pactadas', 28: 'cuotas_pagadas',
    37: 'dias_mora', 38: 'maxima_mora', 50: 'ciudad', 52: 'aliado'
}
