"""待办表(M6-7):一条待办一行,**完成和删除是两列,不是一列**。

**为什么是表不是文件**:PLAN M6-6 立的判据——用户会亲手改的用文件(笔记、做法),
不会亲手改的用表(流水、课件归属)。待办是结构化的到期项,要按到期日排、按日子筛、
按课筛,没有人会打开一个文件去编辑它;而"这周要交什么"在文件上就是每次扫全文自己比日期。

**两个状态位,各管一件事**:
- `done_at`:这件事**做了**(交了、考完了)。
- `deleted_at` / `deleted_reason`:它**本来就不该在**(加错了、重复了、不用做了),
  形状照 M5-20 finance 的 `deleted_at` / `deleted_reason`。
混成一列(比如"状态:完成/删除")的代价是**删一条已完成的就丢了它完成过**,撤回删除时
拿不回来;而且列表里"已完成"和"已删除"的开关会被迫变成一个。两列正交,撤回只清自己那一列。

**这一层只回事实,不说人话**(人话在 `server.py`),也不读时钟:时间戳和"今天"都由工具层
按配置时区算好传进来——F4 查询/改状态分开,F5 没有全局状态。
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from lararium.db import open_connection, transaction

DB_NAME = "todos.sqlite"

# 模型给的 #id 是不可信输入:超出 int64 的 int 在**绑定参数时**抛 OverflowError,
# 而它不是 sqlite3.Error 的子类,会直接逃出工具边界(finance `_is_bindable_id` 同一个坑)。
_MAX_ID = 2**63 - 1

# due 只存 YYYY-MM-DD(或 NULL):字符串比较就是日期比较,排序和"那天及以前"都在 SQL 里做完。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS todos (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    title          TEXT NOT NULL,
    due            TEXT,
    course         TEXT,
    note           TEXT,
    created_at     TEXT NOT NULL,
    done_at        TEXT,
    deleted_at     TEXT,
    deleted_reason TEXT
);
CREATE INDEX IF NOT EXISTS todos_due ON todos (due);
"""

_COLUMNS = (
    "SELECT id, title, due, course, note, created_at, done_at, deleted_at, deleted_reason"
    " FROM todos"
)
# 列表的筛选条件,**总数和这一页共用这一份**(两份各写一次,迟早"说有 8 条却只列得出 3 条",
# 同 threads.py `_LIST_WHERE`)。开关用绑定参数,不拼两份 SQL(S608,而且分支越多越容易漏条件)。
# `due <= ?` 在 due 为 NULL 时不成立:按日子筛,没有日子的自然不在里面。
_LIST_WHERE = (
    " WHERE (? = 1 OR done_at IS NULL) AND (? = 1 OR deleted_at IS NULL)"
    " AND (? IS NULL OR due <= ?) AND (? IS NULL OR course = ?)"
)
# 过期 = 没做、没删、到期日早于今天。数的是**整个筛选范围**,不是这一页——
# 第一页看不全的时候,抬头那个数得是真的。
_LIST_COUNT = (
    "SELECT count(*) AS total,"
    " coalesce(sum(done_at IS NULL AND deleted_at IS NULL AND due < ?), 0) AS overdue"
    " FROM todos"
)
# 没有到期日的排最后(`due IS NULL` 为 0 的在前);同一天的按记下的先后。
_LIST_ORDER = " ORDER BY due IS NULL, due, id LIMIT ? OFFSET ?"
_LIST_COUNT_SQL = _LIST_COUNT + _LIST_WHERE
_LIST_PAGE_SQL = _COLUMNS + _LIST_WHERE + _LIST_ORDER
_GET_SQL = _COLUMNS + " WHERE id = ?"
_SAME_TITLE_SQL = (
    _COLUMNS + " WHERE title = ? AND done_at IS NULL AND deleted_at IS NULL ORDER BY id LIMIT 1"
)


@dataclass(frozen=True)
class Todo:
    id: int
    title: str
    due: str | None
    course: str | None
    note: str | None
    created_at: str
    done_at: str | None
    deleted_at: str | None
    deleted_reason: str | None


