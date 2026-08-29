import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger("lararium")

try:
    import sqlite_vec
except ImportError:  # 冷门架构可能没有 sqlite-vec 的 wheel——别让系统因此起不来
    sqlite_vec = None  # mypy:ignore_missing_imports 使缺模块为 Any,None 赋值无需 ignore

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox (
    id           TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    channel      TEXT NOT NULL,
    content      TEXT NOT NULL,
    attachments  TEXT NOT NULL DEFAULT '[]',
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

-- P1-1:归拢幂等键从时间区间改成**内容(seq 光标)**。唯一调用方 /sweep 每次传
-- now-24h~now,都是新区间,按区间字符串永远幂等不了(一秒三次 → 模型调三次 →
-- 三条重复提案)。光标 = 本次归拢覆盖到的最大 journal seq,下次从那之后扫。
CREATE TABLE IF NOT EXISTS sweep_state (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    cursor_seq INTEGER NOT NULL DEFAULT 0,   -- 归拢已覆盖到的最大 journal seq
    ran_at     TEXT
);

-- P1-3:notice 每天最多一条(压缩被屏障停/归拢提出提案都投,但别刷屏)。
CREATE TABLE IF NOT EXISTS notice_log (
    date TEXT PRIMARY KEY
);

-- M3-6 压缩:M3-3 之后的命根子是 append-only(起居注只增不改),压缩**不删正文**,
-- 只是把"已压成索引"的信封标记掉(退出 L0 一线),索引行单独一张表供 assemble 当 l1。
-- M4-8:前缀区指纹的变更史。改了人设、缓存命中从 90% 掉到 0,得有地方说得清为什么
-- ——「缓存命中是设计约束不是优化项」,那它什么时候变过就必须查得出来。
CREATE TABLE IF NOT EXISTS prefix_log (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    digest     TEXT NOT NULL,
    changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS l1_index (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,      -- 索引行日期(供 90 天保留期剪枝)
    line        TEXT NOT NULL,      -- "话题 · 一句结论"(L1 渲染时拼 日期 · line · 信封id)
    envelope_id TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_l1_date ON l1_index(date);

-- 已被压缩(退出 L0 一线)的信封 id。逐字正文仍在 journal——可重放/可检索,
-- 只是不再往近期上下文里灌。不反复压缩靠它(见 compact)。
CREATE TABLE IF NOT EXISTS compressed_envelopes (
    envelope_id TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL
);
"""

# M3-4 语义检索:嵌入向量(256 维,L2 归一化后入库)与起居注同库。
# vec0 默认 L2 距离;存归一化向量后,L2 序 = 余弦序,cos = 1 - d²/2。
# 这块只有 sqlite-vec 扩展就绪才算;没就绪时整个 schema 不含它(见 connect)。
_VEC_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS journal_vec USING vec0(
    seq INTEGER PRIMARY KEY,
    embedding FLOAT[256]
);
"""

# 语义检索是否可用(sqlite-vec 扩展就绪才算)。connect() 每次重设;三处消费:
#   connect 建 vec 表 / Journal.append 写向量 / Journal.search_similar。
# 默认 True:绝大多数环境扩展在;失败时 connect 把它翻成 False,词法路照常,recall 提示。
VEC_AVAILABLE = True


def _load_vec_extension(conn: sqlite3.Connection) -> bool:
    """加载 sqlite-vec 扩展,成功置 VEC_AVAILABLE=True;任何失败记日志置 False。

    失败不拦启动——扩展没有 ≠ 起不来,只是 recall_similar 不可用(工具会回可读提示)。
    失败面:没编进扩展支持的 Python 上 enable_load_extension 直接 AttributeError;冷门
    架构没有 sqlite-vec 的 wheel(上面的 import 已兜住)。
    """
    global VEC_AVAILABLE
    if sqlite_vec is None:
        VEC_AVAILABLE = False
        logger.warning("sqlite-vec 未安装:语义检索(recall_similar)不可用,词法检索照常")
        return False
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        VEC_AVAILABLE = True
        return True
    except Exception as exc:  # enable_load_extension AttributeError / 扩展加载失败
        VEC_AVAILABLE = False
        logger.warning("sqlite-vec 扩展加载失败(%s):语义检索不可用,词法检索照常", exc)
        return False


# 给**已存在的**库补新列。`CREATE TABLE IF NOT EXISTS` 对老库是空操作:新字段只写进
# SCHEMA 的话,老库永远看不到它,而症状是"新装的机器好使,你自己那台不好使"——
# 最难查的那一类。每项是 (探测语句, 列名, 补列语句),三个都是完整字面量:
# 拼 SQL 字符串是 S608 要防的东西,而这里根本不需要拼。
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    (
        "PRAGMA table_info(inbox)",
        "attachments",
        "ALTER TABLE inbox ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'",
    ),
)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for probe, column, ddl in _ADDED_COLUMNS:
        if column not in {row[1] for row in conn.execute(probe)}:
            conn.execute(ddl)


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
    # M3-4:vec0 是 sqlite-vec 的扩展,vec 虚拟表必须扩展就绪才建。扩展加载失败
    # 只影响语义检索,不拦启动——SCHEMA 里不建 vec 表,词法路照常(M3-4 补做)。
    vec_ok = _load_vec_extension(conn)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA + (_VEC_SCHEMA if vec_ok else ""))
    _add_missing_columns(conn)
    return conn


# 嵌套事务用的 savepoint 名。真常量(F5:模块级只允许不可变常量)。
_NESTED_SAVEPOINT = "lararium_nested"


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务:with 块内**同一连接**的语句原子化,异常自动回滚。

    isolation_level=None 下每个 execute 各自自动提交,跨对象的"一起成、一起不成"
    必须显式 BEGIN/COMMIT。调用方别再伸手拿各对象的 _conn 去拼(违反 S3),把
    一个共享连接交给它即可。
    """
    if conn.in_transaction:
        # **可重入**:已经在事务里就开一个 SAVEPOINT(SQLite 不许嵌套 BEGIN)。
        #
        # 第一版是"直接并入外层、什么都不做",看着也对——内层异常一路冒到外层,整体回滚。
        # 但那个结论有前提:**没人中途 catch**。中间层要是把内层异常吞掉、外层继续走,
        # 内层那半条就跟着外层一起提交了(实测:表里剩 outer-before / inner-半条 /
        # outer-after)。而加这一支的初衷恰恰是"不许留下半条",换个姿势又能留下。
        # SAVEPOINT 让「内层失败只回滚内层、外层照常」**无条件成立**,不必附加前提。
        #
        # 名字用常量不用计数器:本函数只作为 with 块使用,嵌套必然是严格配对的,而
        # SQLite 对重名 savepoint 的规定就是"作用于最近的那一个"——正合严格嵌套的语义。
        # 用计数器就得在模块级放可变状态(F5 禁止),为一个不存在的问题付代价。
        conn.execute(f"SAVEPOINT {_NESTED_SAVEPOINT}")
        try:
            yield conn
            conn.execute(f"RELEASE {_NESTED_SAVEPOINT}")
        except BaseException:  # BaseException 的理由同下方外层:Ctrl-C 落在中间也得回滚
            conn.execute(f"ROLLBACK TO {_NESTED_SAVEPOINT}")
            conn.execute(f"RELEASE {_NESTED_SAVEPOINT}")
            raise
        return
    conn.execute("BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:  # 含 KeyboardInterrupt/SystemExit——Ctrl-C 落在事务中间也得回滚,
        # 否则 conn.in_transaction 仍是 True,之后写库全报 cannot start a transaction。
        conn.execute("ROLLBACK")
        raise
