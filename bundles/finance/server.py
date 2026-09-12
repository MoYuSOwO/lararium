"""finance bundle —— 记账与消费分析(对话侧)。

M4-1 立的骨架:manifest + 独占 SQLite + 统一构造入口 `build(...)`。三个工具的
**签名与文档在 M4-1 定死**(工具 schema 是前缀第0层,顺序冻结后不许再动);
M4-2 起只换函数体、不动签名与 docstring——docstring 就是 schema,改它是一次前缀重建。

M4-2 落地 `record_expense`,M4-3 落地 `query_spending`,M4-4 落地 `list_recent`。
M5-15 / M5-20 追加 `amend_expense` / `delete_expense`(两个都是真机逼出来的,不是设计
出来的);M6-3 追加 `record_income` / `list_income`——账本终于不只有"花出去"一个口子。

**S2:这个文件 995 行,远超那条 300 行的审查线,理由登记在此。** 它是两组"支出 / 钱回来
了"共 7 个工具的函数体,而它们必须共享同一条连接、同一套渲染器(`_render_note` 两个出口
渲染不一致就是 P1-1 那个事故)、同一份金额与时间解析。拆文件的唯一自然切线是按表分成
两个模块,而那样 `query_spending` 要跨模块读退款合计——把一条 SQL 查询变成一次跨模块
调用,换来的只是行数好看。真正该拆的那天是"某一侧长出自己的状态机"(比如退款要门控),
那时切线才是真的。注释占比高是刻意的:这个文件每一处防的都是一次真机事故。
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

# M6-3「钱回来了」的两个方向。**这两个不是一件事**,而混成一个的症状很具体:
# 记一笔 3000 生活费,然后「这个月花了多少」变成 -3000。顺序即 E2 提示里列出的顺序。
INCOME_KINDS = ("refund", "income")

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
# 「这一行还在账上」= `deleted_at IS NULL`,聚合与列表的每条查询都要带上。
# **抽成常量拼进去会被 S608 盯上,而它是对的**,所以逐条写死,改由
# `test_every_listing_query_filters_deleted_rows` 保证一条都不漏:再加一个状态位时
# 必然有一条忘了改,而症状是「删了还在」,正是 M5-20 要消灭的那个形态。
# M5-26 之后这里只剩一个状态位——**两个状态位本身就是上一版的代价**:
# 「这行还在账上吗」要同时问两处,而第二处从来没有读者。
_GROUP_SQL = {
    "category": (
        "SELECT category AS grp, SUM(amount_cents) AS cents, COUNT(*) AS n"
        " FROM expenses WHERE occurred_at >= ? AND occurred_at < ?"
        " AND deleted_at IS NULL"
        " GROUP BY grp ORDER BY cents DESC"
    ),
    "day": (
        # 按天走**时间正序**:分组键本身就是时间序,按金额降序等于把时间轴打散
        # (日子会跳着来)。且两种排序不对称——「哪几天花得多」从正序里一眼能挑,
        # 「趋势」从金额 top-N 里推不出来。正序严格更强。
        "SELECT substr(occurred_at, 1, 10) AS grp, SUM(amount_cents) AS cents, COUNT(*) AS n"
        " FROM expenses WHERE occurred_at >= ? AND occurred_at < ?"
        " AND deleted_at IS NULL"
        " GROUP BY grp ORDER BY grp ASC"
    ),
}

# list_recent 的两条查询。日期缺省时用哨兵边界(存的是 'YYYY-MM-DDTHH:MM:SS',字典序
# 可比),这样 WHERE 恒定、只有 ORDER BY 两种,不必拼 SQL(同 _GROUP_SQL 的理由)。
_OPEN_LOWER = "0000-01-01"
_OPEN_UPPER = "9999-12-31"
_RECENT_COLUMNS = (
    "SELECT id, occurred_at, category, amount_cents, note, deleted_at, deleted_reason FROM expenses"
)
# `? = 1 OR deleted_at IS NULL`:用绑定参数开关"要不要看已删的行",而不是拼两份 WHERE
# ——拼出来的 SQL 会被 S608 盯上,而且分支越多越容易有一条忘了加条件。
_RECENT_WHERE = " WHERE occurred_at >= ? AND occurred_at < ? AND (? = 1 OR deleted_at IS NULL)"
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

# M6-3 收入/退款那张表的两条查询。`deleted_at IS NULL` 从第一天就带上,理由和支出侧
# 一样(逐条写死、由 `test_every_income_query_filters_deleted_rows` 保证一条不漏)。
#
# **合计只有这一条 SQL**:`query_spending` 要的是「同期退款多少」,`list_income` 要的是
# 「收入多少、退款多少」,两个读者问的是同一件事的不同切片。写成两条的话,"哪些行算进来"
# 这个事实就有两份,而它们迟早不一致(M4-4、M5-21 都是这条)。
_INCOME_SQL = {
    "list": (
        "SELECT occurred_at, kind, amount_cents, note, of_expense_id FROM income"
        " WHERE occurred_at >= ? AND occurred_at < ? AND deleted_at IS NULL"
        " ORDER BY occurred_at DESC, id DESC LIMIT ?"
    ),
    "by_kind": (
        "SELECT kind, SUM(amount_cents) AS cents, COUNT(*) AS n FROM income"
        " WHERE occurred_at >= ? AND occurred_at < ? AND deleted_at IS NULL"
        " GROUP BY kind ORDER BY kind"
    ),
    # 验收补:哪些还活着的退款指着某一笔支出。**只有 delete_expense 读它**,理由见
    # 那里——`record_income` 挡住了"退款指向已删的支出",但反过来那一半原来是敞的。
    "against": (
        "SELECT SUM(amount_cents) AS cents, COUNT(*) AS n FROM income"
        " WHERE of_expense_id = ? AND deleted_at IS NULL"
    ),
}

# kind 的规范值与同义词。**它既存下去又决定聚合口径**,所以进库前必须归一成规范值:
# 库里混着「收入」和 income,按 kind 的合计就聚不出东西来。收同义词的理由同 `_GROUP_BY`
# ——模型用中文思考,让它因为写了"退款"而吃一次 E2 往返是白烧钱。
_KIND = {
    "refund": "refund",
    "退款": "refund",
    "退回": "refund",
    "income": "income",
    "收入": "income",
    "进账": "income",
}
_KIND_LABEL = {"refund": "退款", "income": "收入"}

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

# ── M6-3:为什么收入/退款是**新一张表**,不是给 `expenses` 加一个符号位
#
# 1. **符号位撑不住 `category` 那一列。** 它是固定 7 项、NOT NULL,存在的唯一理由就是
#    `_GROUP_SQL` 的 GROUP BY(M4-3 是照着这个小集合设计的)。一笔 3000 的生活费没有
#    类目:要么往 CATEGORIES 里塞个"收入"——那 7 项立刻不再是"钱花在哪儿"的划分,按
#    类目的复盘当场失真;要么硬编成"其他"——于是「其他」这一组变成支出减收入,是一个
#    读不懂的数。**这不是迁移成本,是正确性成本**,而在符号位方案里它无路可走。
# 2. **「这行算不算在账上」会重新变成两处要问的事。** M5-26 刚把 `voided_by` 那个第二
#    状态位拆掉,理由正是那一问要同时问两处、而第二处没有读者。符号位方案里每条查询的
#    `deleted_at IS NULL` 后面都得再跟一个 `AND kind = 'expense'`,而本文件的 SQL 全是
#    逐条写死的字面量(拼接会被 S608 盯上,而它是对的):`_GROUP_SQL` 两条 +
#    `_RECENT_WHERE` 一处(两条列表查询共用)+ `amend` 与 `delete` 各一处 SELECT =
#    **5 处都要多一个条件**。漏一处的症状是"收入被算成支出"或"退款行能被 amend 当支出
#    改",静默、且错在钱上。刚还完的债不该立刻再欠一笔。
# 3. **底稿不许被动。** 用户自己拒绝过"去动那两笔 GPT 的账",理由是「那样底稿就不是原始
#    记录了」。`list_recent` 是全系统唯一返回原始流水的工具;分表之后它**物理上看不见**
#    退款行,「逐字节不变」不依赖任何一个过滤条件成立。
# 4. **两个问题分开问,两条查询都简单。** 「花了多少」= 支出合计 - 同期退款;「进了多少」
#    = 同期收入。各自一条一行 WHERE 的查询。符号位方案要在一条 SQL 里用 CASE 把"退款减、
#    收入不减"写进表达式——那条规则就藏在 SQL 里,没有名字,也没人能对它下断言。
#
# 代价老实说:列的形状和 `expenses` 有重叠(金额/时间/备注/创建时间/状态位),渲染也另
# 走一份。但 G7 的判据是"这几种东西在'要拿它干什么'这件事上真的一样吗"——支出回答
# "钱花在哪儿",这张表回答"钱从哪儿回来的",**连聚合口径都相反**。重叠的是形状不是事实,
# 统一形状省不下任何一处"同一个事实维护两遍"。
#
# `kind` 为什么不再拆成两张表:退款和收入在**存**这件事上完全一样(正数金额、时间、备注、
# 可选指向、状态位),差别只在一个读者(`query_spending` 减不减它)。这才是 G7 说的
# "东西真的一样的时候,统一省的是两处维护同一个事实"。
#
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
    -- M5-20:被删掉的时刻(+ 用户说的理由)。这是这张表**唯一**的状态位——
    -- M5-15 还有第二个(「被谁改写替代了」),M5-26 拆掉了:那份"留痕"没有读者
    -- (真机两天 13 次工具调用,那个"看全部"的开关一次没被调过),而留痕这件事
    -- 起居注已经在干(不可协商第 3 条)。老库怎么退休它见 `_retire_the_voided_column`。
    deleted_at     TEXT,
    deleted_reason TEXT
);
-- occurred_at 是唯一的检索维度:list_recent 按它倒序取前 N,query_spending 按它做范围
-- 扫描。没有索引时两者都要全表扫,而这张表只会越长越长(M4-4 补)。
CREATE INDEX IF NOT EXISTS idx_expenses_occurred_at ON expenses(occurred_at);

-- M6-3「钱回来了」。kind 只有两个值,而它决定的是**聚合口径**:
--   refund   冲抵某一笔支出  → 从「这个月花了多少」里**减掉**
--   income   生活费/兼职/红包 → **根本不进**「花了多少」
-- of_expense_id 可空:那 600 元退的正是那笔 GPT,能指回去才说得清;而退款经常对不上
-- 具体某一笔(几笔合并退),指不上就别硬指。**没有 FOREIGN KEY**:这条连接没开
-- `PRAGMA foreign_keys`(`open_connection` 不开),写上去是个不生效的装饰——
-- 指向合不合法在 `record_income` 里查,而且和插入在同一个事务里查(M5-31)。
-- deleted_at / deleted_reason 照 M5-20 的形状,**不为新表发明第二套**。
CREATE TABLE IF NOT EXISTS income (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    amount_cents   INTEGER NOT NULL,
    kind           TEXT    NOT NULL,   -- refund | income,规范值,进库前已归一
    occurred_at    TEXT    NOT NULL,
    note           TEXT,
    of_expense_id  INTEGER,            -- 仅 refund 有;指 expenses.id
    created_at     TEXT    NOT NULL,
    deleted_at     TEXT,
    deleted_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_income_occurred_at ON income(occurred_at);
"""

