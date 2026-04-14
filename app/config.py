import os

APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "dev-secret-change-in-production")
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/fide.db")

# ACANO
ACANO_BASE_URL = os.getenv("ACANO_BASE_URL", "https://acano.fideseguros.com/api")
ACANO_AUTH_TOKEN = os.getenv("ACANO_AUTH_TOKEN", "")
ACANO_WSDL_URL = os.getenv("ACANO_WSDL_URL", "")
ACANO_PRODUCTS_ENDPOINT = os.getenv("ACANO_PRODUCTS_ENDPOINT", "/consulta-productos")

SESSION_EXPIRY_DAYS = int(os.getenv("SESSION_EXPIRY_DAYS", "30"))
