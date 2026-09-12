"""PDF 每一页转出来的文字——缓存(M6-6c)。

**Steward 独占存储**,和起居注、话头同库同产权(表在 `db.SCHEMA`)。bundle 碰不到它:
import-linter 挡住 bundle import steward,`test_no_bundle_touches_the_page_text_cache`
挡住另一扇门(自己开连接读 steward.sqlite、照着表名写 SQL)。

**键是 `(sha256, page)`**:sha256 是媒体池里那份 PDF 的内容哈希(池子里的文件名就是它,
附件报告里的 id 是它的前 12 位)。同一份发两次是同一个键,"只转一次"不需要任何去重逻辑。

**写它的只有 `transcribe.Transcriber`,读它的是 `read_pdf`。** 6d 的按 id 搜也会读它——
按 sha256 取出每一页的 `text`,这张表的形状已经够用;**这一轮一行搜索代码都不写**。

**一页的状态不另存一列,由 `text` 和 `attempts` 推出来**(一个事实一个出处——状态列和
这两列各说各的那天,"说转好了、文字却是空的"这种账就对不上了):

```
没有行                               还没调过模型          → pending
text 不为 NULL                       转好了                → done
text 为 NULL,attempts 到了上限       转失败了,不再调       → failed
text 为 NULL,attempts 没到上限       调过没成,下次接着试   → pending
```

`attempts` 在**调模型之前**就 +1(`begin_attempt`):调到一半进程没了也算一次。
要是调完才记,一页每次都把进程拖死(或者每次都在记之前崩),它就会被无限重试
——而那正是"反复失败要有上限、不能无限重试烧钱"要挡的东西。
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

# 一份 PDF 最多转前几页。**超出的页读的时候只有图,并说清"这份只转了前 N 页"**。
#
# 200 的理由(2026-09-13 定,真机没跑过,数是估的,口径写在这里):
# - **覆盖真实会发的东西**:一讲的幻灯片 30-80 页、讲义 / 合同 / 论文几页到四五十页,
#   一整学期的课件合成一份也就一两百页——这些**整份都转完**;
# - **一本 800 页的教材发过来**:转前 200 页(前几章),剩下的页照样能看图。
#   按 PLAN 实测的一页约 1.9 秒、1.4k token 估(那次的图比现在的 1600 长边小,A4 现在约
#   2400 token 的图 + 几百 token 的字),单路串行 200 页约 7-10 分钟、六七十万 token,
#   **一次性**,之后只读缓存;
# - **封的是一份文件能烧掉的上限**:一份扫描成 5000 页的东西不该让后台连着调几个小时,
#   而这个上限是那种情况下唯一的闸。
# 读的时候(`PdfText.page`)每次取这个模块属性,不在导入时抄一份:改了它,老文件下次扫到
# 会接着把多出来的页补上,不用迁移。
MAX_CONVERTED_PAGES = 200

# 一页最多调几次模型。和收件箱的重试上限(`LARARIUM_MAX_ATTEMPTS` 默认 3)同一个数、
# 同一个道理:偶发的 429 / 超时有机会重来,一直失败的不会一直烧钱。**不可重试的错**
# (服务商明确拒了这张图)和**画不出来的页**直接判死,不占满三次。
MAX_PAGE_ATTEMPTS = 3

PageState = Literal["done", "pending", "failed", "beyond"]

# 下一份该转的:按发现先后,**整份没收尾**(转好 + 判死 的页数 < 该转的页数)的第一份。
# 该转的页数 = min(总页数, 上限)。打不开的、页数是 0 的不在候选里。
_NEXT_DOC_SQL = """
SELECT d.sha256, MIN(d.total_pages, ?) AS upto FROM pdf_docs d
WHERE d.unreadable = '' AND d.total_pages > 0
  AND (
      SELECT COUNT(*) FROM pdf_pages p
      WHERE p.sha256 = d.sha256 AND p.page <= ? AND (p.text IS NOT NULL OR p.attempts >= ?)
  ) < MIN(d.total_pages, ?)
