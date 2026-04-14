"""Initialize or migrate the database schema."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import init_db, DATABASE_PATH

if __name__ == "__main__":
    print(f"Initializing database at: {DATABASE_PATH}")
    init_db()
    print("Schema created successfully.")
