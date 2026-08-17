import json
import sqlite3
from datetime import UTC, datetime

from lararium.envelope import Envelope


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Inbox:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def put(self, env: Envelope) -> None:
        self._conn.execute(
            "INSERT INTO inbox (id, source, channel, content, meta, ts) VALUES (?,?,?,?,?,?)",
            (
                env.id,
                env.source,
                env.channel,
                env.content,
                json.dumps(env.meta, ensure_ascii=False),
                env.ts.isoformat(),
            ),
        )

    def claim_next(self) -> Envelope | None:
        """严格串行:任一时刻最多一条 processing。"""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            in_flight = self._conn.execute(
                "SELECT COUNT(*) FROM inbox WHERE state='processing'"
            ).fetchone()[0]
            if in_flight:
                self._conn.execute("COMMIT")
                return None
            row = self._conn.execute(
                "SELECT * FROM inbox WHERE state='pending' ORDER BY ts, rowid LIMIT 1"
            ).fetchone()
            if row is None:
                self._conn.execute("COMMIT")
                return None
            self._conn.execute(
                "UPDATE inbox SET state='processing', claimed_at=? WHERE id=?", (_now(), row["id"])
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return Envelope(
            id=row["id"],
            source=row["source"],
            channel=row["channel"],
            content=row["content"],
            meta=json.loads(row["meta"]),
            ts=datetime.fromisoformat(row["ts"]),
        )

    def complete(self, env_id: str) -> None:
        self._conn.execute(
            "UPDATE inbox SET state='done', completed_at=? WHERE id=?", (_now(), env_id)
        )

    def fail(self, env_id: str, error: str) -> None:
        self._conn.execute(
            "UPDATE inbox SET state='failed', error=?, completed_at=? WHERE id=?",
            (error, _now(), env_id),
        )

    def pending_count(self) -> int:
        return int(
            self._conn.execute("SELECT COUNT(*) FROM inbox WHERE state='pending'").fetchone()[0]
        )
