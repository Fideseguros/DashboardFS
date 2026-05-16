import sqlite3
import os
from contextlib import contextmanager
from app.config import DATABASE_PATH

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sync_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    records_fetched INTEGER DEFAULT 0,
    records_inserted INTEGER DEFAULT 0,
    error_message TEXT,
    source TEXT DEFAULT 'manual_upload',
    uploaded_by INTEGER,
    duration_seconds REAL
);

CREATE TABLE IF NOT EXISTS credits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cuenta TEXT,
    solicitud TEXT,
    identificacion TEXT NOT NULL,
    cliente TEXT NOT NULL,
    estado TEXT NOT NULL,
    linea TEXT,
    valor_credito REAL,
    saldo_capital REAL,
    saldo_favor REAL,
    valor_cuota REAL,
    fecha_inicio TEXT,
    fecha_vencimiento TEXT,
    fecha_ult_pago TEXT,
    calificacion TEXT,
    fecha_desembolso TEXT,
    tasa_efectiva REAL,
    plazo INTEGER,
    cuotas_pactadas INTEGER,
    cuotas_pagadas INTEGER,
    dias_mora INTEGER DEFAULT 0,
    maxima_mora INTEGER DEFAULT 0,
    ciudad TEXT,
    aliado TEXT,
    sync_batch_id INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (sync_batch_id) REFERENCES sync_logs(id)
);

CREATE INDEX IF NOT EXISTS idx_credits_estado ON credits(estado);
CREATE INDEX IF NOT EXISTS idx_credits_linea ON credits(linea);
CREATE INDEX IF NOT EXISTS idx_credits_aliado ON credits(aliado);
CREATE INDEX IF NOT EXISTS idx_credits_ciudad ON credits(ciudad);
CREATE INDEX IF NOT EXISTS idx_credits_calificacion ON credits(calificacion);
CREATE INDEX IF NOT EXISTS idx_credits_sync ON credits(sync_batch_id);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    role TEXT NOT NULL DEFAULT 'viewer',
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    last_login TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    ip TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip TEXT NOT NULL,
    username TEXT,
    success INTEGER NOT NULL DEFAULT 0,
    attempted_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip, attempted_at);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT,
    action TEXT NOT NULL,
    details TEXT,
    ip TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_logs(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action, created_at);

-- ============== Módulo Recaudo (Pagos / Ingresos) ==============
CREATE TABLE IF NOT EXISTS pagos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entidad TEXT,
    linea_credito TEXT,
    fecha_movimiento TEXT,
    fecha_documento TEXT,
    identificacion TEXT,         -- cifrado
    cliente TEXT,                -- cifrado
    cuenta TEXT,
    solicitud TEXT,
    aliado TEXT,
    tipo_mvto TEXT,
    tipo_documento TEXT,
    documento TEXT,
    usuario TEXT,
    capital REAL DEFAULT 0,
    interes_corriente REAL DEFAULT 0,
    interes_mora REAL DEFAULT 0,
    iva REAL DEFAULT 0,
    saldo_favor REAL DEFAULT 0,
    gastos_pj REAL DEFAULT 0,
    cargos_admin REAL DEFAULT 0,
    total REAL DEFAULT 0,
    total_cheque REAL DEFAULT 0,
    total_efectivo REAL DEFAULT 0,
    total_tarjeta REAL DEFAULT 0,
    total_interno REAL DEFAULT 0,
    autorizacion TEXT,
    observaciones TEXT,
    sync_batch_id INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pagos_fecha ON pagos(fecha_movimiento);
CREATE INDEX IF NOT EXISTS idx_pagos_aliado ON pagos(aliado);
CREATE INDEX IF NOT EXISTS idx_pagos_sync ON pagos(sync_batch_id);

-- ============== Módulo Solicitudes (Pipeline) ==============
CREATE TABLE IF NOT EXISTS solicitudes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    solicitud_origen TEXT,
    solicitud TEXT,
    hoja_ruta TEXT,
    linea TEXT,
    identificacion TEXT,         -- cifrado
    solicitante TEXT,            -- cifrado
    tipo_moneda TEXT,
    valor REAL DEFAULT 0,
    paso_ruta TEXT,
    responsable TEXT,
    estado TEXT,
    subestado TEXT,
    empresa TEXT,
    oficina TEXT,
    fecha_solicitud TEXT,
    periodo_convocatoria TEXT,
    auxilio TEXT,
    usuario TEXT,
    sync_batch_id INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_solicitudes_estado ON solicitudes(estado);
CREATE INDEX IF NOT EXISTS idx_solicitudes_fecha ON solicitudes(fecha_solicitud);
CREATE INDEX IF NOT EXISTS idx_solicitudes_sync ON solicitudes(sync_batch_id);