# 老库补列:`CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作,不补的症状是
# "新装的机器好使,你自己那台不好使",而且报在运行时不报在启动时(M5-4 的教训)。
_ADDED_COLUMNS = (
    (
        "PRAGMA table_info(expenses)",
        "deleted_at",
        "ALTER TABLE expenses ADD COLUMN deleted_at TEXT",
    ),
    (
        "PRAGMA table_info(expenses)",
        "deleted_reason",
        "ALTER TABLE expenses ADD COLUMN deleted_reason TEXT",
    ),
)

# M5-26 迁移标记。写明是这次迁移标的:哪天用户带 include_deleted=True 翻到这几行,
# 「原因」那一栏得说得清它为什么在那儿,而不是一个没有出处的"已删除"。
_RETIRED_REASON = "M5-26 迁移:这行原先被改写顶掉了,本来就不算在账上"


def _retire_the_voided_column(conn: sqlite3.Connection, tz: ZoneInfo) -> None:
    """老库的退休手续:`voided_by` 那一列在删掉之前,先把靠它藏起来的行标成已删。

    **顺序是死的,反过来就出事**:列一拿掉,`voided_by IS NULL` 这个过滤条件跟着没了,
    那些被改写顶掉的旧行会**重新冒到账上**——真机上是 3 行,账上凭空多三笔,而用户
    不会知道为什么。标成已删之后它们仍然查得到(`include_deleted=True`),但不进任何
    视图、不进任何合计,**和迁移前用户看到的逐字节一样**。

    两条语句包在一个事务里:只标不删是白跑一趟(下次开库再标一次,幂等),
    只删不标就是账上多三笔。这一步跑完列就没了,再开库时探测不到,自然是空操作。
    """
    if "voided_by" not in {row[1] for row in conn.execute("PRAGMA table_info(expenses)")}:
        return
    stamp = datetime.now(tz).replace(tzinfo=None).isoformat(timespec="seconds")
    with transaction(conn):
        conn.execute(
            "UPDATE expenses SET deleted_at = ?, deleted_reason = ?"
            " WHERE voided_by IS NOT NULL AND deleted_at IS NULL",
            (stamp, _RETIRED_REASON),
        )
        conn.execute("ALTER TABLE expenses DROP COLUMN voided_by")


