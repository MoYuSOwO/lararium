"""话头存储(Steward 独占)——和起居注同库同产权。

话头是「还没聊完的事」(周级),对话自身的状态,不是生活领域——拔不掉,做成 bundle
等于让可插拔的东西变成核心依赖。它每轮随信封进第 5 层(当前信封),所以不封顶会把
信封撑爆:条数上限 MAX_OPEN、单条字数 MAX_NOTE_LEN 都在这一层守。
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class ThreadInfo:
    topic: str
    note: str
    updated_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Threads:
    # 每轮进上下文,这两个上限是"把信封撑爆"的焊死点。
    MAX_OPEN = 5
    MAX_NOTE_LEN = 80

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def open_thread(self, topic: str, note: str) -> ThreadInfo:
        """建/更新一个话头:同名是更新不是新建(upsert 靠主键)。

        note 就地截到 MAX_NOTE_LEN——写进去的就该是进上下文那份,别让库越攒越肥。
        """
        note = note.strip()[: self.MAX_NOTE_LEN]
        now = _now()
        self._conn.execute(
            "INSERT INTO threads (topic, note, state, updated_at) VALUES (?, ?, 'open', ?) "
            "ON CONFLICT(topic) DO UPDATE SET note=excluded.note, state='open', "
            "updated_at=excluded.updated_at",
            (topic, note, now),
        )
        return ThreadInfo(topic=topic, note=note, updated_at=now)

    def close_thread(self, topic: str) -> bool:
        """关掉一个话头。找不到在开的同名 → False。"""
        cur = self._conn.execute(
            "UPDATE threads SET state='closed', updated_at=? WHERE topic=? AND state='open'",
            (_now(), topic),
        )
        return cur.rowcount > 0

    def open_threads(self) -> list[ThreadInfo]:
        """只返回开着的,按最近更新排序,条数上限 MAX_OPEN、note 截到 MAX_NOTE_LEN。

        上限在读取时再压一道(老数据/旁路写入可能超),写入时已截是第一道。
        """
        rows = self._conn.execute(
            "SELECT topic, note, updated_at FROM threads WHERE state='open' "
            "ORDER BY updated_at DESC, topic LIMIT ?",
            (self.MAX_OPEN,),
        ).fetchall()
        return [
            ThreadInfo(
                topic=r["topic"],
                note=r["note"][: self.MAX_NOTE_LEN],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]
