import os

APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "dev-secret-change-in-production")
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/fide.db")

# Session
# Máximo absoluto de una sesión (10 h: cubre la jornada completa). La sesión
# muere antes si pasa SESSION_IDLE_MINUTES sin actividad (expiración deslizante).
SESSION_EXPIRY_HOURS = int(os.getenv("SESSION_EXPIRY_HOURS", "10"))
SESSION_IDLE_MINUTES = int(os.getenv("SESSION_IDLE_MINUTES", "60"))
# El middleware actualiza last_seen_at como máximo cada N segundos (no en cada request).
SESSION_TOUCH_SECONDS = int(os.getenv("SESSION_TOUCH_SECONDS", "60"))

# Política de contraseñas
PASSWORD_MIN_LENGTH = int(os.getenv("PASSWORD_MIN_LENGTH", "12"))

# Verificación en dos pasos (TOTP)
TOTP_ISSUER = os.getenv("TOTP_ISSUER", "FIDES Seguro")
TOTP_PENDING_MINUTES = int(os.getenv("TOTP_PENDING_MINUTES", "5"))
TOTP_MAX_FAILED = int(os.getenv("TOTP_MAX_FAILED", "5"))

# Rate limiting (failed logins per IP)
LOGIN_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_LOCKOUT_MINUTES = int(os.getenv("LOGIN_LOCKOUT_MINUTES", "15"))

# Cookie security — require HTTPS in production
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "1") == "1"
