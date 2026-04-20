"""Fide Seguros Dashboard - FastAPI Application."""
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pathlib import Path

from app.database import init_db, get_connection
from app.routes import auth, credits, sync

app = FastAPI(title="Fide Seguros Dashboard", version="2.0.0")

app.include_router(auth.router)
app.include_router(credits.router)
app.include_router(sync.router)

TEMPLATES_DIR = Path(__file__).parent / "templates"


@app.on_event("startup")
def startup():
    init_db()


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
