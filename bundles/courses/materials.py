"""课件归属表(M6-6b):哪个媒体 id 归在哪门课下、在那门课里叫什么。**只记归属,不拷字节。**

**为什么是表不是文件**:PLAN M6-6 立的判据——用户会亲手改的用文件(笔记),不会亲手改的
用表(课件的归属、流水)。字节在媒体池里(`data/media/<sha256>.<ext>`,Steward / gateway
那一侧的存储),bundle 按 §5 数据产权摸不到它,也不需要摸:读课件是 Steward 侧的
`read_pdf` / `read_image`,跨过边界的唯一把手就是 id。

**课程这一列存的是课程目录相对课程根的路径**:活着的课是「线性代数」,回收站里的是
「.trash/线性代数-20260913T053648」(`CourseStore.label` 算)。于是改名 / 删除 / 撤回
"搬整个目录"在表这一侧的对应物,就是把这一列**从一个路径改成另一个路径**(`relabel`)
——6a 写的"课件天然跟着目录走"那句话,在表这一侧就是这一个 UPDATE。
活着的课名不许以 `.` 开头,所以两种键永远撞不上。

库在 `data/courses/.materials.sqlite`:和笔记同一个目录,容器化时 courses 一个挂载点就是
这个 bundle 的全部数据;前导 `.` 让它不在用户 `ls` 的笔记里晃,而 `CourseStore.names()`
只列目录,它本来也不会被当成一门课。
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from lararium.db import open_connection, transaction

DB_NAME = ".materials.sqlite"

# 行序 = 归档顺序(rowid):「第10讲」按码位排会跑到「第2讲」前面,而用户是按讲次发的。
# 两条 UNIQUE 各守一件事:同一门课里**名字**重了,之后谁也说不清指的是哪份;同一门课里
# **同一份**归两次,列表里就是一个东西两个名字。不同的课之间都可以——同一份讲义归到两门课,
# 各叫各的名字,是正常用法。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS materials (
    course   TEXT NOT NULL,
    name     TEXT NOT NULL,
    media_id TEXT NOT NULL,
    UNIQUE (course, name),
    UNIQUE (course, media_id)
);
"""


@dataclass(frozen=True)
class Material:
    """一份归到课下的课件:用户的叫法 + 媒体 id。**名字给人看,id 给机器用。**"""

    name: str
    media_id: str


class Materials:
    """归属表。连接从 `db.open_connection` 拿(D3:工具调用是并发的,裸连接会烂游标)。"""

    def __init__(self, root: Path) -> None:
        self._conn = open_connection(Path(root) / DB_NAME)
        self._conn.executescript(_SCHEMA)

    @contextmanager
    def atomically(self) -> Iterator[None]:
        """一个事务,**整块持锁**(`db.transaction`)。

        两个用处:① 改名 / 撤回时表和目录"一起成、一起不成"——表先改、目录后搬,搬失败
        (OSError)事务就回滚;② `courses__add_file` 的"查有没有重的 → 插进去"在同一把锁里,
        一条 assistant 消息里并发的两次 courses__add_file 不会一起查到"没有"。
        """
        with transaction(self._conn):
            yield

    def listed(self, course: str) -> list[Material]:
        """这门课下的课件,按归档顺序。"""
        rows = self._conn.execute(
            "SELECT name, media_id FROM materials WHERE course = ? ORDER BY rowid", (course,)
        ).fetchall()
        return [Material(name=row["name"], media_id=row["media_id"]) for row in rows]

    def clash(self, course: str, material: Material) -> Material | None:
        """这门课里已经有**同名**或者**同一份**的那一条;没有回 None。先查名字(说得更具体)。"""
        row = self._conn.execute(
            "SELECT name, media_id FROM materials WHERE course = ? AND (name = ? OR media_id = ?)"
            " ORDER BY name = ? DESC LIMIT 1",
            (course, material.name, material.media_id, material.name),
        ).fetchone()
        return None if row is None else Material(name=row["name"], media_id=row["media_id"])

    def count(self, course: str) -> int:
        """这门课下有几份。改名 / 删除 / 撤回的回话里要说"几份课件跟着过去了"。"""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM materials WHERE course = ?", (course,)
        ).fetchone()
        return int(row["n"])

    def add(self, course: str, material: Material) -> None:
        """记一条归属。重了由 UNIQUE 抛 `sqlite3.IntegrityError`——调用方应该先 `clash` 过。"""
        self._conn.execute(
            "INSERT INTO materials (course, name, media_id) VALUES (?, ?, ?)",
            (course, material.name, material.media_id),
        )

    def relabel(self, old: str, new: str) -> None:
        """把 `old` 这个课程键下的所有课件搬到 `new` 下——目录搬到哪,归属跟到哪。"""
        self._conn.execute("UPDATE materials SET course = ? WHERE course = ?", (new, old))
