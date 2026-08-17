import difflib
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

LEDGER_SECTIONS = ("身份", "关系", "长期偏好", "正在进行")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    content      TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source       TEXT NOT NULL,
    proposal_ids TEXT NOT NULL
);
"""


def memory_schema() -> str:
    return _SCHEMA


@dataclass(frozen=True)
class Snapshot:
    id: int
    ts: str
    content: str
    source: str
    proposal_ids: list[str]


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _blank_ledger() -> str:
    return "\n".join(f"## {s}\n" for s in LEDGER_SECTIONS)


class Ledger:
    def __init__(self, path: Path, conn: sqlite3.Connection) -> None:
        self.path = path
        self._conn = conn

    def read(self) -> str:
        """纯读。文件不存在是异常状态,必须响亮地报错——组装器每轮都调它,
        悄悄返回一份空账本 = 助手静默失忆,而用户只会觉得"它怎么全忘了"。"""
        if not self.path.exists():
            raise FileNotFoundError(
                f"账本文件不存在:{self.path}。正常启动时应已由 ensure_initialized() 建好。"
                f"若是误删,历史快照仍在库里,可用 history() 找到最近一条再 rollback() 恢复。"
            )
        return self.path.read_text(encoding="utf-8")

    def ensure_initialized(self) -> bool:
        """账本不存在就建一份空的并落 init 快照。只在启动时调用,返回是否新建了。"""
        if self.path.exists():
            return False
        self.write(_blank_ledger(), source="init", proposal_ids=[])
        return True

    def snapshot(self, content: str, source: str, proposal_ids: list[str]) -> int:
        cur = self._conn.execute(
            "INSERT INTO ledger_history (ts, content, content_hash, source, proposal_ids) "
            "VALUES (?,?,?,?,?)",
            (
                datetime.now(UTC).isoformat(),
                content,
                _hash(content),
                source,
                json.dumps(proposal_ids),
            ),
        )
        # AUTOINCREMENT 主键的 INSERT 必有 lastrowid;typeshed 标为 int|None,这里收窄
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    def write(self, content: str, source: str, proposal_ids: list[str]) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(content, encoding="utf-8")
        return self.snapshot(content, source, proposal_ids)

    def sync_manual_edit(self) -> bool:
        """文件与最新快照不符 → 先把手编版存为一次变更。返回是否发生了同步。"""
        current = self.read()
        latest = self._conn.execute(
            "SELECT content_hash FROM ledger_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if latest is not None and latest["content_hash"] == _hash(current):
            return False
        self.snapshot(current, source="manual_edit" if latest else "init", proposal_ids=[])
        return True

    def history(self, limit: int = 20) -> list[Snapshot]:
        rows = self._conn.execute(
            "SELECT * FROM ledger_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            Snapshot(r["id"], r["ts"], r["content"], r["source"], json.loads(r["proposal_ids"]))
            for r in rows
        ]

    def get(self, snapshot_id: int) -> Snapshot:
        row = self._conn.execute(
            "SELECT * FROM ledger_history WHERE id=?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"快照不存在: {snapshot_id}")
        return Snapshot(
            row["id"], row["ts"], row["content"], row["source"], json.loads(row["proposal_ids"])
        )

    def rollback(self, snapshot_id: int) -> None:
        target = self.get(snapshot_id)
        self.write(target.content, source="rollback", proposal_ids=[])

    def diff(self, id_a: int, id_b: int) -> str:
        a = self.get(id_a).content.splitlines(keepends=True)
        b = self.get(id_b).content.splitlines(keepends=True)
        return "".join(difflib.unified_diff(a, b, fromfile=f"#{id_a}", tofile=f"#{id_b}"))
