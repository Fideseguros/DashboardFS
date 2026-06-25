"""Saldo de Cartera — snapshot agregado del archivo 'Resumen Estado Cuenta'
de la plataforma nueva de cartera.

Plantilla del archivo (`Resumen Estado Cuenta YYYYMMDD.xlsx`):
- Fila 1 = headers
- Filas 2..N = una por cuenta de cartera
- Columnas relevantes (1-indexed):
    9  Capital Prestamos
    10 Interes Corriente
    11 Interes De Mora      ← se MUESTRA pero NO suma al saldo_cartera
    12 Cargos Administrativos
    13 Deudores Varios
    14 Retencion En La Fuente
    17 Total                ← bruto del Excel (incluye Mora)
- Solo guardamos AGREGADOS — sin detalle por cuenta ni PII.
- saldo_cartera = total_general − total_int_mora.
"""
import re
import logging
import sqlite3
from datetime import date, datetime
from fastapi import APIRouter, Depends, UploadFile, File, Request, HTTPException
from app.database import get_db, get_connection
from app.auth.middleware import require_auth, require_superadmin
from app.sync.upload_helpers import upload_session, to_float

router = APIRouter(prefix="/api/saldo-cartera", tags=["saldo-cartera"])
_log = logging.getLogger("fide.saldo_cartera")
MAX_UPLOAD_MB = 25

# Detección de columnas POR NOMBRE de header (no por índice fijo). La
# plataforma cambia el orden/inserta columnas cada cierto tiempo (ej. 24-jun
# añadió "Gastos Aplicacion Y Almacenamiento Cloud" que desfasó Total), así
# que ubicar cada concepto por su título lo hace inmune a esos cambios.
#
# Cada concepto define un matcher: 'exact' (header == frase) o 'contains'
# (header contiene la frase). 'mora' usa frase completa para no confundirse
# con "Dias Mora"; 'total' usa exact para no agarrar "Total Cheque" etc.
COLUMN_SPECS = [
    ('capital',    'contains', 'capital prestamos'),
    ('int_corr',   'contains', 'interes corriente'),
    ('int_mora',   'contains', 'interes de mora'),
    ('cargos',     'contains', 'cargos administrativos'),
    ('deudores',   'contains', 'deudores varios'),
    ('retencion',  'contains', 'retencion en la fuente'),
    ('total',      'exact',    'total'),
]


def _norm_header(s):
    import unicodedata
    s = str(s or '').strip().lower()
    s = ' '.join(s.split())  # colapsa espacios múltiples
    return ''.join(c for c in unicodedata.normalize('NFD', s)
                   if unicodedata.category(c) != 'Mn')


# Columnas FIRMA del Resumen Estado Cuenta — existen en él pero NO en otros
# reportes que comparten columnas de montos (ej. IngresosDesembolso, que tiene
# Capital/Mora/Total pero es de movimientos). Si ninguna está, es otro archivo.
SIGNATURE_HEADERS = ['numero cuenta', 'saldo vigente', 'estado cobro']
# Columnas que delatan el archivo de MOVIMIENTOS (recaudo) — si están, NO es
# el Resumen Estado Cuenta aunque comparta columnas de montos.
MOVEMENT_HEADERS = ['fecha movimiento', 'tipo mvto']


def _resolve_columns(header_row):
    """Devuelve {concepto: índice} ubicando cada columna por su nombre.

    Lanza ValueError (con detalle) si falta alguna columna esperada o si el
    archivo no parece un Resumen Estado Cuenta (típico: archivo equivocado).
    """
    norm = [_norm_header(h) for h in header_row]
    norm_set = set(norm)

    def _has(frase):
        return any(frase in h for h in norm)

    # 1) Rechazar si tiene firma de archivo de movimientos (recaudo).
    if any(_has(m) for m in MOVEMENT_HEADERS):
        raise ValueError(
            "Este archivo parece ser de Recaudo/Movimientos, no el «Resumen Estado "
            "Cuenta». Sube el archivo correcto en este botón (el Resumen Estado "
            "Cuenta tiene una fila por cuenta, no por pago)."
        )
    # 2) Exigir al menos una columna firma del Resumen.
    if not any(_has(s) for s in SIGNATURE_HEADERS):
        raise ValueError(
            "Este archivo no parece ser el «Resumen Estado Cuenta» (no encontré "
            "columnas como «Numero Cuenta» o «Saldo Vigente»). Verifica que estés "
            "subiendo el archivo correcto en este botón."
        )
    # 3) Ubicar las columnas de montos por nombre.
    cols = {}
    faltan = []
    for concepto, modo, frase in COLUMN_SPECS:
        idx = None
        for i, h in enumerate(norm):
            if (modo == 'exact' and h == frase) or (modo == 'contains' and frase in h):
                idx = i
                break
        if idx is None:
            faltan.append(f"«{frase}»")
        else:
            cols[concepto] = idx
    if faltan:
        raise ValueError(
            "Este archivo no parece ser el «Resumen Estado Cuenta». Verifica que "
            "estés subiendo el archivo correcto en este botón. No encontré las "
            "columnas: " + ", ".join(faltan)
        )
    return cols


def _detect_snapshot_date(filename: str) -> str:
    """Extrae YYYYMMDD del nombre del archivo, fallback a hoy.

    Ejemplo: 'Resumen Estado Cuenta 20260604.xlsx' → '2026-06-04'.
    """
    if filename:
        m = re.search(r'(20\d{2})(\d{2})(\d{2})', filename)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
            except ValueError:
                pass
    return date.today().isoformat()


