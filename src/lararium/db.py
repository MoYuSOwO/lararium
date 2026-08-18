import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
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

CREATE TABLE IF NOT EXISTS journal (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    envelope_id TEXT NOT NULL,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    search_text TEXT,              -- 仅可检索的 kind 才填,供 LIKE 回退用
    ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_journal_envelope ON journal(envelope_id, seq);

CREATE TABLE IF NOT EXISTS outbox (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    envelope_id  TEXT NOT NULL,
    channel      TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'reply',   -- reply | notice
    content      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_channel ON outbox(channel, seq);

CREATE VIRTUAL TABLE IF NOT EXISTS journal_fts USING fts5(
    text,
    seq UNINDEXED,
    tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS threads (
    topic      TEXT PRIMARY KEY,
    note       TEXT NOT NULL,
    state      TEXT NOT NULL DEFAULT 'open',   -- open | closed
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_threads_open ON threads(state, updated_at);
"""


def connect(path: Path) -> sqlite3.Connection:
    """isolation_level=None:自己管事务,claim 要用 BEGIN IMMEDIATE。

    check_same_thread=False:FastMCP 和 Pydantic AI 都把**同步**工具函数丢进线程池执行,
    而连接是在主线程建的。不关掉这个检查,任何碰数据库的工具调用都会抛
    ProgrammingError。安全性由架构保证——收件箱严格串行,任一时刻只有一轮在跑,
    不存在真正的并发访问。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务:with 块内**同一连接**的语句原子化,异常自动回滚。

    isolation_level=None 下每个 execute 各自自动提交,跨对象的"一起成、一起不成"
    必须显式 BEGIN/COMMIT。调用方别再伸手拿各对象的 _conn 去拼(违反 S3),把
    一个共享连接交给它即可。
    """
    conn.execute("BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
