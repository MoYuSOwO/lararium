import json
import sqlite3
from datetime import UTC, datetime

from lararium.envelope import Envelope


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Inbox:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @property
    def conn(self) -> sqlite3.Connection:
        """只读暴露连接:组装根需要拿它开显式事务(如 投递+完成 原子化)时走这里,
        别伸手够 _conn(S3)。"""
        return self._conn

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

    def put_idempotent(self, env: Envelope) -> bool:
        """幂等入队:同 id 只处理一次,已存在则返回 False(重复)。

        id 是主键,INSERT OR IGNORE 靠 rowcount 分辨首插/重复——HTTP 幂等的根基:
        客户端重发同 id,消息只在库里留一行(DESIGN §9 ingress)。
        """
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO inbox (id, source, channel, content, meta, ts) "
            "VALUES (?,?,?,?,?,?)",
            (
                env.id,
                env.source,
                env.channel,
                env.content,
                json.dumps(env.meta, ensure_ascii=False),
                env.ts.isoformat(),
            ),
        )
        return cur.rowcount == 1

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
                "UPDATE inbox SET state='processing', claimed_at=?, attempts=attempts+1 WHERE id=?",
                (_now(), row["id"]),
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

    def release(self, env_id: str) -> None:
        """可重试失败后把信封放回 pending 供再次认领。

        不清 attempts:它在 claim 时 +1,是重试上限的计数依据。清了就等于
        每次都从头算,毒消息会无限重试。
        """
        self._conn.execute(
            "UPDATE inbox SET state='pending', claimed_at=NULL WHERE id=?", (env_id,)
        )

    def attempts(self, env_id: str) -> int:
        row = self._conn.execute("SELECT attempts FROM inbox WHERE id=?", (env_id,)).fetchone()
        return int(row["attempts"]) if row else 0

    def pending_count(self) -> int:
        return int(
            self._conn.execute("SELECT COUNT(*) FROM inbox WHERE state='pending'").fetchone()[0]
        )

    def recover_stale(self, max_attempts: int = 2) -> tuple[int, int]:
        """把上次运行遗留的 processing 记录清理掉,返回 (重新排队数, 放弃数)。

        只应在启动时调用一次。同时跑两个 Steward 会互相抢活——本系统按设计
        只有一个,这也是"严格串行"成立的前提。
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            abandoned = self._conn.execute(
                "UPDATE inbox SET state='failed', error=?, completed_at=? "
                "WHERE state='processing' AND attempts >= ?",
                ("重启后仍未处理完,已达重试上限,可能是毒消息", _now(), max_attempts),
            ).rowcount
            requeued = self._conn.execute(
                "UPDATE inbox SET state='pending', claimed_at=NULL WHERE state='processing'"
            ).rowcount
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return requeued, abandoned