-- ============== Recaudo Histórico (plataforma vieja, AGREGADO por id_prestamo) ==============
CREATE TABLE IF NOT EXISTS pagos_legacy (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    id_prestamo TEXT NOT NULL,
    id_solicitud TEXT,
    identificacion TEXT,         -- cifrado
    nombre TEXT,                 -- cifrado
    num_pagos INTEGER DEFAULT 0,
    valor_pago_total REAL DEFAULT 0,
    iva_pagado_total REAL DEFAULT 0,
    cargos_netos_total REAL DEFAULT 0,
    interes_mora_total REAL DEFAULT 0,
    fecha_primer_pago TEXT,
    fecha_ultimo_pago TEXT,
    prestamo_cancelado TEXT,
    metodo_pago_principal TEXT,
    sync_batch_id INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pagos_legacy_prestamo ON pagos_legacy(id_prestamo);
CREATE INDEX IF NOT EXISTS idx_pagos_legacy_sync ON pagos_legacy(sync_batch_id);

-- ============== Solicitudes Histórico (plataforma vieja) ==============
CREATE TABLE IF NOT EXISTS solicitudes_legacy (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    id_solicitud TEXT,
    id_entidad TEXT,
    fecha_solicitud TEXT,
    tipo_identificacion TEXT,
    identificacion TEXT,            -- cifrado
    nombre_completo TEXT,            -- cifrado
    originador TEXT,
    producto TEXT,
    estado TEXT,
    estado_precalif TEXT,
    fecha_desembolso TEXT,
    monto REAL DEFAULT 0,
    plazo_dias INTEGER,
    numero_cuotas INTEGER,
    frecuencia_pagos TEXT,
    fecha_inicio_pagos TEXT,
    tasa_interes REAL,
    canal TEXT,
    genero TEXT,
    edad INTEGER,
    departamento TEXT,
    ciudad TEXT,
    nombre_banco TEXT,
    tipo_solicitud TEXT,
    asesor_comercial TEXT,
    decision_modelo TEXT,
    cliente_recurrente TEXT,
    sync_batch_id INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_solic_legacy_estado ON solicitudes_legacy(estado);
CREATE INDEX IF NOT EXISTS idx_solic_legacy_fecha ON solicitudes_legacy(fecha_solicitud);
CREATE INDEX IF NOT EXISTS idx_solic_legacy_sync ON solicitudes_legacy(sync_batch_id);

-- ============== Estados Financieros (Estado de Resultados mensual) ==============
CREATE TABLE IF NOT EXISTS estados_financieros (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,           -- 1-12
    cuenta_code TEXT NOT NULL,        -- e.g. '41502001'
    cuenta_descripcion TEXT,          -- 'INTERESES FINANCIACION POLIZAS'
    nivel INTEGER NOT NULL,           -- profundidad PUC: 1=grupo, 2=cuenta, 4=subgrupo, 6=cuenta detalle, 8=auxiliar
    parent_code TEXT,                 -- código del nivel padre
    is_total INTEGER DEFAULT 0,       -- 1 si es fila Total, 0 si es cuenta
    valor REAL DEFAULT 0,
    sync_batch_id INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ef_year_month ON estados_financieros(year, month);
CREATE INDEX IF NOT EXISTS idx_ef_cuenta ON estados_financieros(cuenta_code);
CREATE INDEX IF NOT EXISTS idx_ef_sync ON estados_financieros(sync_batch_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ef_year_month_cuenta ON estados_financieros(year, month, cuenta_code);

-- ============== Módulo Cobro Jurídico (Procesos) ==============
CREATE TABLE IF NOT EXISTS procesos_juridicos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identificacion TEXT,         -- cifrado
    nombre TEXT,                 -- cifrado
    naturaleza_litigio TEXT,
    avance TEXT,
    respuesta_compania TEXT,
    probabilidad TEXT,
    medida_cautelar TEXT,
    juzgado TEXT,
    sync_batch_id INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_juridico_prob ON procesos_juridicos(probabilidad);
CREATE INDEX IF NOT EXISTS idx_juridico_sync ON procesos_juridicos(sync_batch_id);
"""

CREDIT_FIELDS = [
    'cuenta', 'solicitud', 'identificacion', 'cliente', 'estado', 'linea',
    'valor_credito', 'saldo_capital', 'saldo_favor', 'valor_cuota',
    'fecha_inicio', 'fecha_vencimiento', 'fecha_ult_pago', 'calificacion',
    'fecha_desembolso', 'tasa_efectiva', 'plazo', 'cuotas_pactadas',
    'cuotas_pagadas', 'dias_mora', 'maxima_mora', 'ciudad', 'aliado'
]


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DATABASE_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.executescript(SCHEMA_SQL)
    _migrate_existing_schema(conn)
    conn.close()


def _migrate_existing_schema(conn):
    """Add new columns/tables to existing DBs without breaking."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "role" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'viewer'")
    sess_cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
    if "ip" not in sess_cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN ip TEXT")
    log_cols = [r[1] for r in conn.execute("PRAGMA table_info(sync_logs)").fetchall()]
    if "uploaded_by" not in log_cols:
        conn.execute("ALTER TABLE sync_logs ADD COLUMN uploaded_by INTEGER")
    conn.commit()
