"""finance bundle —— 记账与消费分析(对话侧)。

M4-1 立的骨架:manifest + 独占 SQLite + 统一构造入口 `build(...)`。三个工具的
**签名与文档在 M4-1 定死**(工具 schema 是前缀第0层,顺序冻结后不许再动);
M4-2 起只换函数体、不动签名与 docstring——docstring 就是 schema,改它是一次前缀重建。

M4-2 落地 `record_expense`,M4-3 落地 `query_spending`,M4-4 落地 `list_recent`。
"""

import re
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from fastmcp import FastMCP

from bundles.runtime import BundleRuntime
from lararium.db import add_missing_columns, open_connection, transaction

# 固定类目,不是自由文本:模型每次发明一个新词(「吃饭」「餐饮」「外卖」各记一笔),
# M4-3 的 GROUP BY 就聚不出东西来。顺序即 E2 提示里列出的顺序,保持稳定。
CATEGORIES = ("餐饮", "交通", "日用", "娱乐", "医疗", "人情", "其他")

# 金额上界 = SQLite INTEGER 能存的最大值(int64)。超过它 sqlite3 在**绑定参数时**抛
# OverflowError,而那不是 sqlite3.Error 的子类——异常会直接逃出工具边界(M4-2 补)。
# 真人说不出 9.2e16 元这种数,但 E2 的意义正是边界上不推演可能性。
_MAX_CENTS = 2**63 - 1

# 聚合结果的行数上限(A4 工具铁律)。按类目最多 7 行、天然安全;按天不封顶——查一年
# 就是 365 行,一次工具调用把 L0 顶穿,而压缩是全系统仅有的两个缓存重建点之一。
#
# **31 的判据是「最常见的那个查询要原样装得下」,不是对齐 MAX_SEARCH_HITS**(初版取 20,
# 就是照抄了那个数)。查这个月按天 = 31 天,是财务 bundle 最常见的一次查询,被截断则
# monthly-review 的第一句方法「先看总额趋势」当场做不到;而防「查一年」,31 一样防得住。
MAX_GROUP_ROWS = 31

# list_recent 的条数硬上限。它是**全系统唯一返回原始流水的工具**,而 limit 是模型可控
# 参数:负数在 SQLite 的 LIMIT 里是"不限制"(M3-1 的教训),不钳制就是全表倒进上下文。
MAX_RECENT_ROWS = 20
# 单条 note 的显示上限。20 行、每行一条无限长备注,一样能顶穿 L0;对齐 steward/tools.py
# MAX_HIT_CHARS 的用意,数字按"一行看得完"取。
MAX_NOTE_CHARS = 60

# 围栏分隔符。**这是从 lararium.steward.assembler 抄来的一份**——bundle 不许 import
# steward(它是未来的独立容器,零依赖是刻意的),所以只能抄。抄了就会漂,
# `test_fence_markers_match_the_stewards` 把两边钉在一起:哪天 assembler 改了分隔符,
# 那条测试立刻红,而不是让这里的防线静默失效。
FENCE_OPEN = "<<<"
FENCE_CLOSE = ">>>"
# 本行里包 note 用的界符。note 正文里出现它就能在同一行伪造出第二个字段,一并中和。
_NOTE_OPEN = "「"
_NOTE_CLOSE = "」"

# group_by 的规范值与同义词。类目是**存下去**的东西所以必须固定,group_by 只是控制参数、
# 进 SQL 前就归一成规范值,收几个同义词不会污染数据——而模型用中文思考,
# 让它因为写了"类目"而吃一次 E2 往返是白烧钱。
# 两条写全的字面量,不用 f-string 拼列名:拼出来的 SQL 会被 S608 盯上(而且它是对的
# ——白名单今天成立不等于明天有人加个分支时还成立),写死则连"可能"都没有,
# 顺带还能整条 grep 出来。聚合全在 SQL 里做完:取回来在 Python 里算,就已经把三百条
# 塞进内存了,离塞进上下文只差一步(A4)。
_GROUP_SQL = {
    "category": (
        "SELECT category AS grp, SUM(amount_cents) AS cents, COUNT(*) AS n"
        " FROM expenses WHERE occurred_at >= ? AND occurred_at < ? AND voided_by IS NULL"
        " GROUP BY grp ORDER BY cents DESC"
    ),
    "day": (
        # 按天走**时间正序**:分组键本身就是时间序,按金额降序等于把时间轴打散
        # (日子会跳着来)。且两种排序不对称——「哪几天花得多」从正序里一眼能挑,
        # 「趋势」从金额 top-N 里推不出来。正序严格更强。
        "SELECT substr(occurred_at, 1, 10) AS grp, SUM(amount_cents) AS cents, COUNT(*) AS n"
        " FROM expenses WHERE occurred_at >= ? AND occurred_at < ? AND voided_by IS NULL"
        " GROUP BY grp ORDER BY grp ASC"
    ),
}

