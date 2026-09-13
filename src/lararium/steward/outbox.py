import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from lararium.db import transaction


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

    @property
    def conn(self) -> sqlite3.Connection:
        """只读暴露连接:与 inbox 同库时才可能同事务(投递+完成原子化),见 loop。"""
        return self._conn

    def put(
        self,
        envelope_id: str,
        channel: str,
        content: str,
        kind: str = "reply",
        *,
        expires_at: datetime | None = None,
    ) -> int:
        """把一条投递写入出件箱,返回其全局递增 seq。

        `expires_at`(M6-9):过了这个时刻还没交出去就扔(见 `drop_expired`)。缺省 None = 一直等
        ——回复和待审通知照旧是"消息在等你开口"。
        """
        cur = self._conn.execute(
            "INSERT INTO outbox (envelope_id, channel, kind, content, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                envelope_id,
                channel,
                kind,
                content,
                datetime.now(UTC).isoformat(),
                None if expires_at is None else _instant(expires_at),
            ),
        )
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    def take(
        self, channel: str, after: int, limit: int = 50, *, now: datetime | None = None
    ) -> list[OutboxItem]:
        """取本渠道 seq > after 的条目,并(观测性地)标记 delivered_at。

        at-least-once:标记不阻止再次 take——同一条可能出现多次,客户端按 seq 去重。
        **过期的、已经扔掉的不给**(M6-9):微信窗口关着的时候适配器发不出去,游标不推,
        用户三天后开口它会重新来取——那时候不许把三天前那句问候交出去。
        """
        instant = _instant(now or datetime.now(UTC))
        rows = self._conn.execute(
            "SELECT seq, envelope_id, channel, kind, content, created_at, delivered_at "
            "FROM outbox WHERE channel=? AND seq>? AND dropped_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > ?) ORDER BY seq LIMIT ?",
            (channel, after, instant, limit),
        ).fetchall()
        if not rows:
            return []
        now_text = datetime.now(UTC).isoformat()
        for r in rows:  # 逐条参数化标记,不用动态 IN,避免构造性 SQL(S608)
            self._conn.execute("UPDATE outbox SET delivered_at=? WHERE seq=?", (now_text, r["seq"]))
        return [
            OutboxItem(
                seq=int(r["seq"]),
                envelope_id=r["envelope_id"],
                channel=r["channel"],
                kind=r["kind"],
                content=r["content"],
                created_at=r["created_at"],
                delivered_at=now_text,
            )
            for r in rows
        ]

    def drop_expired(self, now: datetime) -> list[OutboxItem]:
        """把过了保质期、还没扔的标上 dropped_at,返回被扔的那几条(调用方要把它们记进起居注)。

        只扔一次:同一条第二次不会再返回——"扔了多少条"要数得清,不能每次来取都再记一遍。
        """
        instant = _instant(now)
        with transaction(self._conn):
            rows = self._conn.execute(
                "SELECT seq, envelope_id, channel, kind, content, created_at, delivered_at "
                "FROM outbox WHERE dropped_at IS NULL AND expires_at IS NOT NULL "
                "AND expires_at <= ? ORDER BY seq",
                (instant,),
            ).fetchall()
            for r in rows:
                self._conn.execute(
                    "UPDATE outbox SET dropped_at=? WHERE seq=?", (instant, r["seq"])
                )
        return [
            OutboxItem(
                seq=int(r["seq"]),
                envelope_id=r["envelope_id"],
                channel=r["channel"],
                kind=r["kind"],
                content=r["content"],
                created_at=r["created_at"],
                delivered_at=r["delivered_at"],
            )
            for r in rows
        ]


def _instant(moment: datetime) -> str:
    """保质期那两列的写法:统一 UTC、**固定六位微秒**。比较是按字符串比的,
    `isoformat()` 在微秒为 0 时会省掉那一段,两种长度混着比就会在同一秒里判反。"""
    return moment.astimezone(UTC).isoformat(timespec="microseconds")
