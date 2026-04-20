"""Fide Seguros Dashboard - FastAPI Application."""
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pathlib import Path

import os
import logging
from app.database import init_db, get_connection, get_db
from app.routes import auth, credits, sync

app = FastAPI(title="Fide Seguros Dashboard", version="2.0.0")

app.include_router(auth.router)
app.include_router(credits.router)
app.include_router(sync.router)

TEMPLATES_DIR = Path(__file__).parent / "templates"
_log = logging.getLogger("fide.startup")


def _bootstrap_admin():
    """Create the first superadmin from BOOTSTRAP_ADMIN_USER/PASSWORD env vars
    if the users table is empty. No-op on subsequent startups."""
    admin_user = os.getenv("BOOTSTRAP_ADMIN_USER", "").strip()
    admin_pass = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")
    if not admin_user or not admin_pass:
        return
    import bcrypt as _bcrypt
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
        if row and row["cnt"] > 0:
            return
        pwd_hash = _bcrypt.hashpw(admin_pass.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")
        conn.execute(
            "INSERT INTO users (username, password_hash, display_name, role) "
            "VALUES (?, ?, ?, 'superadmin')",
            (admin_user, pwd_hash, "Administrador")
        )
    _log.warning("Bootstrap admin '%s' creado. Elimina BOOTSTRAP_ADMIN_* de Railway.", admin_user)


@app.on_event("startup")
def startup():
    init_db()
    _bootstrap_admin()


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    # Strict-Transport-Security is only meaningful over HTTPS (Railway serves HTTPS by default).
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
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