ORDER BY d.found_at, d.rowid
LIMIT 1
"""


@dataclass(frozen=True)
class PageText:
    """`read_pdf` 读一页时要的全部。`converted` 是这份已经转好几页,`limit` 是上限。"""

    state: PageState
    text: str
    converted: int
    limit: int


def _now() -> str:
    return datetime.now(UTC).isoformat()


class PdfText:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ── 查询 ────────────────────────────────────────────────────────────

    def known(self) -> set[str]:
        """已经登记过的那几份(打不开的也算——登记过就不再去开它)。"""
        return {r["sha256"] for r in self._conn.execute("SELECT sha256 FROM pdf_docs").fetchall()}

    def page(self, sha256: str, page: int) -> PageText:
        """这一页现在是哪种状态。**没登记过的也答得出**(刚收到、转换器还没扫到):pending。"""
        limit = MAX_CONVERTED_PAGES
        converted = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM pdf_pages WHERE sha256=? AND text IS NOT NULL", (sha256,)
            ).fetchone()[0]
        )
        if page > limit:
            return PageText("beyond", "", converted, limit)
        doc = self._conn.execute(
            "SELECT unreadable FROM pdf_docs WHERE sha256=?", (sha256,)
        ).fetchone()
        if doc is not None and doc["unreadable"]:
            return PageText("failed", "", converted, limit)
        row = self._conn.execute(
            "SELECT text, attempts FROM pdf_pages WHERE sha256=? AND page=?", (sha256, page)
        ).fetchone()
        if row is not None and row["text"] is not None:
            return PageText("done", str(row["text"]), converted, limit)
        if row is not None and row["attempts"] >= MAX_PAGE_ATTEMPTS:
            return PageText("failed", "", converted, limit)
        return PageText("pending", "", converted, limit)

    def next_page(self) -> tuple[str, int] | None:
        """下一页该转哪一页;都收尾了回 None。

        一份之内**先转没试过的,再回头试失败过的**(失败次数少的、页码小的先):一页一直
        出错,不该让后面几十页陪它等退避——"一页失败不影响别的页"在排队这一层也成立。
        """
        doc = self._conn.execute(
            _NEXT_DOC_SQL,
            (MAX_CONVERTED_PAGES, MAX_CONVERTED_PAGES, MAX_PAGE_ATTEMPTS, MAX_CONVERTED_PAGES),
        ).fetchone()
        if doc is None:
            return None
        sha256, upto = str(doc["sha256"]), int(doc["upto"])
        rows = self._conn.execute(
            "SELECT page, text IS NOT NULL AS done, attempts FROM pdf_pages "
            "WHERE sha256=? AND page <= ?",
            (sha256, upto),
        ).fetchall()
        tried = {int(r["page"]) for r in rows}
        for page in range(1, upto + 1):
            if page not in tried:
                return sha256, page
        retry = min(
            (r for r in rows if not r["done"] and r["attempts"] < MAX_PAGE_ATTEMPTS),
            key=lambda r: (r["attempts"], r["page"]),
        )
        return sha256, int(retry["page"])

    # ── 命令 ────────────────────────────────────────────────────────────

    def register(self, sha256: str, *, total_pages: int, unreadable: str = "") -> None:
        """登记一份新发现的 PDF。**已经登记过的不动**——重登记绝不许清掉已转好的页。"""
        self._conn.execute(
            "INSERT OR IGNORE INTO pdf_docs (sha256, total_pages, unreadable, found_at) "
            "VALUES (?, ?, ?, ?)",
            (sha256, total_pages, unreadable, _now()),
        )

    def begin_attempt(self, sha256: str, page: int) -> None:
        """要调模型了,先记一次(见模块 docstring:崩在调用中间也算数)。"""
        self._conn.execute(
            "INSERT INTO pdf_pages (sha256, page, attempts, updated_at) VALUES (?, ?, 1, ?) "
            "ON CONFLICT(sha256, page) DO UPDATE SET "
            "attempts = pdf_pages.attempts + 1, updated_at = excluded.updated_at",
            (sha256, page, _now()),
        )

    def save_text(self, sha256: str, page: int, text: str) -> None:
        self._conn.execute(
            "INSERT INTO pdf_pages (sha256, page, text, attempts, updated_at) VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(sha256, page) DO UPDATE SET "
            "text = excluded.text, error = '', updated_at = excluded.updated_at",
            (sha256, page, text, _now()),
        )

    def record_failure(self, sha256: str, page: int, error: str, *, give_up: bool) -> None:
        """这一次没成。次数在 `begin_attempt` 里已经记过了,这里只记原因;
        `give_up=True`(服务商明确拒了、这页画不出来)直接顶到上限——再调也是同一个结果。"""
        self._conn.execute(
            "INSERT INTO pdf_pages (sha256, page, attempts, error, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(sha256, page) DO UPDATE SET "
            "attempts = MAX(pdf_pages.attempts, excluded.attempts), "
            "error = excluded.error, updated_at = excluded.updated_at",
            (sha256, page, MAX_PAGE_ATTEMPTS if give_up else 1, error, _now()),
        )
