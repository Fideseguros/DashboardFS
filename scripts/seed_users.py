"""Create initial user accounts."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bcrypt as _bcrypt
from app.database import get_db


def hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode('utf-8'), _bcrypt.gensalt()).decode('utf-8')

USERS = [
    {"username": "admin", "password": "FideSeguros2026!", "display_name": "Administrador"},
    # Add partner accounts here:
    # {"username": "socio1", "password": "password123", "display_name": "Socio 1"},
]

if __name__ == "__main__":
    with get_db() as conn:
        for user in USERS:
            existing = conn.execute("SELECT id FROM users WHERE username = ?", (user["username"],)).fetchone()
            if existing:
                print(f"  User '{user['username']}' already exists, skipping.")
                continue
            password_hash = hash_password(user["password"])
            conn.execute(
                "INSERT INTO users (username, password_hash, display_name) VALUES (?, ?, ?)",
                (user["username"], password_hash, user["display_name"])
            )
            print(f"  Created user: {user['username']}")
    print("Done.")
