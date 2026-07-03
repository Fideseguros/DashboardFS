"""Fide Seguros Dashboard - FastAPI Application."""
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.gzip import GZipMiddleware
from pathlib import Path

import os
import logging
from datetime import datetime
from app.database import init_db, get_connection, get_db, backfill_masked_pii
from app.routes import auth, credits, sync, users, financieros, saldo_cartera, habeas_data, cliente, resumen
from app.routes.extras import recaudo, solicitudes as solicitudes_router, juridico

app = FastAPI(title="Fide Seguros Dashboard", version="2.0.0")

# Comprime respuestas JSON > 500 bytes. Los endpoints de cartera/recaudo/
# solicitudes devuelven listas grandes que se reducen 70-90% con gzip.
# level=5: ~mismo ratio que 6 con menos CPU por respuesta (mejor en Railway).
app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=5)

app.include_router(auth.router)
app.include_router(credits.router)
app.include_router(sync.router)
app.include_router(users.router)
app.include_router(recaudo)
app.include_router(solicitudes_router)
app.include_router(juridico)
app.include_router(financieros.router)
app.include_router(saldo_cartera.router)
app.include_router(habeas_data.router)
app.include_router(cliente.router)
app.include_router(resumen.router)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
_log = logging.getLogger("fide.startup")


def _bootstrap_admin():
    """Create or reset the superadmin from BOOTSTRAP_ADMIN_USER/PASSWORD env vars.

    Auditoría A4: una vez ejecutado, marcamos bootstrap_done=YYYY-MM-DD en
    kv_store. Si las env vars siguen presentes después (alguien las dejó
    colgadas en Railway), emitimos WARNING ruidoso EN CADA STARTUP — para
    que sea visible que es un riesgo de seguridad y se eliminen.

    El bootstrap NO se re-ejecuta automáticamente: una vez done, ignoramos
    las env vars (a menos que se borre la fila kv_store.bootstrap_done a
    mano, lo cual requiere acceso a la BD). Esto previene rotación
    accidental de password cada vez que reinicia el contenedor.
    """
    admin_user = os.getenv("BOOTSTRAP_ADMIN_USER", "").strip()
    admin_pass = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")
    if not admin_user or not admin_pass:
        return

    with get_db() as conn:
        done_row = conn.execute(
            "SELECT value FROM kv_store WHERE key = 'bootstrap_done'"
        ).fetchone()
        if done_row:
            _log.warning(
                "SECURITY: BOOTSTRAP_ADMIN_* sigue seteado en env pero "
                "bootstrap ya se ejecutó (done=%s). Elimina las variables "
                "de Railway → Settings → Variables AHORA.",
                done_row["value"]
            )
            return

        import bcrypt as _bcrypt
        pwd_hash = _bcrypt.hashpw(admin_pass.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")
        existing = conn.execute(
            "SELECT id FROM users WHERE username = ?", (admin_user,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE users SET password_hash = ?, role = 'superadmin', is_active = 1 "
                "WHERE id = ?",
                (pwd_hash, existing["id"])
            )
            _log.warning("Bootstrap: contraseña de '%s' reseteada. ELIMINA BOOTSTRAP_ADMIN_* de Railway ahora.", admin_user)
        else:
            conn.execute(
                "INSERT INTO users (username, password_hash, display_name, role) "
                "VALUES (?, ?, ?, 'superadmin')",
                (admin_user, pwd_hash, "Administrador")
            )
            _log.warning("Bootstrap admin '%s' creado. ELIMINA BOOTSTRAP_ADMIN_* de Railway ahora.", admin_user)
        # Marcar como done para que cualquier startup posterior solo emita
        # el WARNING (no rote password de nuevo).
        conn.execute(
            "INSERT OR REPLACE INTO kv_store (key, value, updated_at) "
            "VALUES ('bootstrap_done', ?, datetime('now'))",
            (datetime.utcnow().isoformat(),)
        )


def _cleanup_old_data():
    """Retención de datos (Habeas Data M7): purgar logs muy viejos.

    - audit_logs > 2 años: SIC acepta plazos razonables de retención para
      acciones administrativas. Más de 2 años de logs de reveal/export no
      tiene utilidad operativa y acumula trazas de PII (aunque ahora
      están scrubbed con hash HMAC, mejor no acumularlas).
    - sync_logs no manual_upload > 180 días: ya teníamos cleanup para
      manual_upload (30 días); extender a los demás sources.
    """
    try:
        with get_db() as conn:
            r1 = conn.execute(
                "DELETE FROM audit_logs WHERE created_at < datetime('now', '-2 years')"
            )
            r2 = conn.execute(
                "DELETE FROM sync_logs WHERE source != 'manual_upload' "
                "AND started_at < datetime('now', '-180 days') AND status != 'running'"
            )
            if r1.rowcount or r2.rowcount:
                _log.info("retention cleanup: audit_logs=%d sync_logs=%d",
                          r1.rowcount, r2.rowcount)
    except Exception:
        _log.exception("retention cleanup failed (non-fatal)")


@app.on_event("startup")
def startup():
    init_db()
    _bootstrap_admin()
    _cleanup_old_data()
    # Backfill de PII enmascarada para acelerar /api/credits.
    # Solo corre si hay filas pendientes — idempotente.
    backfill_masked_pii()


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    # Strict-Transport-Security is only meaningful over HTTPS (Railway serves HTTPS by default).
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # CSP defensiva contra XSS (issue auditoría A2). Permite 'unsafe-inline'
    # porque dashboard.html tiene ~3000 líneas de JS+CSS inline; refactor a
    # archivos externos es trabajo grande. Lo que SÍ bloqueamos:
    #   - connect-src 'self': fetch() solo al propio dominio → un XSS no
    #     puede exfiltrar a un dominio atacante.
    #   - frame-ancestors 'none': nadie puede embeber en iframe (clickjacking).
    #   - object-src 'none': sin Flash/applet/PDF embebido.
    # Se permiten CDNs específicos para Chart.js y fonts.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    return response


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "fide-dashboard"}


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return (TEMPLATES_DIR / "login.html").read_text(encoding="utf-8")


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    token = request.cookies.get("fide_token", "")
    if not token:
        return RedirectResponse(url="/login", status_code=302)

    conn = get_connection()
    try:
        session = conn.execute(
            "SELECT 1 FROM sessions WHERE token = ? AND expires_at > datetime('now')",
            (token,)
        ).fetchone()
    finally:
        conn.close()

    if not session:
        return RedirectResponse(url="/login", status_code=302)

    return (TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")