def _connect(root: Path, tz: ZoneInfo) -> sqlite3.Connection:
    """finance 独占自己的库(§5 数据产权):只碰 data_dir/finance/finance.sqlite。"""
    root.mkdir(parents=True, exist_ok=True)
    # M5-8:走 `open_connection` 而不是裸 sqlite3——**bundle 的库面对的是同一个线程池、
    # 同一个洞**。模型一口气报三笔,三个 `record_expense` 并发跑,这条连接照样会烂。
    conn = open_connection(root / "finance.sqlite")
    conn.executescript(_FINANCE_SCHEMA)
    add_missing_columns(conn, _ADDED_COLUMNS)
    # 补列在前、退休在后:老库可能两样都缺,而退休那一步要往 deleted_at 里写。
    _retire_the_voided_column(conn, tz)
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


def _is_bindable_id(value: object) -> bool:
    """模型给的 #id 能不能交给 sqlite 去绑定。

    超出 int64 的 int 在**绑定参数时**抛 `OverflowError`,而它不是 `sqlite3.Error` 的
    子类——`except sqlite3.Error` 接不住,异常直接逃出工具边界,整轮炸掉,用户看到的是
    助手死掉。`record_expense` 的金额上界 `_MAX_CENTS` 防的是同一个东西(M4-2 补),
    而模型给的 id 同样是不可信输入(L3)。

    **一个存不下的 id 在库里必然不存在**,所以这里只回真假:调用方照常走它那句
    「没有 #x 这笔」,不必为此多发明一句话(两套说法必然漂,P1-1)。
    """
    return not isinstance(value, int) or -_MAX_CENTS - 1 <= value <= _MAX_CENTS


