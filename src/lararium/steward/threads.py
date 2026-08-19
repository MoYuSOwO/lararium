"""话头存储(Steward 独占)——和起居注同库同产权。

话头是「还没聊完的事」(周级),对话自身的状态,不是生活领域——拔不掉,做成 bundle
等于让可插拔的东西变成核心依赖。它每轮随信封进第 5 层(当前信封),所以不封顶会把
信封撑爆:条数上限 MAX_OPEN、单条字数 MAX_NOTE_LEN 都在这一层守。
"""

import re
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


MAX_TOPIC_LEN = 24  # 话头名本来就该短;主键塞几千字也不像话(实测没上限时 5000 字照存)


def _normalize_topic(topic: str) -> str:
    """把话头名归一到「同一把钥匙」。topic 同样是模型传的、同样每轮进信封:
    不归一化,"装修" / " 装修" / "装修 " 会变成三条,close 关掉的只是复制品(实测)。
    折叠内部空白(含换行/制表),去首尾,截到 MAX_TOPIC_LEN;空名直接拒。
    """
    topic = re.sub(r"\s+", " ", topic).strip()
    if not topic:
        raise ValueError("话头名不能为空")
    topic = topic[:MAX_TOPIC_LEN]
    return topic.strip()  # 截断可能落在空格上,键尾不留空格


class Threads:
    # 每轮进上下文,这些上限是"把信封撑爆"的焊死点。
    MAX_OPEN = 5
    MAX_NOTE_LEN = 80

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def open_thread(self, topic: str, note: str) -> ThreadInfo:
        """建/更新一个话头:同名是更新不是新建(upsert 靠主键)。

        topic 归一化(折内部空白 + 去首尾)后当钥匙;note 就地截到 MAX_NOTE_LEN——
        写进去的就该是进上下文那份,别让库越攒越肥。
        """
        topic = _normalize_topic(topic)
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
        """关掉一个话头。找不到在开的同名 → False。空/归一后为空的 key 也当找不到。"""
        try:
            topic = _normalize_topic(topic)
        except ValueError:
            return False
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

    def all_open_threads(self) -> list[ThreadInfo]:
        """**全部** open 话头(不分页、不截前 5)。

        open_threads() 只给上下文用(每轮进信封,5 条是"撑爆信封"的闸);
        归拢(M3-5)要能看到**掉出前 5 名的那批**——它们还是 open,模型看不见也就
        关不掉(实测 22 条 open 只露 5 条),没这条门就漏了。M3-5 夜间归拢专治这个。
        """
        rows = self._conn.execute(
            "SELECT topic, note, updated_at FROM threads WHERE state='open' "
            "ORDER BY updated_at DESC, topic"
        ).fetchall()
        return [
            ThreadInfo(topic=r["topic"], note=r["note"], updated_at=r["updated_at"]) for r in rows
        ]
