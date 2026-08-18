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
    source: str | None = None
    channel: str = ""
    untrusted: bool = False


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
                "SELECT j.envelope_id, j.kind, f.text, j.ts, "
                "json_extract(j.payload, '$.source') AS source, "
                "json_extract(j.payload, '$.channel') AS channel, "
                "json_extract(j.payload, '$.meta.untrusted') AS untrusted "
                "FROM journal_fts f JOIN journal j ON j.seq = f.seq "
                "WHERE journal_fts MATCH ? ORDER BY j.seq DESC LIMIT ?",
                (f'"{escaped}"', limit),
            ).fetchall()
        else:
            escaped = query
            for ch in ("\\", "%", "_"):
                escaped = escaped.replace(ch, "\\" + ch)
            rows = self._conn.execute(
                "SELECT envelope_id, kind, search_text AS text, ts, "
                "json_extract(payload, '$.source') AS source, "
                "json_extract(payload, '$.channel') AS channel, "
                "json_extract(payload, '$.meta.untrusted') AS untrusted "
                "FROM journal "
                "WHERE search_text LIKE ? ESCAPE '\\' ORDER BY seq DESC LIMIT ?",
                (f"%{escaped}%", limit),
            ).fetchall()
        return [
            SearchHit(
                r["envelope_id"],
                r["kind"],
                r["text"],
                r["ts"],
                source=r["source"],
                channel=r["channel"] or "",
                untrusted=bool(r["untrusted"]),
            )
            for r in rows
        ]

    def _turn(self, env_id: str) -> dict[str, Any]:
        """取某一轮 (envelope → reply) 的对,带 provenance 字段(P1-1)。

        两个 recent_* 共用同一份提取逻辑,不许各写一份——漂移会让两条路径渲染出的
        历史轮不一致(P1-1 的教训)。
        """
        events = self.replay(env_id)
        env = next((e for e in events if e["kind"] == "envelope"), None)
        assistant = next(
            (e["payload"].get("content") for e in events if e["kind"] == "reply"), None
        )
        return {
            "envelope_id": env_id,
            "user": env["payload"].get("content") if env else None,
            "assistant": assistant,
            "source": env["payload"].get("source", "user") if env else "user",
            "channel": env["payload"].get("channel", "cli") if env else "cli",
            "untrusted": bool(env["payload"].get("meta", {}).get("untrusted")) if env else False,
            "ts": env["payload"].get("ts") if env else None,
        }

    def recent_turns(self, limit: int) -> list[dict[str, Any]]:
        """取最近 N 轮的 (user, assistant) 对,时间正序返回给 L0。

        每条带上 source / channel / untrusted / ts——L0 渲染要给历史轮套上
        "外部数据"的包裹(P1-1),没有这些 provenance 字段就无从判断。
        """
        ids = [
            r["envelope_id"]
            for r in self._conn.execute(
                "SELECT envelope_id, MAX(seq) AS last_seq FROM journal "
                "GROUP BY envelope_id ORDER BY last_seq DESC LIMIT ?",
                (limit,),
            ).fetchall()
        ][::-1]
        return [self._turn(env_id) for env_id in ids]

    def recent_turns_within_budget(
        self, max_tokens: int, max_turns: int = 2000
    ) -> list[dict[str, Any]]:
        """M3-1:L0 按 token 预算截断。从最新往回填,累计估算 token 超预算即停;
        返回时间正序(旧→新)。

        token 是 `len(文本)//2` 的中文粗估——**只是预算控制,不是精确计费**,注明
        是估算。单轮即使超预算也**至少返回最新一轮**:宁可多塞一轮,也别把"刚说的"
        丢了(截断发生在最旧端,最新一轮是对话接续的锚点)。
        max_turns 是轮数兜底:预算再大也不超过它(L0 整段进上下文,不封顶会撑爆)。
        """
        ids = [
            r["envelope_id"]
            for r in self._conn.execute(
                "SELECT envelope_id, MAX(seq) AS last_seq FROM journal "
                "GROUP BY envelope_id ORDER BY last_seq DESC LIMIT ?",
                (max_turns,),
            ).fetchall()
        ]
        turns: list[dict[str, Any]] = []
        used = 0
        for env_id in ids:
            t = self._turn(env_id)
            est = (len(t["user"] or "") + len(t["assistant"] or "")) // 2
            if turns and used + est > max_tokens:  # 最新一轮(首个)无条件进
                break
            turns.append(t)
            used += est
        return turns[::-1]
