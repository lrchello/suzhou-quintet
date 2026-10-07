"""SQLite connections are request-local; BEGIN IMMEDIATE serializes ticket writes."""

import sqlite3
from pathlib import Path

from flask import current_app, g


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"], timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
        g.db.execute("PRAGMA busy_timeout=15000")
    return g.db


def init_db():
    db = get_db()
    db.executescript(Path(current_app.root_path, "schema.sql").read_text(encoding="utf-8"))
    db.execute("PRAGMA journal_mode=WAL")
    db.commit()


def close_db(error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def settings():
    return {r["key"]: r["value"] for r in get_db().execute("SELECT * FROM settings")}
