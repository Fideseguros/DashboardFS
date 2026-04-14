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
    source TEXT DEFAULT 'acano_api',
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
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    last_login TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
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
    conn.close()
