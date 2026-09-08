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
    # 只有 list_threads 会带回 closed 的行(M5-33);别的读法本来就只查 open,
    # 所以默认 "open" 而不是逼每个构造点都写一遍。
    state: str = "open"


def _now() -> str:
    return datetime.now(UTC).isoformat()


MAX_TOPIC_LEN = 24  # 话头名本来就该短;主键塞几千字也不像话(实测没上限时 5000 字照存)

# `? = 1 OR state='open'`:用绑定参数开关"要不要连关掉的一起列",而不是拼两份 WHERE
# ——照 finance 那边 `_RECENT_WHERE` 的规矩(拼出来的 SQL 会被 S608 盯上,而且分支
# 越多越容易有一条忘了加条件)。总数和这一页共用同一个 WHERE:两份各写一次,
# 迟早出现「说有 8 条却只列得出 3 条」。
_LIST_WHERE = " WHERE (? = 1 OR state='open')"
_LIST_COUNT_COLUMNS = "SELECT count(*) FROM threads"
_LIST_PAGE_COLUMNS = "SELECT topic, note, state, updated_at FROM threads"
_LIST_TOTAL_SQL = _LIST_COUNT_COLUMNS + _LIST_WHERE
_LIST_PAGE_SQL = (
    _LIST_PAGE_COLUMNS + _LIST_WHERE + " ORDER BY updated_at DESC, topic LIMIT ? OFFSET ?"
)


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

    def list_threads(
        self, *, limit: int, offset: int, include_closed: bool = False
    ) -> tuple[int, list[ThreadInfo]]:
        """分页列话头,返回(总数, 这一页)。默认只列开着的。

        **不受 MAX_OPEN 限制**(M5-33):那个上限管的是"每轮信封里塞几条",和"我要查
        一下"是两件事。单页条数由调用方封顶(工具那边的 MAX_THREAD_ROWS)。

        **note 不在这里再截一次**:MAX_NOTE_LEN 是**入库时**就截的(open_thread 是唯一
        写入口,夜间归拢也走它),库里本来就不会更长。两处截断迟早漂成两个数——
        M4-4 的两套渲染器就是这么来的。

        排序和 open_threads() 一致(updated_at 倒序、topic 兜底),这样"信封里那几条"
        正好是这里的第一页,模型不用在两种顺序之间对账。
        """
        want_all = 1 if include_closed else 0
        total = int(self._conn.execute(_LIST_TOTAL_SQL, (want_all,)).fetchone()[0])
        rows = self._conn.execute(_LIST_PAGE_SQL, (want_all, limit, offset)).fetchall()
        return total, [
            ThreadInfo(
                topic=r["topic"],
                note=r["note"],
                updated_at=r["updated_at"],
                state=r["state"],
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