def _parse_day(raw: str) -> date | None:
    """只认 YYYY-MM-DD。看不懂返回 None(由调用方给人话提示)。"""
    try:
        return date.fromisoformat(raw.strip())
    except (ValueError, AttributeError):
        return None


def _render_note(note: str | None, label: str = "备注") -> str:
    """把 note 当**不可信文本**渲染,一条都不例外。`label` 只换标签词
    (删除理由和 note 同源,都是模型写的文本),**刀是同一把**。

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
    return f" · {label}{_NOTE_OPEN}{safe[:MAX_NOTE_CHARS]}{_NOTE_CLOSE}"


def _render_reason(reason: str | None) -> str:
    return _render_note(reason, label="原因")


def _group_line(row: sqlite3.Row) -> str:
    return f"- {row['grp']} {_yuan(row['cents'])} 元({row['n']} 笔)"


def _cents(rows: list[sqlite3.Row]) -> int:
    return sum(r["cents"] for r in rows)


def _yuan(cents: int) -> str:
    """分 → 元的显示形态。全程 Decimal,显示层也不让浮点沾边。"""
    return f"{Decimal(cents) / 100:.2f}"


def _income_totals(conn: sqlite3.Connection, lower: str, upper: str) -> dict[str, tuple[int, int]]:
    """一段区间内按 kind 的 (分, 笔数)。**只读**(F4),两个读者共用(见 `_INCOME_SQL`)。

    区间是半开的 `[lower, upper)`,和 `query_spending` / `list_recent` 同一个口径
    ——上界取次日零点,否则带时刻的行会被闭区间吃掉(M4-3 的那条)。
    """
    return {
        row["kind"]: (row["cents"], row["n"])
        for row in conn.execute(_INCOME_SQL["by_kind"], (lower, upper))
    }


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
            # M6-3:**校验照旧挡住负数,但这次指路。** 真机上用户想记那 600 元退款,试了
            # -600、只收到"要一个大于 0 的数字",于是她的下一步是考虑去动那两笔 GPT 的
            # 原始记录(她自己判断不该动,把它挂在话头里挂了三天)。负数支出几乎只有一个
            # 来头——想记的是退款或收入。0 和溢出不指路:那两个不是"方向搞反了"。
            hint = "退款或收入用 record_income 记(记正数),别记成负数的支出。"
            tail = f"这笔没记。{hint}" if cents is not None and cents < 0 else "这笔没记。"
            return f"金额不对({amount}):要一个大于 0 的数字,单位是元(比如 28.5)。{tail}"
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
        而**总额那一行始终是全区间的**。
        区间里有退款(record_income 记的)时末尾多一行,把支出、退款、实际花掉三个数
        一起说清楚——**别再自己减一遍**;收入不在这里,它不算在"花了多少"里。"""
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
            # M6-3:退款按**它自己的日期**落进区间,和它指的那笔支出在哪个月无关。
            # 反过来的话,「9 月花了多少」会被 10 月才到账的退款改写——那个数昨天还是
            # 另一个,而没有任何人会知道为什么。收入一律不取:它不进这个口径。
            refund_cents, refund_n = _income_totals(conn, start.isoformat(), upper).get(
                "refund", (0, 0)
            )
        except sqlite3.Error as exc:  # E2:查不了也要让模型知道,而不是整轮炸掉
            return f"查不了(库读取失败:{exc})。"

        if not groups:
            # 没有支出**但收到过退款**时不许说"没有记录"——那是在说这段时间什么都没发生,
            # 而钱确实动了。没有退款时这一句逐字节不变(M6-3 的硬口径)。
            if refund_n:
                return (
                    f"{since} ~ {until} 没有支出,"
                    f"同期收到退款 {_yuan(refund_cents)} 元({refund_n} 笔)。"
                )
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
        # M6-3 的口径全在这一行里:**三个数一起说**——支出多少、退回来多少、实际花掉多少。
        # 没有退款时一行都不多(支出侧的输出逐字节不变,这是本任务的硬口径)。
        #
        # 为什么不把「合计」那一行改成净额:上面每一组都是支出,合计就是它们的和;改成净额
        # 之后它和下面的分组行对不上,而**"对不上"这件事模型看不出来**,它只会照着念。
        if refund_n:
            net = total - refund_cents
            settled = (
                f"抵掉退款后实际花掉 {_yuan(net)} 元"
                if net >= 0
                # 负数的"花了多少"是一句读不懂的话,而这个区间确实是钱变多了。
                else f"抵掉退款后净收回 {_yuan(-net)} 元"
            )
            lines.append(
                f"同期收到退款 {_yuan(refund_cents)} 元({refund_n} 笔):"
                f"支出 {_yuan(total)} 元,{settled}。"
            )
        return "\n".join(lines)

    def list_recent(
        limit: int = 10,
        since: str | None = None,
        until: str | None = None,
        order: str = "recent",
        include_deleted: bool = False,
    ) -> str:
        """列出流水——全系统唯一返回原始流水的工具,因此硬封顶(上限 20),limit 为负数
        或超大值都钳制到上限。since/until 格式 YYYY-MM-DD、两端都含,缺省为全时段;
        order 取 recent(最近的在前)或 largest(金额从大到小)。回答"某段时间最大的
        一笔"要 order=largest **并且**给上 since/until——只给 order 会答成全时段之最。
        每行开头的 #id 可以直接喂给 amend_expense 或 delete_expense;被删掉的行默认
        不列,include_deleted=True 才带上(标「已删除」)
        ——用户说"删错了恢复一下"时就这么找回那个 #id。"""
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
                conn.execute(_RECENT_SQL[mode], (lower, upper, 1 if include_deleted else 0, n))
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
            # 标注只有一种了(M5-26):被改过的行就是**这一行**,没有"旧版本"这种东西。
            mark = (
                "" if r["deleted_at"] is None else f" 已删除{_render_reason(r['deleted_reason'])}"
            )
            lines.append(
                f"- #{r['id']} {when} {r['category']} {_yuan(r['amount_cents'])} 元"
                f"{_render_note(r['note'])}{mark}"
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
        只传要改的字段,没传的原样保留。**就地改,#id 不变**——改完还是那一行,
        用户记着的那个号接着能用;已经删掉的行改不了(要记就直接记一笔新的)。
        **这个工具只管"改"。要整笔去掉用 delete_expense,别拿改备注的办法假装删掉**
        ——那样金额还在账上照样计入合计,而用户以为已经没了。"""
        try:
            # M5-31:**整个读-改-写在一个事务里**。四个字段的新值全部来自那次 SELECT,
            # 而读和写之间原来是敞开的——两个 amend 打同一行(一条 assistant 消息里的
            # 多个工具调用是**并发**的,M5-8),都读到旧值,后写的把先写的按旧值盖回去,
            # 两边还都报成功。`GuardedConnection` 那把锁挡不住:它保的是「一条语句/一个
            # 事务内部不被打断」,而这个洞在两条独立语句**之间**。锁的粒度就是一个事务,
            # 包进去窗口就没了。
            #
            # **校验也必须在里面**:「判的时候没删、写的时候已删」和「判的时候是 49、
            # 写完是别人的 22」是同一个洞的两张脸,把 SELECT 单独包住不解决任何问题。
            #
            # `immediate=True`:这正是 `transaction()` docstring 说的「读了再改」的临界区
            # ——一上来就拿写锁。同进程里那把可重入锁已经串好了,这一笔是给独立容器形态
            # (`create_server`,连接不止一条)留的:否则读快照之后再写会撞 BUSY_SNAPSHOT。
            with transaction(conn, immediate=True):
                row = (
                    conn.execute(
                        "SELECT id, amount_cents, category, occurred_at, note, deleted_at"
                        " FROM expenses WHERE id = ?",
                        (expense_id,),
                    ).fetchone()
                    if _is_bindable_id(expense_id)
                    else None
                )
                if row is None:
                    return (
                        f"没有 #{expense_id} 这笔。先用 list_recent 看一眼有哪些,#id 在每行开头。"
                    )
                if row["deleted_at"] is not None:
                    # 不指恢复那条路(M5-26):「删了的账要改成别的数」不是撤销,是记一笔新的。
                    # 绕去恢复只会在账本上多两条没意义的痕迹——删了又活、活了又是另一个数。
                    return (
                        f"#{expense_id} 已经删了,改不了——它不在账上,改了你也看不见。"
                        f"要记就直接记一笔新的(record_expense)。"
                    )

                cents = row["amount_cents"] if amount is None else _to_cents(amount)
                if cents is None or cents <= 0 or cents > _MAX_CENTS:
                    # **先校验再动手**:校验没过却已经写了一半,会把一笔好记录改坏。
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

                # 一条 UPDATE 就完了(M5-26):没有"插新行 + 作废旧行"那对必须同生共死的
                # 语句。`id` / `created_at` 不在 SET 里——它们一个字节都不许动。
                conn.execute(
                    "UPDATE expenses SET amount_cents = ?, category = ?, occurred_at = ?,"
                    " note = ? WHERE id = ?",
                    (cents, new_category, stamp, new_note, expense_id),
                )

                before = f"{row['category']} {_yuan(row['amount_cents'])} 元"
                after = f"{new_category} {_yuan(cents)} 元"
                # **说清楚号没变**:用户接下来可能还要拿这个 #id 做别的事(再改一次、
                # 或者删掉),而上一版回的是「改了 #1 → #2」,那个箭头教会模型去记新号。
                return (
                    f"改了 #{expense_id}:{before} → {after}({stamp.replace('T', ' ')[:16]})"
                    f"{_render_note(new_note)}。还是 #{expense_id},号没变。"
                )
        except sqlite3.Error as exc:  # E2:改不动也要让模型知道这步没成
            # 事务整块包进 try:BEGIN 拿不到写锁、COMMIT 失败,都是"这笔没改成",
            # 一样不许抛给模型(抛出去整轮就炸了,用户看到的是助手死掉)。
            return f"这笔没改成(库写入失败:{exc})。"

    def delete_expense(
        expense_id: int,
        reason: str | None = None,
        undo: bool = False,
    ) -> str:
        """删掉一笔记错的账——比如这笔根本不该存在、或者是测试时随手记的。
        expense_id 是 list_recent 每行开头那个 #id。删掉之后正常查询看不见它、
        合计也不算它。**删错了可以撤回**:同一个 id 再调一次、带 undo=True,
        原样回到账上(金额、类目、时间一个字都不变)。
        要改金额或类目用 amend_expense,别先删再重记。"""
        try:
            # M5-31:这里也是「读了再改」,和 amend 同一个洞。判过 `deleted_at` 才动手,
            # 而判和动手之间原来敞着:两个并发的删除都读到"还在账上",都写一遍
            # deleted_at,于是**两边都跟用户说「删了」**、第二次的理由把第一次的盖掉
            # ——上面那句"再删一次不该覆盖第一次的理由"在并发下是空的。和 amend 撞车时
            # 更难看:它报给用户的金额是 amend 改之前那一份,账上却是改之后的。
            with transaction(conn, immediate=True):
                row = (
                    conn.execute(
                        "SELECT id, amount_cents, category, note, deleted_at FROM expenses"
                        " WHERE id = ?",
                        (expense_id,),
                    ).fetchone()
                    if _is_bindable_id(expense_id)
                    else None
                )
                # **所有判断都在动手之前**:M5-15 栽过的是反过来——先写后校验,失败时账已经变了。
                if row is None:
                    return (
                        f"没有 #{expense_id} 这笔,什么都没动。"
                        f"先用 list_recent 看一眼有哪些,#id 在每行开头。"
                    )
                what = f"{row['category']} {_yuan(row['amount_cents'])} 元"

                if undo:
                    if row["deleted_at"] is None:
                        return f"#{expense_id}({what})没被删,现在就在账上,不用恢复。"
                    sql = (
                        "UPDATE expenses SET deleted_at = NULL, deleted_reason = NULL WHERE id = ?"
                    )
                    args: tuple[object, ...] = (expense_id,)
                    done = f"恢复了 #{expense_id}:{what} 又回到账上了。"
                else:
                    if row["deleted_at"] is not None:
                        # 再删一次不该覆盖第一次的理由,更不该让模型以为"这次才生效"。
                        return (
                            f"#{expense_id}({what})已经删过了,账上没有它。要拿回来就带 undo=True。"
                        )
                    sql = "UPDATE expenses SET deleted_at = ?, deleted_reason = ? WHERE id = ?"
                    args = (
                        datetime.now(tz).replace(tzinfo=None).isoformat(timespec="seconds"),
                        reason,
                        expense_id,
                    )
                    # 验收补:**这笔支出身上挂着的退款不会跟着删。** `record_income` 拒绝
                    # 让退款指向一条已删的支出(那条不在任何合计里,"冲抵"它就是冲抵一个
                    # 不存在的数),但那道闸只守了写退款这一个方向——从这边把支出删掉,
                    # 同一个不变量一样破,而且是**无声**破:退款照旧从「花了多少」里减,
                    # `list_income` 照旧印着 `(冲抵 #1)`,而 `#1` 在 `list_recent` 里已经
                    # 找不到了,模型只能自己编一个解释。M5-8 的原话:同一个假设写在两处,
                    # 只守一处等于没守。
                    #
                    # **不拦这次删除**:底稿是用户的,他自己判断该不该删(M5-20 就是真机逼
                    # 出来的)。只把事实说出口——多少钱、还在减——剩下的交给他;而上面那句
                    # undo 恰好也是这件事的解法,所以这一句放在它前面。
                    hit = conn.execute(_INCOME_SQL["against"], (expense_id,)).fetchone()
                    dangling = (
                        f"有 {hit['n']} 笔退款(合计 {_yuan(hit['cents'])} 元)指着这笔,"
                        f"删掉它之后那笔退款还在账上、还在从「花了多少」里减,"
                        f"而对应的支出没有了。"
                        if hit["n"]
                        else ""
                    )
                    done = (
                        f"删了 #{expense_id}:{what}{_render_reason(reason)}。"
                        f"合计里不算它了。{dangling}"
                        f"删错的话再调一次 delete_expense、带 undo=True 就能拿回来。"
                    )

                conn.execute(sql, args)
                return done
        except sqlite3.Error as exc:  # E2:没成也要让模型知道,别回话说办好了
            verb = "恢复" if undo else "删除"
            return f"这笔没{verb}成(库写入失败:{exc})。"

    def record_income(
        amount: float,
        kind: str,
        occurred_at: str | None = None,
        note: str | None = None,
        of_expense_id: int | None = None,
    ) -> str:
        """记一笔**钱回来了**:退款或收入。金额记正数(单位元),方向由 kind 决定——

        kind=refund(退款):冲抵某一笔支出,比如订阅退款、买错了退货。它会从
        「这个月花了多少」里**减掉**;对得上哪一笔就把 of_expense_id 填成那笔的 #id
        (list_recent 每行开头那个),几笔合起来退、对不上具体某一笔就别填。
        kind=income(收入):生活费、奖学金、兼职、红包。它**不进**「花了多少」
        ——记一笔生活费不该让这个月的支出变少。

        occurred_at 缺省用当前时间,「上周三」这类相对时间要先调 current_time 换算成
        YYYY-MM-DD 再传。**负数支出不是退款**:要记退款就用这个工具,
        别去改原始那笔支出——那样底稿就不是原始记录了。"""
        cents = _to_cents(amount)
        if cents is None or cents <= 0 or cents > _MAX_CENTS:
            return (
                f"金额不对({amount}):要一个大于 0 的数字,单位是元(比如 600)。"
                f"退款和收入都记正数,方向由 kind 决定。这笔没记。"
            )
        mode = _KIND.get(kind.strip().lower() if isinstance(kind, str) else "")
        if mode is None:
            # 只列合法值不够:`refund` / `income` 这两个词本身不解释"哪个会减掉花了多少",
            # 而那正是这里唯一要分清的事(E2:让模型自己选对再重试)。
            return (
                f"看不懂 kind「{kind}」:只能是 refund(退款,冲抵某笔支出,会从"
                f"「花了多少」里减掉)或 income(收入,生活费/兼职/红包,不算在"
                f"「花了多少」里)。这笔没记。"
            )
        if mode == "income" and of_expense_id is not None:
            # 不许悄悄接受:收入永远不减支出,而模型会以为自己记了一笔退款,
            # 于是回话说"这个月少花了 600",合计里一分都没减。
            return (
                "收入不冲抵任何支出,of_expense_id 只给 refund 用。这笔没记"
                "——是退款就把 kind 改成 refund,是收入就别传 of_expense_id。"
            )

        if occurred_at is None:
            when = datetime.now(tz).replace(tzinfo=None)
        else:
            parsed = _parse_when(occurred_at, tz)
            if parsed is None:
                # 同 record_expense:不许悄悄退回"现在",那会让账上的日期无声地错。
                return (
                    f"看不懂时间「{occurred_at}」:要 YYYY-MM-DD 或 YYYY-MM-DD HH:MM。"
                    f"相对时间先调 current_time 换算成日期再传。这笔没记。"
                )
            when = parsed

        target = ""
        try:
            # M5-31:**先读后判再写,整块一个事务。** 「那笔支出在不在、是不是已删」是读完
            # 才判的;判和写之间敞着的话,一次 delete_expense 正好落在中间,退款就指向了
            # 一条已经不在账上的支出,而回话说得像办成了。
            # 没传 of_expense_id 的那一支其实是纯插入(没有"读了再改"),但**不给它开特例**:
            # 条件式加锁正是下一个人把 SELECT 挪出事务的那个口子,而代价只是一次拿锁。
            # `immediate=True` 的理由同 amend:同进程那把可重入锁已经串好了,这一笔是给
            # 独立容器形态(`create_server`,连接不止一条)留的——先读快照再写会撞
            # BUSY_SNAPSHOT,那时候用户收到的是一句"库写入失败"。
            with transaction(conn, immediate=True):
                if of_expense_id is not None:
                    row = (
                        conn.execute(
                            "SELECT id, amount_cents, category, deleted_at FROM expenses"
                            " WHERE id = ?",
                            (of_expense_id,),
                        ).fetchone()
                        if _is_bindable_id(of_expense_id)
                        else None
                    )
                    if row is None:
                        return (
                            f"没有 #{of_expense_id} 这笔支出,这笔退款没记。"
                            f"先用 list_recent 看一眼有哪些,#id 在每行开头;"
                            f"要是对不上具体哪一笔,就别传 of_expense_id。"
                        )
                    if row["deleted_at"] is not None:
                        # 已删的那行不在任何合计里,"冲抵"它就是冲抵一个不存在的数,
                        # 而回话会说得像办成了——用户以为这个月少花了 600,账上并没有。
                        return (
                            f"#{of_expense_id} 那笔支出已经删了、不在账上,退款指不过去,"
                            f"这笔没记。要是它对不上具体哪一笔,"
                            f"就别传 of_expense_id 再记一次。"
                        )
                    target = (
                        f",冲抵 #{of_expense_id}({row['category']} {_yuan(row['amount_cents'])} 元)"
                    )
                conn.execute(
                    "INSERT INTO income (amount_cents, kind, occurred_at, note,"
                    " of_expense_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        cents,
                        mode,
                        when.isoformat(timespec="seconds"),
                        note,
                        of_expense_id,
                        datetime.now(tz).replace(tzinfo=None).isoformat(timespec="seconds"),
                    ),
                )
        except sqlite3.Error as exc:  # E2:写不进去也要让模型知道这步没成
            return f"这笔没记进去(库写入失败:{exc})。"

        # 回话里**把口径带上**:模型接下来要用这个数说话,而"减不减进花了多少"是这两种
        # 东西唯一的区别。备注走和 list_income 同一个渲染器(P1-1:两套必然漂)。
        rule = "「花了多少」里会减掉它。" if mode == "refund" else "收入不算在「花了多少」里。"
        return (
            f"记好了:{_KIND_LABEL[mode]} {_yuan(cents)} 元"
            f"({when.strftime('%m-%d %H:%M')}){target}{_render_note(note)}。{rule}"
        )

    def list_income(
        limit: int = 10,
        since: str | None = None,
        until: str | None = None,
    ) -> str:
        """列出收入与退款(支出用 list_recent,两边是两本账)。since/until 格式
        YYYY-MM-DD、两端都含,缺省为全时段;硬封顶 20 条,limit 为负数或超大值都钳制
        到上限。第一行给的是**全区间**的两个合计,各自带着口径:退款会从「花了多少」
        里减掉,收入不算在里面——**这两个数别加在一起**,它们回答的不是同一个问题。"""
        # 负数在 SQLite 的 LIMIT 里是"不限制",不钳制就是全表倒进上下文(M3-1 教训)。
        # 上限和 list_recent 共用一个:两个工具都返回原始记录,顶穿 L0 的方式一模一样。
        n = MAX_RECENT_ROWS if limit < 1 else min(limit, MAX_RECENT_ROWS)

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
            # 上界取次日零点、开区间,理由同 query_spending(闭区间会吃掉末日带时刻的行)
            upper = (end + timedelta(days=1)).isoformat()

        try:
            totals = _income_totals(conn, lower, upper)
            rows = list(conn.execute(_INCOME_SQL["list"], (lower, upper, n)))
        except sqlite3.Error as exc:  # E2:查不了也要让模型知道,而不是整轮炸掉
            return f"查不了(库读取失败:{exc})。"

        scoped = since is not None or until is not None
        scope = f"{since or '开头'} ~ {until or '现在'} " if scoped else ""
        if not rows:
            # "这段没有" ≠ "一笔都没记过"(照 list_recent 的那条:混成一句会让模型
            # 以为这本账是空的)。
            return f"{scope}没有收入也没有退款。" if scoped else "还没有记过收入或退款。"

        income_cents, income_n = totals.get("income", (0, 0))
        refund_cents, refund_n = totals.get("refund", (0, 0))
        # 顺序写死(收入在前),不跟着 SQL 的排序抖:同一份数据每次读起来要一样。
        parts = []
        if income_n:
            parts.append(f"收入 {_yuan(income_cents)} 元({income_n} 笔)")
        if refund_n:
            parts.append(f"退款 {_yuan(refund_cents)} 元({refund_n} 笔)")
        # **两个数各自带口径**。少了这半句,「3000」这个数就没有单位——而月度复盘里
        # 「花了多少」和「进了多少」是两个数、两套算法,说错一个整段复盘就是错的。
        if income_n and refund_n:
            rule = "退款从「花了多少」里减掉,收入不算在里面。"
        elif refund_n:
            rule = "退款从「花了多少」里减掉。"
        else:
            rule = "收入不算在「花了多少」里。"

        lines = [f"{scope}{','.join(parts)}。{rule}"]
        for r in rows:
            when = r["occurred_at"].replace("T", " ")[:16]
            against = "" if r["of_expense_id"] is None else f"(冲抵 #{r['of_expense_id']})"
            # kind 取不到标签就原样显示:KeyError 会逃出工具边界,而这里没有任何东西
            # 值得为它炸掉一整轮(E2)。
            label = _KIND_LABEL.get(r["kind"], r["kind"])
            lines.append(
                f"- {when} {label} {_yuan(r['amount_cents'])} 元{against}{_render_note(r['note'])}"
            )
        # 合计那一行是**全区间**的,和列了几行无关;所以截断必须说出口——静默截断读起来
        # 和"就这些"一模一样,模型会拿残缺的流水去解释一个全区间的合计。
        hidden = income_n + refund_n - len(rows)
        if hidden:
            lines.append(f"(还有 {hidden} 笔更早的没列出来,上面的合计是全区间的)")
        return "\n".join(lines)

    # 顺序即冻结顺序(前缀第 0 层):**只追加在末尾**,不许插队。
    return [
        record_expense,
        query_spending,
        list_recent,
        amend_expense,
        delete_expense,
        # M6-3:「钱回来了」这一侧。同样只追加在末尾,前五位一个没动。
        record_income,
        list_income,
    ]


def build(data_dir: Path, *, timezone: str) -> BundleRuntime:
    """统一构造入口(bundle 契约),至少含 tools: list[Callable]。工具顺序由
    manifest.yaml 与测试钉死(前缀第0层)。

    timezone 由组装根注入而不是在这里给默认值:默认值会和 `Settings.timezone` 各走各的,
    用户改了配置、账本却还按老时区记——那正是 M1 Task 9 修过的那个 8 小时时差。
    """
    tz = ZoneInfo(timezone)
    conn = _connect(Path(data_dir) / "finance", tz)
    return BundleRuntime(tools=_tool_functions(conn, tz))


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
