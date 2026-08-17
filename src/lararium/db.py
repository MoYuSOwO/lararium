import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox (
    id           TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    channel      TEXT NOT NULL,
    content      TEXT NOT NULL,
    meta         TEXT NOT NULL,
    ts           TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT 'pending',
    error        TEXT,
    claimed_at   TEXT,
    completed_at TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_inbox_state ON inbox(state, ts);
"""


def connect(path: Path) -> sqlite3.Connection:
    """isolation_level=None:自己管事务,claim 要用 BEGIN IMMEDIATE。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn
