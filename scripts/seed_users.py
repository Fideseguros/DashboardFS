"""Create or update initial user accounts.

Usage (locally or on Railway):
    python -m scripts.seed_users

Roles disponibles:
    - superadmin : todos los permisos (subir Excel, exportar CSV completo,
                   ver PII descifrada, revisar todo).
    - viewer     : ver dashboard (PII enmascarada) + exportar CSV enmascarado.
    - consulta   : solo consulta en pantalla. Sin export, sin subir, sin reveal.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bcrypt as _bcrypt
from app.database import get_db, init_db


def hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode('utf-8'), _bcrypt.gensalt()).decode('utf-8')


# Contraseñas SIEMPRE desde env vars — NUNCA hardcoded en este archivo.
# Si SEED_ADMIN_PASSWORD no está seteada, el script aborta antes de tocar la BD.
# Auditoría de seguridad: una contraseña commiteada al repo es Habeas Data
# crítico — multa SIC hasta 2000 SMMLV si la cuenta admin se compromete.
USERS_SPEC = [
    {
        "username": "admin",
        "password_env": "SEED_ADMIN_PASSWORD",
        "display_name": "Administrador",
        "role": "superadmin",
    },
    # Para agregar más usuarios: definir SEED_<USER>_PASSWORD en env.
]


if __name__ == "__main__":
    # Recolectar contraseñas desde env ANTES de tocar BD.
    users = []
    for spec in USERS_SPEC:
        pwd = os.environ.get(spec["password_env"])
        if not pwd:
            print(f"ERROR: env var {spec['password_env']} no está seteada. Aborto.")
            print(f"  Local: export {spec['password_env']}='tu-contraseña-fuerte'")
            print(f"  Railway: configura la variable en Settings → Variables")
            sys.exit(1)
        if len(pwd) < 12:
            print(f"ERROR: {spec['password_env']} debe tener ≥12 caracteres. Aborto.")
            sys.exit(1)
        users.append({**spec, "password": pwd})

    init_db()
    with get_db() as conn:
        for user in users:
            existing = conn.execute("SELECT id FROM users WHERE username = ?",
                                    (user["username"],)).fetchone()
            if existing:
                conn.execute(
                    "UPDATE users SET role = ?, is_active = 1 WHERE username = ?",
                    (user["role"], user["username"])
                )
                print(f"  Updated role for '{user['username']}' -> {user['role']}")
                continue
            password_hash = hash_password(user["password"])
            conn.execute(
                "INSERT INTO users (username, password_hash, display_name, role) "
                "VALUES (?, ?, ?, ?)",
                (user["username"], password_hash, user["display_name"], user["role"])
            )
            print(f"  Created user: {user['username']} ({user['role']})")
    print("Done.")
