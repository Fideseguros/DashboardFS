"""Create or update initial user accounts.

Usage (locally or on Railway):
    python -m scripts.seed_users
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
    # Example viewer accounts:
    # {"username": "consulta", "password": "temporal123", "display_name": "Consulta", "role": "viewer"},
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