@dataclass(frozen=True)
class Listing:
    total: int  # 筛选范围内一共几条(不止这一页)
    overdue: int  # 其中过期没做完的
    rows: list[Todo]


def _todo(r: sqlite3.Row) -> Todo:
    return Todo(
        id=r["id"],
        title=r["title"],
        due=r["due"],
        course=r["course"],
        note=r["note"],
        created_at=r["created_at"],
        done_at=r["done_at"],
        deleted_at=r["deleted_at"],
        deleted_reason=r["deleted_reason"],
    )


class TodoStore:
    """连接从 `db.open_connection` 拿(M5-8:一条 assistant 消息里的多个工具调用是并发的)。"""

    def __init__(self, root: Path) -> None:
        self._conn = open_connection(Path(root) / DB_NAME)
        self._conn.executescript(_SCHEMA)

    @contextmanager
    def atomically(self) -> Iterator[None]:
        """「读了再改」的临界区(M5-31):判状态和写状态在同一个事务里,整块持锁。

        勾掉 / 删除都是"先看它现在是什么,再决定写不写"——两次并发的删除都读到"还在",
        第二次的理由就把第一次的盖掉,两边还都跟用户说删了。
        """
        with transaction(self._conn, immediate=True):
            yield

    def get(self, todo_id: int) -> Todo | None:
        """找不到、或者 id 大到根本存不下(那样的 id 在库里必然不存在)→ None。"""
        if isinstance(todo_id, int) and not -_MAX_ID - 1 <= todo_id <= _MAX_ID:
            return None
        r = self._conn.execute(_GET_SQL, (todo_id,)).fetchone()
        return None if r is None else _todo(r)

    def open_with_title(self, title: str) -> Todo | None:
        """同名、没做、没删的最早一条——给"是不是记重了"那句提示用。"""
        r = self._conn.execute(_SAME_TITLE_SQL, (title,)).fetchone()
        return None if r is None else _todo(r)

    def insert(
        self,
        *,
        title: str,
        due: str | None,
        course: str | None,
        note: str | None,
        created_at: str,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO todos (title, due, course, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (title, due, course, note, created_at),
        )
        return int(cur.lastrowid or 0)

    def overwrite_fields(
        self,
        todo_id: int,
        *,
        title: str,
        due: str | None,
        course: str | None,
        note: str | None,
    ) -> None:
        """就地改四个内容字段。`id` / `created_at` / 两个状态位不在 SET 里——一个字节都不许动。"""
        self._conn.execute(
            "UPDATE todos SET title = ?, due = ?, course = ?, note = ? WHERE id = ?",
            (title, due, course, note, todo_id),
        )

    def set_done(self, todo_id: int, done_at: str | None) -> None:
        """`done_at=None` 是撤回:**只清这一列**,别的列原样——撤回要逐字节回到勾掉之前。"""
        self._conn.execute("UPDATE todos SET done_at = ? WHERE id = ?", (done_at, todo_id))

    def set_deleted(self, todo_id: int, deleted_at: str | None, reason: str | None) -> None:
        """`deleted_at=None, reason=None` 是撤回:两列一起清,`done_at` 不碰
        ——删掉一条已完成的再撤回,它还是已完成。"""
        self._conn.execute(
            "UPDATE todos SET deleted_at = ?, deleted_reason = ? WHERE id = ?",
            (deleted_at, reason, todo_id),
        )

    def listing(
        self,
        *,
        today: str,
        due_before: str | None,
        course: str | None,
        include_done: bool,
        include_deleted: bool,
        limit: int,
        offset: int,
    ) -> Listing:
        where = (
            1 if include_done else 0,
            1 if include_deleted else 0,
            due_before,
            due_before,
            course,
            course,
        )
        # 两条读放进同一个事务:中间插进一次并发的勾掉,抬头的总数就和这一页对不上了。
        with transaction(self._conn):
            counted = self._conn.execute(_LIST_COUNT_SQL, (today, *where)).fetchone()
            rows = self._conn.execute(_LIST_PAGE_SQL, (*where, limit, offset)).fetchall()
        return Listing(
            total=int(counted["total"]),
            overdue=int(counted["overdue"]),
            rows=[_todo(r) for r in rows],
        )