# list_recent 的两条查询。日期缺省时用哨兵边界(存的是 'YYYY-MM-DDTHH:MM:SS',字典序
# 可比),这样 WHERE 恒定、只有 ORDER BY 两种,不必拼 SQL(同 _GROUP_SQL 的理由)。
_OPEN_LOWER = "0000-01-01"
_OPEN_UPPER = "9999-12-31"
_RECENT_COLUMNS = "SELECT id, occurred_at, category, amount_cents, note, voided_by FROM expenses"
# `? = 1 OR voided_by IS NULL`:用绑定参数开关"要不要看作废行",而不是拼两份 WHERE
# ——拼出来的 SQL 会被 S608 盯上,而且分支越多越容易有一条忘了加条件。
_RECENT_WHERE = " WHERE occurred_at >= ? AND occurred_at < ? AND (? = 1 OR voided_by IS NULL)"
_RECENT_SQL = {
    "recent": _RECENT_COLUMNS + _RECENT_WHERE + " ORDER BY occurred_at DESC, id DESC LIMIT ?",
    # 金额并列时用时间倒序兜底,保证同一份数据每次返回同一个顺序(前缀之外也不该抖)
    "largest": _RECENT_COLUMNS
    + _RECENT_WHERE
    + " ORDER BY amount_cents DESC, occurred_at DESC, id DESC LIMIT ?",
}

_ORDER_BY = {
    "recent": "recent",
    "最近": "recent",
    "最新": "recent",
    "largest": "largest",
    "最大": "largest",
    "最大额": "largest",
}

_GROUP_BY = {
    "category": "category",
    "类目": "category",
    "按类目": "category",
    "分类": "category",
    "按分类": "category",
    "day": "day",
    "date": "day",
    "天": "day",
    "按天": "day",
    "日": "day",
    "按日": "day",
}

