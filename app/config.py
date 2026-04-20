import os

APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "dev-secret-change-in-production")
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/fide.db")

# Session
SESSION_EXPIRY_HOURS = int(os.getenv("SESSION_EXPIRY_HOURS", "4"))

# Rate limiting (failed logins per IP)
LOGIN_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_LOCKOUT_MINUTES = int(os.getenv("LOGIN_LOCKOUT_MINUTES", "15"))

# Cookie security — require HTTPS in production
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "1") == "1"
