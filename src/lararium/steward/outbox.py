import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class OutboxItem:
    seq: int
    envelope_id: str
    channel: str
    kind: str
    content: str
    created_at: str
    delivered_at: str | None


class Outbox:
    """出件箱(D10):回复/通知的投递队列,独立于起居注。

    起居注是逐字 append-only,投递状态要 UPDATE——两者职责不同,不能混用。
    at-least-once:delivered_at 只是观测字段,不是投递保证;同一条可被反复 take,
    客户端按 seq 去重。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def put(self, envelope_id: str, channel: str, content: str, kind: str = "reply") -> int:
        """把一条投递写入出件箱,返回其全局递增 seq。"""
        cur = self._conn.execute(
            "INSERT INTO outbox (envelope_id, channel, kind, content, created_at) "
            "VALUES (?,?,?,?,?)",
            (envelope_id, channel, kind, content, datetime.now(UTC).isoformat()),
        )
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    def take(self, channel: str, after: int, limit: int = 50) -> list[OutboxItem]:
        """取本渠道 seq > after 的条目,并(观测性地)标记 delivered_at。

        at-least-once:标记不阻止再次 take——同一条可能出现多次,客户端按 seq 去重。
        """
        rows = self._conn.execute(
            "SELECT seq, envelope_id, channel, kind, content, created_at, delivered_at "
            "FROM outbox WHERE channel=? AND seq>? ORDER BY seq LIMIT ?",
            (channel, after, limit),
        ).fetchall()
        if not rows:
            return []
        now = datetime.now(UTC).isoformat()
        for r in rows:  # 逐条参数化标记,不用动态 IN,避免构造性 SQL(S608)
            self._conn.execute("UPDATE outbox SET delivered_at=? WHERE seq=?", (now, r["seq"]))
        return [
            OutboxItem(
                seq=int(r["seq"]),
                envelope_id=r["envelope_id"],
                channel=r["channel"],
                kind=r["kind"],
                content=r["content"],
                created_at=r["created_at"],
                delivered_at=now,
            )
            for r in rows
        ]
