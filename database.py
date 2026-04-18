"""
Database layer – SQLite via Flask's g object
"""

import sqlite3
import os
from flask import g, current_app

DB_PATH = os.path.join(os.path.dirname(__file__), "rsvp.db")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
    return g.db


def init_db():
    """Create tables if they don't exist yet."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS guests (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT    NOT NULL,
            phone     TEXT    NOT NULL UNIQUE,
            rsvp      TEXT    DEFAULT NULL,   -- 'yes' | 'no' | NULL
            rsvp_time TEXT    DEFAULT NULL,
            last_sent TEXT    DEFAULT NULL
        )
    """)
    conn.commit()
    conn.close()
    print("✅ Database ready:", DB_PATH)
