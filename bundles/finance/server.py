"""finance bundle —— 记账与消费分析(对话侧)。

M4-1 立的骨架:manifest + 独占 SQLite + 统一构造入口 `build(...)`。三个工具的
**签名与文档在 M4-1 定死**(工具 schema 是前缀第0层,顺序冻结后不许再动);
M4-2 起只换函数体、不动签名与 docstring——docstring 就是 schema,改它是一次前缀重建。

M4-2 落地 `record_expense`,M4-3 落地 `query_spending`;`list_recent` 仍是 E2 人话占位,
正体在 M4-4。
"""

import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from fastmcp import FastMCP

from bundles.runtime import BundleRuntime

# 固定类目,不是自由文本:模型每次发明一个新词(「吃饭」「餐饮」「外卖」各记一笔),
# M4-3 的 GROUP BY 就聚不出东西来。顺序即 E2 提示里列出的顺序,保持稳定。
CATEGORIES = ("餐饮", "交通", "日用", "娱乐", "医疗", "人情", "其他")

# 金额上界 = SQLite INTEGER 能存的最大值(int64)。超过它 sqlite3 在**绑定参数时**抛
# OverflowError,而那不是 sqlite3.Error 的子类——异常会直接逃出工具边界(M4-2 补)。
# 真人说不出 9.2e16 元这种数,但 E2 的意义正是边界上不推演可能性。
_MAX_CENTS = 2**63 - 1

# 聚合结果的行数上限(A4 工具铁律)。按类目最多 7 行、天然安全;按天不封顶——查一年
# 就是 365 行,一次工具调用把 L0 顶穿,而压缩是全系统仅有的两个缓存重建点之一。
# 20 这个数对齐 steward/tools.py 的 MAX_SEARCH_HITS,理由相同。
MAX_GROUP_ROWS = 20

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
        " FROM expenses WHERE occurred_at >= ? AND occurred_at < ?"
        " GROUP BY grp ORDER BY cents DESC"
    ),
    "day": (
        "SELECT substr(occurred_at, 1, 10) AS grp, SUM(amount_cents) AS cents, COUNT(*) AS n"
        " FROM expenses WHERE occurred_at >= ? AND occurred_at < ?"
        " GROUP BY grp ORDER BY cents DESC"
    ),
}

_GROUP_BY = {
    "category": "category",
    "类目": "category",
    "分类": "category",
    "day": "day",
    "天": "day",
    "日": "day",
    "date": "day",
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
    created_at   TEXT    NOT NULL
);
"""


def _connect(root: Path) -> sqlite3.Connection:
    """finance 独占自己的库(§5 数据产权):只碰 data_dir/finance/finance.sqlite。"""
    root.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False 的理由同 memory:工具函数跑在框架的线程池里
    conn = sqlite3.connect(root / "finance.sqlite", isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_FINANCE_SCHEMA)
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
        occurred_at 缺省用当前时间,给了就用给的。"""
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

        yuan = f"{Decimal(cents) / 100:.2f}"
        tail = f",{note}" if note else ""
        return f"记好了:{category} {yuan} 元({when.strftime('%m-%d %H:%M')}{tail})。"

    def query_spending(
        since: str,
        until: str,
        group_by: str,
    ) -> str:
        """按类目/按天聚合一段时间内的支出(since/until 格式 YYYY-MM-DD),返回总额 +
        每组一行结论;聚合在 SQL 里算完再返回,**绝不返回单笔流水**。"""
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
        lines = [f"{since} ~ {until},共 {count} 笔,合计 {_yuan(total)} 元(按{label}):"]
        for row in groups[:MAX_GROUP_ROWS]:
            lines.append(f"- {row['grp']} {_yuan(row['cents'])} 元({row['n']} 笔)")
        rest = groups[MAX_GROUP_ROWS:]
        if rest:
            # 静默截断读起来和"就这些"一模一样,模型会拿残缺的合计去下结论。
            # 被砍掉的部分必须报出组数和合计,总额那一行才对得上。
            lines.append(
                f"- 其余 {len(rest)} 组合计 {_yuan(sum(r['cents'] for r in rest))} 元(未逐条列出)"
            )
        return "\n".join(lines)

    def list_recent(limit: int = 10) -> str:
        """列出最近几笔流水——全系统唯一返回原始流水的工具,因此硬封顶(上限 20),
        limit 为负数或超大值都钳制到上限。"""
        return "查最近流水还没接通(本里程碑后续实现),暂时查不了。"

    return [record_expense, query_spending, list_recent]


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
