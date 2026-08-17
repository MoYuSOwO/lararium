import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

SEARCHABLE_KINDS = {"envelope", "reply", "tool_result"}


@dataclass(frozen=True)
class SearchHit:
    envelope_id: str
    kind: str
    text: str
    ts: str


def _searchable_text(payload: dict[str, Any]) -> str:
    """只把人话丢进检索索引,避免 JSON 结构噪声淹没查询。"""
    for key in ("content", "text", "summary"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return json.dumps(payload, ensure_ascii=False)


class Journal:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def append(self, envelope_id: str, kind: str, payload: dict[str, Any]) -> int:
        ts = datetime.now(UTC).isoformat()
        text = _searchable_text(payload) if kind in SEARCHABLE_KINDS else None
        cur = self._conn.execute(
            "INSERT INTO journal (envelope_id, kind, payload, search_text, ts) VALUES (?,?,?,?,?)",
            (envelope_id, kind, json.dumps(payload, ensure_ascii=False), text, ts),
        )
        # AUTOINCREMENT 主键的 INSERT 必有 lastrowid;typeshed 标为 int|None,这里收窄
        assert cur.lastrowid is not None
        seq = int(cur.lastrowid)
        if text is not None:
            self._conn.execute("INSERT INTO journal_fts (text, seq) VALUES (?,?)", (text, seq))
        return seq

    def replay(self, envelope_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT seq, envelope_id, kind, payload, ts FROM journal "
            "WHERE envelope_id=? ORDER BY seq",
            (envelope_id,),
        ).fetchall()
        return [
            {
                "seq": r["seq"],
                "envelope_id": r["envelope_id"],
                "kind": r["kind"],
                "payload": json.loads(r["payload"]),
                "ts": r["ts"],
            }
            for r in rows
        ]

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        """≥3 字用 FTS5 trigram;更短的走 LIKE 回退(trigram 不匹配短查询)。"""
        query = query.strip()
        if not query:
            return []
        if len(query) >= 3:
            escaped = query.replace('"', '""')
            rows = self._conn.execute(
                "SELECT j.envelope_id, j.kind, f.text, j.ts "
                "FROM journal_fts f JOIN journal j ON j.seq = f.seq "
                "WHERE journal_fts MATCH ? ORDER BY j.seq DESC LIMIT ?",
                (f'"{escaped}"', limit),
            ).fetchall()
        else:
            escaped = query
            for ch in ("\\", "%", "_"):
                escaped = escaped.replace(ch, "\\" + ch)
            rows = self._conn.execute(
                "SELECT envelope_id, kind, search_text AS text, ts FROM journal "
                "WHERE search_text LIKE ? ESCAPE '\\' ORDER BY seq DESC LIMIT ?",
                (f"%{escaped}%", limit),
            ).fetchall()
        return [SearchHit(r["envelope_id"], r["kind"], r["text"], r["ts"]) for r in rows]

    def recent_turns(self, limit: int) -> list[dict[str, Any]]:
        """取最近 N 轮的 (user, assistant) 对,时间正序返回给 L0。"""
        ids = [
            r["envelope_id"]
            for r in self._conn.execute(
                "SELECT envelope_id, MAX(seq) AS last_seq FROM journal "
                "GROUP BY envelope_id ORDER BY last_seq DESC LIMIT ?",
                (limit,),
            ).fetchall()
        ][::-1]
        turns = []
        for env_id in ids:
            events = self.replay(env_id)
            user = next(
                (e["payload"].get("content") for e in events if e["kind"] == "envelope"), None
            )
            assistant = next(
                (e["payload"].get("content") for e in events if e["kind"] == "reply"), None
            )
            turns.append({"envelope_id": env_id, "user": user, "assistant": assistant})
        return turns