@router.post("/upload")
async def upload(request: Request, user=Depends(require_superadmin), file: UploadFile = File(...)):
    async with upload_session(
        request, user, file, source="saldo_cartera_upload", max_mb=MAX_UPLOAD_MB,
        generic_error_msg="No se pudo importar el archivo. Revisa el formato del Resumen Estado Cuenta."
    ) as ctx:
        rows = ctx.read_excel()
        if not rows or len(rows) < 2:
            raise ValueError("Archivo vacío o sin filas de datos")

        # Ubicar las columnas POR NOMBRE (robusto a inserción/reordenamiento
        # de columnas por parte de la plataforma). Lanza ValueError claro si
        # falta alguna (típico: archivo equivocado).
        cols = _resolve_columns(rows[0])
        c_cap, c_corr, c_mora = cols['capital'], cols['int_corr'], cols['int_mora']
        c_carg, c_deu, c_ret, c_tot = (cols['cargos'], cols['deudores'],
                                       cols['retencion'], cols['total'])
        max_col = max(cols.values())

        snapshot_date = _detect_snapshot_date(file.filename or "")

        total_capital = total_int_corr = total_int_mora = 0.0
        total_cargos = total_deudores = total_retencion = total_general = 0.0
        n = 0
        # Skip fila 1 (headers). Una fila válida tiene Numero Cuenta + algún monto.
        for r in rows[1:]:
            if not r or len(r) <= max_col:
                continue
            cuenta = r[0]
            if cuenta is None or str(cuenta).strip() == '':
                continue  # filas vacías al final
            n += 1
            total_capital      += to_float(r[c_cap]) or 0
            total_int_corr     += to_float(r[c_corr]) or 0
            total_int_mora     += to_float(r[c_mora]) or 0
            total_cargos       += to_float(r[c_carg]) or 0
            total_deudores     += to_float(r[c_deu]) or 0
            total_retencion    += to_float(r[c_ret]) or 0
            total_general      += to_float(r[c_tot]) or 0
        saldo_cartera = total_general - total_int_mora

        try:
            with get_db() as conn:
                # Reemplazar snapshot de la misma fecha si ya existe (re-upload del mismo día).
                conn.execute(
                    "DELETE FROM saldo_cartera_snapshots WHERE snapshot_date = ?",
                    (snapshot_date,)
                )
                conn.execute("""
                    INSERT INTO saldo_cartera_snapshots
                    (snapshot_date, n_cuentas, total_capital, total_int_corriente,
                     total_int_mora, total_cargos_admin, total_deudores_varios,
                     total_retencion_fuente, total_general, saldo_cartera, sync_batch_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (snapshot_date, n, total_capital, total_int_corr, total_int_mora,
                      total_cargos, total_deudores, total_retencion, total_general,
                      saldo_cartera, ctx.sync_id))
        except sqlite3.IntegrityError:
            # Race condition: otro upload del mismo snapshot_date ganó la carrera.
            raise HTTPException(
                status_code=409,
                detail=f"Otra carga del {snapshot_date} ya está procesándose. Espera unos segundos y reintenta."
            )

        ctx.set_counts(fetched=len(rows), inserted=n)
        ctx.set_audit_extra(f"snapshot_date={snapshot_date} n_cuentas={n} saldo={saldo_cartera:.0f}")
        return {
            "status": "success",
            "snapshot_date": snapshot_date,
            "n_cuentas": n,
            "saldo_cartera": saldo_cartera,
            "total_general": total_general,
            "total_int_mora": total_int_mora,
        }


#: Ajuste de saldo de cartera que no proviene del archivo de la plataforma
#: (cartera legacy no migrada / garantías). Se incorpora al saldo total por
#: política de gerencia. Mantener este valor sincronizado con contabilidad.
_AJUSTE_CARTERA = 110_398_316


@router.get("/latest")
def latest(_user=Depends(require_auth)):
    """Última snapshot disponible.

    `saldo_cartera` = (Total − Interés de Mora del archivo) + ajuste de
    cartera no incluida en el archivo. El endpoint NO expone el ajuste por
    separado — solo el saldo final consolidado.
    """
    conn = get_connection()
    try:
        r = conn.execute("""
            SELECT snapshot_date, n_cuentas, total_capital, total_int_corriente,
                   total_int_mora, total_cargos_admin, total_deudores_varios,
                   total_retencion_fuente, total_general, saldo_cartera, created_at
            FROM saldo_cartera_snapshots
            ORDER BY snapshot_date DESC, id DESC LIMIT 1
        """).fetchone()
        if not r:
            return {
                "snapshot_date": None,
                "n_cuentas": 0,
                "total_capital": 0, "total_int_corriente": 0, "total_int_mora": 0,
                "total_cargos_admin": 0, "total_deudores_varios": 0,
                "total_retencion_fuente": 0, "total_general": 0,
                "saldo_cartera": _AJUSTE_CARTERA,
                "created_at": None,
            }
        d = dict(r)
        saldo_real = d.get('saldo_cartera') or 0  # = total_general - total_int_mora del archivo
        d['saldo_cartera'] = saldo_real + _AJUSTE_CARTERA
        return d
    finally:
        conn.close()


@router.get("/history")
def history(limit: int = 60, _user=Depends(require_auth)):
    """Histórico de snapshots para sparkline / evolución."""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT snapshot_date, saldo_cartera, total_general, total_int_mora, n_cuentas
            FROM saldo_cartera_snapshots
            ORDER BY snapshot_date DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
