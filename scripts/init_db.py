#!/usr/bin/env python3
"""Initialize the APEX SQLite database."""
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from apex.config import get_settings
from apex.db.connection import init_db

if __name__ == "__main__":
    settings = get_settings()
    print(f"Initializing database at: {settings.apex_db_path}")
    conn = init_db(settings.apex_db_path)
    print("Database initialized successfully.")
    conn.close()
