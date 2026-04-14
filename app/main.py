"""Fide Seguros Dashboard - FastAPI Application."""
from fastapi import FastAPI, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from app.database import init_db
from app.auth.middleware import require_auth
from app.routes import auth, credits, sync

app = FastAPI(title="Fide Seguros Dashboard", version="1.0.0")

# Include routers
app.include_router(auth.router)
app.include_router(credits.router)
app.include_router(sync.router)

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Initialize DB on startup
@app.on_event("startup")
def startup():
    init_db()


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "fide-dashboard"}


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return (TEMPLATES_DIR / "login.html").read_text(encoding="utf-8")


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    # Check cookie-based auth for page load (not API)
    token = request.cookies.get("fide_token", "")
    if not token:
        return RedirectResponse(url="/login", status_code=302)

    from app.database import get_connection
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
