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


USERS = [
    {"username": "admin", "password": "FideSeguros2026!", "display_name": "Administrador", "role": "superadmin"},
    # Ejemplos (descomentar / ajustar según necesidad):
    # {"username": "operador", "password": "temp456",  "display_name": "Operador",  "role": "viewer"},
    # {"username": "consulta", "password": "temp789",  "display_name": "Consulta",  "role": "consulta"},
]


if __name__ == "__main__":
    init_db()
    with get_db() as conn:
        for user in USERS:
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