# 架构测试 test_only_the_ledger_module_writes_files 只放行 ledger.py 写文件;
# bundle 的库是 SQLite,写入走 sqlite3 连接,不落那条 AST 的禁写面。
# occurred_at 存的是**配置时区的墙上时间、不带偏移**:SQLite 的 date() 见到偏移会先
# 折回 UTC 再切天(date('2026-08-21T01:00:00+08:00') = 2026-08-20),M4-3 按天/按月
# 分组就会静悄悄错一天。单用户单时区,存墙上时间既够用又让字符串比较直接可用。
_FINANCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS expenses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    amount_cents INTEGER NOT NULL,
    category     TEXT    NOT NULL,
    occurred_at  TEXT    NOT NULL,
    note         TEXT,
    created_at   TEXT    NOT NULL,
    -- M5-15:被谁替代了。NULL = 这行still有效。**作废而不是就地覆盖**,形状照抄
    -- memory/ledger.py:保留全部历史,让当前视图干净。就地覆盖读起来最干净,
    -- 但它销毁证据,而"进过系统的一切留痕"是不可协商第 3 条。
    voided_by    INTEGER REFERENCES expenses(id)
);
-- occurred_at 是唯一的检索维度:list_recent 按它倒序取前 N,query_spending 按它做范围
-- 扫描。没有索引时两者都要全表扫,而这张表只会越长越长(M4-4 补)。
CREATE INDEX IF NOT EXISTS idx_expenses_occurred_at ON expenses(occurred_at);
"""

# 老库补列:`CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作,不补的症状是
# "新装的机器好使,你自己那台不好使",而且报在运行时不报在启动时(M5-4 的教训)。
_ADDED_COLUMNS = (
    (
        "PRAGMA table_info(expenses)",
        "voided_by",
        "ALTER TABLE expenses ADD COLUMN voided_by INTEGER REFERENCES expenses(id)",
    ),
)


def _connect(root: Path) -> sqlite3.Connection:
    """finance 独占自己的库(§5 数据产权):只碰 data_dir/finance/finance.sqlite。"""
    root.mkdir(parents=True, exist_ok=True)
    # M5-8:走 `open_connection` 而不是裸 sqlite3——**bundle 的库面对的是同一个线程池、
    # 同一个洞**。模型一口气报三笔,三个 `record_expense` 并发跑,这条连接照样会烂。
    conn = open_connection(root / "finance.sqlite")
    conn.executescript(_FINANCE_SCHEMA)
    add_missing_columns(conn, _ADDED_COLUMNS)
    return conn


def _to_cents(amount: float) -> int | None:
    """元 → 整数分。走 Decimal 而不是 round(amount * 100):后者在 0.1+0.2 这类值上
    会把误差带进账,月度合计对不上一分钱时你已经查不出是哪笔了。看不懂的值返回 None。"""
    try:
        return int((Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError):
        return None  # NaN / inf / 压根不是数


def _parse_when(raw: str, tz: ZoneInfo) -> datetime | None:
    """把模型给的时间换算成配置时区的墙上时间。看不懂返回 None(由调用方给人话提示)。

    带偏移的先 astimezone 折过来再去掉偏移——原样存会让 M4-3 的分组按 UTC 切天。
    """
    try:
        dt = datetime.fromisoformat(raw.strip())
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(tz)
    return dt.replace(tzinfo=None)


def _parse_day(raw: str) -> date | None:
    """只认 YYYY-MM-DD。看不懂返回 None(由调用方给人话提示)。"""
    try:
        return date.fromisoformat(raw.strip())
    except (ValueError, AttributeError):
        return None


def _render_note(note: str | None) -> str:
    """把 note 当**不可信文本**渲染,一条都不例外。

    note 是模型写的,而模型在不可信轮会把短信正文转述进去(L3:模型输出是不可信输入)。
    bundle 拿不到本轮的信任度,所以不做区分——统一过三刀:

    - **折行**:换行不折,一条 note 就能伪造出后续流水行,而伪造出来的那行和真实流水
      形式上一模一样,还坐在可信位置、没有任何来源标记(P1-2);
    - **中和分隔符**:围栏归 Steward 用,这条输出将来会以 tool_result 的身份被
      search_history 捞回去再渲染一次,正文里的 `>>>` 能提前闭合围栏(P1-3);
      本行的界符同理;
    - **截断**:先折再截,别让空白吃掉预算(照抄 tools.py `_render_hit` 的顺序)。
    """
    if not note:
        return ""
    folded = re.sub(r"\s+", " ", note).strip()
    safe = (
        folded.replace(FENCE_OPEN, "＜＜＜")  # noqa: RUF001 - 换成全角形近字是目的不是笔误
        .replace(FENCE_CLOSE, "＞＞＞")  # noqa: RUF001 - 同上
        .replace(_NOTE_OPEN, "﹁")
        .replace(_NOTE_CLOSE, "﹂")
    )
    return f" · 备注{_NOTE_OPEN}{safe[:MAX_NOTE_CHARS]}{_NOTE_CLOSE}"


def _group_line(row: sqlite3.Row) -> str:
    return f"- {row['grp']} {_yuan(row['cents'])} 元({row['n']} 笔)"


def _cents(rows: list[sqlite3.Row]) -> int:
    return sum(r["cents"] for r in rows)


def _yuan(cents: int) -> str:
    """分 → 元的显示形态。全程 Decimal,显示层也不让浮点沾边。"""
    return f"{Decimal(cents) / 100:.2f}"


def _tool_functions(conn: sqlite3.Connection, tz: ZoneInfo) -> list[Callable]:
    """工具顺序即冻结顺序(前缀第0层),由 manifest.yaml 与测试钉死。
    工具边界不许抛异常(E2)——出错给模型一句人话,让它能自己纠正而不是整轮炸掉。
    """

    def record_expense(
        amount: float,
        category: str,
        occurred_at: str | None = None,
        note: str | None = None,
    ) -> str:
        """记一笔支出。amount 为元(内部转整数分存储,不存浮点);category 必须是固定
        类目之一(餐饮|交通|日用|娱乐|医疗|人情|其他),非法类目返回可读提示并列出合法值;
        occurred_at 缺省用当前时间,给了就用给的——「昨天」「上周三」这类**相对时间要
        先调 current_time 拿到今天是几号、自己换算成 YYYY-MM-DD 再传**,这里认不了。"""
        cents = _to_cents(amount)
        if cents is None or cents <= 0 or cents > _MAX_CENTS:
            return f"金额不对({amount}):要一个大于 0 的数字,单位是元(比如 28.5)。这笔没记。"
        if category not in CATEGORIES:
            legal = "|".join(CATEGORIES)
            return f"没有「{category}」这个类目。合法类目:{legal}。挑一个最接近的重记,这笔没记。"

        if occurred_at is None:
            when = datetime.now(tz).replace(tzinfo=None)
        else:
            parsed = _parse_when(occurred_at, tz)
            if parsed is None:
                # 不许悄悄退回"现在":模型说的是「上周三」,账上落成今天,没有任何人会知道。
                return (
                    f"看不懂时间「{occurred_at}」:要 YYYY-MM-DD 或 YYYY-MM-DD HH:MM。"
                    f"相对时间先调 current_time 换算成日期再传。这笔没记。"
                )
            when = parsed

        stamp = when.isoformat(timespec="seconds")
        try:
            conn.execute(
                "INSERT INTO expenses (amount_cents, category, occurred_at, note, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    cents,
                    category,
                    stamp,
                    note,
                    datetime.now(tz).replace(tzinfo=None).isoformat(timespec="seconds"),
                ),
            )
        except sqlite3.Error as exc:  # E2:写不进去也要让模型知道这步没成
            return f"这笔没记进去(库写入失败:{exc})。"

        # 备注走 **和 list_recent 同一个** 渲染器。曾经这里是 `f",{note}"` 原样回吐:
        # 换行没折、围栏没中和,而隔壁 list_recent 渲染得干干净净——同一个文件里两套
        # 渲染器,正是 assembler.py 记下的那条教训(P1-1:当前轮包了、历史轮没包)。
        # 共用之后,包裹要么两边都有、要么两边都没有,不会只在一边悄悄退化。
        return (
            f"记好了:{category} {_yuan(cents)} 元"
            f"({when.strftime('%m-%d %H:%M')}){_render_note(note)}。"
        )

    def query_spending(
        since: str,
        until: str,
        group_by: str,
    ) -> str:
        """按类目/按天聚合一段时间内的支出(since/until 格式 YYYY-MM-DD,两端都含),
        group_by 取 category(按类目,金额从高到低)或 day(按天,时间正序);返回总额 +
        每组一行结论;聚合在 SQL 里算完再返回,**绝不返回单笔流水**。
        区间太长时按天会砍掉最早那段、合并成一行「更早 N 天合计」放在最前面,
        而**总额那一行始终是全区间的**。"""
        start, end = _parse_day(since), _parse_day(until)
        if start is None or end is None:
            bad = since if start is None else until
            return f"看不懂日期「{bad}」:要 YYYY-MM-DD。相对时间先调 current_time 换算。"
        if start > end:
            return f"日期反了:since={since} 晚于 until={until},换过来再查。"
        mode = _GROUP_BY.get(group_by.strip().lower() if isinstance(group_by, str) else "")
        if mode is None:
            return f"看不懂 group_by「{group_by}」:只能是 category(按类目)或 day(按天)。"

        # until 含端点,但比较的是 'YYYY-MM-DDTHH:MM:SS' 字符串——用 <= until 会把当天
        # 带时刻的流水全吃掉('2026-08-31T20:00:00' > '2026-08-31')。所以上界取次日零点、
        # 开区间。这样既含全端点,又保持成范围扫描(M4-4 加索引后直接受益)。
        upper = (end + timedelta(days=1)).isoformat()
        try:
            groups = list(conn.execute(_GROUP_SQL[mode], (start.isoformat(), upper)))
        except sqlite3.Error as exc:  # E2:查不了也要让模型知道,而不是整轮炸掉
            return f"查不了(库读取失败:{exc})。"

        if not groups:
            return f"{since} ~ {until} 没有记录。"

        total = sum(r["cents"] for r in groups)
        count = sum(r["n"] for r in groups)
        label = "类目" if mode == "category" else "天"
        # 总额那行始终是**全区间**的,和截断与否无关——截掉的部分单列一行报合计,
        # 两边永远对得上。静默截断读起来和"就这些"一模一样,模型会拿残缺的合计下结论。
        lines = [f"{since} ~ {until},共 {count} 笔,合计 {_yuan(total)} 元(按{label}):"]
        if mode == "category":
            # 类目按金额降序,砍尾巴:哪一类吃掉了预算,第一行就是答案。
            # 最多 7 组,这条截断线实际上摸不着,留着是为了不让"以后加类目"变成隐患。
            dropped, shown = groups[MAX_GROUP_ROWS:], groups[:MAX_GROUP_ROWS]
            lines += [_group_line(r) for r in shown]
            if dropped:
                lines.append(
                    f"- 其余 {len(dropped)} 组合计 {_yuan(_cents(dropped))} 元(未逐条列出)"
                )
        else:
            # 按天是时间正序,所以砍的是**最早**那段(问"今年花了多少"的人关心近况),
            # 合计行放最前面——它在时间轴上本来就该在那儿。
            dropped, shown = groups[:-MAX_GROUP_ROWS], groups[-MAX_GROUP_ROWS:]
            if dropped:
                lines.append(
                    f"- 更早 {len(dropped)} 天合计 {_yuan(_cents(dropped))} 元(未逐条列出)"
                )
            lines += [_group_line(r) for r in shown]
        return "\n".join(lines)

    def list_recent(
        limit: int = 10,
        since: str | None = None,
        until: str | None = None,
        order: str = "recent",
        include_voided: bool = False,
    ) -> str:
        """列出流水——全系统唯一返回原始流水的工具,因此硬封顶(上限 20),limit 为负数
        或超大值都钳制到上限。since/until 格式 YYYY-MM-DD、两端都含,缺省为全时段;
        order 取 recent(最近的在前)或 largest(金额从大到小)。回答"某段时间最大的
        一笔"要 order=largest **并且**给上 since/until——只给 order 会答成全时段之最。
        每行开头的 #id 可以直接喂给 amend_expense;被改过的旧行默认不列,
        include_voided=True 才带上(标「已作废」)。"""
        # 负数在 SQLite 的 LIMIT 里是"不限制",不钳制就是全表倒进上下文(M3-1 教训)。
        n = MAX_RECENT_ROWS if limit < 1 else min(limit, MAX_RECENT_ROWS)
        mode = _ORDER_BY.get(order.strip().lower() if isinstance(order, str) else "")
        if mode is None:
            return f"看不懂 order「{order}」:只能是 recent(最近的在前)或 largest(金额从大到小)。"

        lower, upper = _OPEN_LOWER, _OPEN_UPPER
        if since is not None:
            start = _parse_day(since)
            if start is None:
                return f"看不懂日期「{since}」:要 YYYY-MM-DD。相对时间先调 current_time 换算。"
            lower = start.isoformat()
        if until is not None:
            end = _parse_day(until)
            if end is None:
                return f"看不懂日期「{until}」:要 YYYY-MM-DD。相对时间先调 current_time 换算。"
            # 上界取次日零点、开区间,理由同 query_spending(闭区间会吃掉末日带时刻的流水)
            upper = (end + timedelta(days=1)).isoformat()

        try:
            rows = list(
                conn.execute(_RECENT_SQL[mode], (lower, upper, 1 if include_voided else 0, n))
            )
        except sqlite3.Error as exc:  # E2:查不了也要让模型知道,而不是整轮炸掉
            return f"查不了(库读取失败:{exc})。"

        scoped = since is not None or until is not None
        if not rows:
            # "这段没有" ≠ "一笔都没记过":混成一句会让模型以为账本是空的。
            return (
                f"{since or '开头'} ~ {until or '现在'} 没有记录。" if scoped else "还没有记过账。"
            )

        word = "最近" if mode == "recent" else "最大的"
        scope = f"{since or '开头'} ~ {until or '现在'} " if scoped else ""
        lines = [f"{scope}{word} {len(rows)} 笔:"]
        for r in rows:
            when = r["occurred_at"].replace("T", " ")[:16]
            voided = f" 已作废(见 #{r['voided_by']})" if r["voided_by"] is not None else ""
            lines.append(
                f"- #{r['id']} {when} {r['category']} {_yuan(r['amount_cents'])} 元"
                f"{_render_note(r['note'])}{voided}"
            )
        return "\n".join(lines)

    def amend_expense(
        expense_id: int,
        amount: float | None = None,
        category: str | None = None,
        occurred_at: str | None = None,
        note: str | None = None,
    ) -> str:
        """改一笔已经记错的流水。expense_id 是 list_recent 每行开头那个 #id;
        只传要改的字段,没传的原样保留。**旧行不会被删掉,而是标成作废并指向新行**
        ——所以改错了还能再改,历史查得到(list_recent 带 include_voided=True 能看见)。
        账上永远只留一条有效记录,不需要、也没有删除这个操作。"""
        row = conn.execute(
            "SELECT id, amount_cents, category, occurred_at, note, voided_by"
            " FROM expenses WHERE id = ?",
            (expense_id,),
        ).fetchone()
        if row is None:
            return f"没有 #{expense_id} 这笔。先用 list_recent 看一眼有哪些,#id 在每行开头。"
        if row["voided_by"] is not None:
            # 顺着旧 id 一路改下去会长出一条谁也读不懂的链子,而且每改一次多一行垃圾。
            return (
                f"#{expense_id} 已经改过了,现在的那条是 #{row['voided_by']}。"
                f"要接着改就改 #{row['voided_by']}。"
            )

        cents = row["amount_cents"] if amount is None else _to_cents(amount)
        if cents is None or cents <= 0 or cents > _MAX_CENTS:
            # **先校验再动手**:先作废后失败会把一笔好记录改没了。
            return f"金额不对({amount}):要一个大于 0 的数字,单位是元。这笔没改。"
        new_category = row["category"] if category is None else category
        if new_category not in CATEGORIES:
            legal = "|".join(CATEGORIES)
            return f"没有「{new_category}」这个类目。合法类目:{legal}。这笔没改。"
        stamp = row["occurred_at"]
        if occurred_at is not None:
            parsed = _parse_when(occurred_at, tz)
            if parsed is None:
                return (
                    f"看不懂时间「{occurred_at}」:要 YYYY-MM-DD 或 YYYY-MM-DD HH:MM。"
                    f"相对时间先调 current_time 换算成日期再传。这笔没改。"
                )
            stamp = parsed.isoformat(timespec="seconds")
        new_note = row["note"] if note is None else note

        try:
            # 作废与新记必须一起成或一起不成:崩在中间会留下"作废了但没有替代行",
            # 那正是这一步要消灭的形态(账上凭空少一笔)。
            with transaction(conn):
                cur = conn.execute(
                    "INSERT INTO expenses"
                    " (amount_cents, category, occurred_at, note, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        cents,
                        new_category,
                        stamp,
                        new_note,
                        datetime.now(tz).replace(tzinfo=None).isoformat(timespec="seconds"),
                    ),
                )
                new_id = int(cur.lastrowid or 0)
                conn.execute("UPDATE expenses SET voided_by = ? WHERE id = ?", (new_id, expense_id))
        except sqlite3.Error as exc:  # E2:改不动也要让模型知道这步没成
            return f"这笔没改成(库写入失败:{exc})。"

        before = f"{row['category']} {_yuan(row['amount_cents'])} 元"
        after = f"{new_category} {_yuan(cents)} 元"
        return (
            f"改了 #{expense_id} → #{new_id}:{before} → {after}"
            f"({stamp.replace('T', ' ')[:16]}){_render_note(new_note)}。旧的那条留着,标了作废。"
        )

    # 顺序即冻结顺序(前缀第 0 层):**只追加在末尾**,不许插队。
    return [record_expense, query_spending, list_recent, amend_expense]


def build(data_dir: Path, *, timezone: str) -> BundleRuntime:
    """统一构造入口(bundle 契约),至少含 tools: list[Callable]。工具顺序由
    manifest.yaml 与测试钉死(前缀第0层)。

    timezone 由组装根注入而不是在这里给默认值:默认值会和 `Settings.timezone` 各走各的,
    用户改了配置、账本却还按老时区记——那正是 M1 Task 9 修过的那个 8 小时时差。
    """
    conn = _connect(Path(data_dir) / "finance")
    return BundleRuntime(tools=_tool_functions(conn, ZoneInfo(timezone)))


def create_server(data_dir: Path, *, timezone: str) -> FastMCP:
    """MCP 服务入口,和 memory 同形状;生产单独容器时由它接管。"""
    mcp = FastMCP("finance")
    for fn in build(data_dir, timezone=timezone).tools:
        mcp.tool()(fn)
    return mcp


if __name__ == "__main__":
    import os

    # 独立容器形态下 bundle 自己读 env(进程边界就是它的配置入口)。这两个默认值必须和
    # `Settings` 里同名变量的默认值一致——改一边就得改另一边,否则容器化后账会差时区。
    create_server(
        Path(os.environ.get("LARARIUM_DATA_DIR", "./data")),
        timezone=os.environ.get("LARARIUM_TIMEZONE", "Asia/Shanghai"),
    ).run()
