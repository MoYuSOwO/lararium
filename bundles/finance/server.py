"""finance bundle —— 记账与消费分析(对话侧)。

M4-1 立的骨架:manifest + 独占 SQLite + 统一构造入口 `build(...)`。三个工具的
**签名与文档在 M4-1 定死**(工具 schema 是前缀第0层,顺序冻结后不许再动);
M4-2 起只换函数体、不动签名与 docstring——docstring 就是 schema,改它是一次前缀重建。

M4-2 落地 `finance__record_expense`,M4-3 落地 `finance__query_spending`,M4-4 落地 `finance__list_recent`。
M5-15 / M5-20 追加 `finance__amend_expense` / `finance__delete_expense`(两个都是真机逼出来的,不是设计
出来的);M6-3 追加 `finance__record_income` / `finance__list_income`——账本终于不只有"花出去"一个口子。
M6-3a 把 M6-3 的一半拆掉:**退款并进收入**,`kind` / `of_expense_id` 两列退休。
M6-4 追加 `finance__set_budget` / `finance__list_budgets` / `finance__remove_budget`,以及**超线那句话**——它不新开
一条消息,是拼在写操作的回话尾巴上(新开一条就要走出件箱,那是主动推送的形状)。
用户的判断比 M6-3 里写的任何一条都硬:「很难说全退」——真实退款经常是部分退、几笔合并退、
退到代金券,「冲抵某一笔支出」这个指针大多数时候指不准;而"抵掉退款后实际花掉 Z"整个
建在那个指针上,**前提不成立,派生出来的数就比没有更坏**:一个读起来很确定的数字,
底下是个猜的对应关系。

**S2:这个文件过了 1000 行(M6-4 之前 927),远超那条 300 行的审查线,理由重新登记。**
预算是这个文件里的第三摊东西了(支出 / 钱回来了 / 预算),所以 M6-4 把「该不该拆」
重新论了一遍,结论还是**不拆**,但理由和 M6-3 那版不是同一条:

- 按**表**切(expenses | income | budgets)切不动:三份共享同一条连接、同一套渲染器
  (`_render_note` 两个出口渲染不一致就是 P1-1 那个事故)、同一份金额与时间解析,
  拆了就是三份渲染器三份解析器——**而 M6-4 恰好加重了这一条**:「已花」必须和
  `finance__query_spending` 印的数一模一样,保证它的办法是**只有一份 SQL**(`_GROUP_SQL`),
  拆出去就变成一次跨模块调用去借那条查询,借的时候借错一个参数就是两个数对不上。
- 按**层**切(SQL 一个模块、工具一个模块)是可以切的,但那条缝把「一次查询」变成
  「一次跨模块调用」,而本文件的价值恰恰在于每条 SQL 旁边写着它防的那次事故。

真正该拆的那天仍然是"某一侧长出自己的状态机"——预算离那个门槛还差得远:它没有状态位、
没有历史、没有审批,三个工具加起来一百行出头。注释占比高是刻意的:这个文件每一处防的
都是一次真机事故。
"""

import re
import sqlite3
from collections.abc import Callable, Iterable
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

# M6-4 预算那条线的第二种 scope:整个月的总额。它和类目摆在同一列里(`budgets.scope`),
# 那一列的含义就是"哪条线"——两种线各自只有一个数,不是两张表。
TOTAL_SCOPE = "总额"
# 预算能设在哪几条线上,**顺序即回话里的顺序**:类目按 CATEGORIES,总额垫底。
# 跟着插入顺序或 SQL 默认顺序走的话,同一份数据每次读回来的次序会抖,而这份输出
# 会以 tool_result 的身份进上下文。
BUDGET_SCOPES = (*CATEGORIES, TOTAL_SCOPE)

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

# 收入那张表的两条查询。`deleted_at IS NULL` 从第一天就带上,理由和支出侧一样
# (逐条写死、由 `test_every_income_query_filters_deleted_rows` 保证一条不漏)。
#
# M6-3a 之后**只剩一个读者**(`list_income`):退款并进收入之后,「花了多少」只回答支出,
# `query_spending` 不再读这张表。两条 SQL 的 WHERE 逐字相同——一条要聚合、一条要流水,
# 这是无法合并的两件事,所以钉的是"两条都带那个条件"(上面那条参数化测试)。
_INCOME_SQL = {
    "list": (
        "SELECT occurred_at, amount_cents, note FROM income"
        " WHERE occurred_at >= ? AND occurred_at < ? AND deleted_at IS NULL"
        " ORDER BY occurred_at DESC, id DESC LIMIT ?"
    ),
    "total": (
        "SELECT SUM(amount_cents) AS cents, COUNT(*) AS n FROM income"
        " WHERE occurred_at >= ? AND occurred_at < ? AND deleted_at IS NULL"
    ),
}

# 预算那三条 SQL(逐条写死,同 `_GROUP_SQL` 的理由)。
#
# `set` 是 upsert:一条线一个 scope,再设一次是改额度。
# `remove` 用 `DELETE … RETURNING`:**这样就不需要"先读再删"那个临界区**——回话要说出
# "原来是多少",而先 SELECT 再 DELETE 的话,两次并发的撤销会都报「撤了(原来是 500)」
# (M5-31 那个洞的形状)。一条语句拿到被删掉的那一行,窗口压根不存在。
# (RETURNING 要 SQLite ≥ 3.35,而全局约束已经要求 3.35 —— M5-26 的 DROP COLUMN 就是那条。)
_BUDGET_SQL = {
    "all": "SELECT scope, limit_cents FROM budgets",
    "set": (
        "INSERT INTO budgets (scope, limit_cents, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(scope) DO UPDATE SET limit_cents = excluded.limit_cents,"
        " updated_at = excluded.updated_at"
    ),
    "remove": "DELETE FROM budgets WHERE scope = ? RETURNING limit_cents",
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

# ── M6-3:为什么收入是**新一张表**,不是给 `expenses` 加一个符号位
#
# 1. **符号位撑不住 `category` 那一列。** 它是固定 7 项、NOT NULL,存在的唯一理由就是
#    `_GROUP_SQL` 的 GROUP BY(M4-3 是照着这个小集合设计的)。一笔 3000 的生活费没有
#    类目:要么往 CATEGORIES 里塞个"收入"——那 7 项立刻不再是"钱花在哪儿"的划分,按
#    类目的复盘当场失真;要么硬编成"其他"——于是「其他」这一组变成支出减收入,是一个
#    读不懂的数。**这不是迁移成本,是正确性成本**,而在符号位方案里它无路可走。
# 2. **「这行算不算在账上」会重新变成两处要问的事。** M5-26 刚把 `voided_by` 那个第二
#    状态位拆掉,理由正是那一问要同时问两处、而第二处没有读者。符号位方案里每条查询的
#    `deleted_at IS NULL` 后面都得再跟一个"这行是支出吗",而本文件的 SQL 全是
#    逐条写死的字面量(拼接会被 S608 盯上,而它是对的):`_GROUP_SQL` 两条 +
#    `_RECENT_WHERE` 一处(两条列表查询共用)+ `amend` 与 `delete` 各一处 SELECT =
#    **5 处都要多一个条件**。漏一处的症状是"收入被算成支出"或"收入行能被 amend 当支出
#    改",静默、且错在钱上。刚还完的债不该立刻再欠一笔。
# 3. **底稿不许被动。** 用户自己拒绝过"去动那两笔 GPT 的账",理由是「那样底稿就不是原始
#    记录了」。`list_recent` 是全系统唯一返回原始流水的工具;分表之后它**物理上看不见**
#    收入行,「逐字节不变」不依赖任何一个过滤条件成立。
# 4. **两个问题分开问,两条查询都简单。** 「花了多少」= 支出合计;「进了多少」= 同期收入。
#    各自一条一行 WHERE 的查询。符号位方案要在一条 SQL 里用 CASE 把"哪一类算进哪个数"
#    写进表达式——那条规则就藏在 SQL 里,没有名字,也没人能对它下断言。
#
# 代价老实说:列的形状和 `expenses` 有重叠(金额/时间/备注/创建时间/状态位),渲染也另
# 走一份。但 G7 的判据是"这几种东西在'要拿它干什么'这件事上真的一样吗"——支出回答
# "钱花在哪儿",这张表回答"钱从哪儿回来的",**两个数压根不进同一个合计**。重叠的是形状
# 不是事实,统一形状省不下任何一处"同一个事实维护两遍"。
#
# M6-3a 把 `kind` 也拆了,而那是**同一条判据的另一半**:退款和收入在存这件事上完全一样,
# M6-3 保留的那个差别(减不减「花了多少」)本身就不该存在——东西真的一样的时候,连那个
# `kind` 都是多的。一张表、一个口径、一句话说得清:收入不算在「花了多少」里。
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
-- occurred_at 是唯一的检索维度:finance__list_recent 按它倒序取前 N,finance__query_spending 按它做范围
-- 扫描。没有索引时两者都要全表扫,而这张表只会越长越长(M4-4 补)。
CREATE INDEX IF NOT EXISTS idx_expenses_occurred_at ON expenses(occurred_at);

-- 「钱回来了」:生活费、兼职、红包,**以及退款到账**。一个口径,一句话说得清——
-- 收入不算在「花了多少」里。M6-3 这张表原先还有两列:`kind`(refund | income)决定
-- 减不减「花了多少」,`of_expense_id` 指着被冲抵的那笔支出。**M6-3a 两列一起退休**:
-- 「冲抵某一笔支出」假设退款是全额的,而真实退款经常部分退、几笔合并退、退到代金券,
-- 那个指针大多数时候指不准;建在它上面的"抵掉退款后实际花掉 Z"因此比没有更坏。
-- 老库怎么退休这两列见 `_retire_the_refund_columns`。
-- deleted_at / deleted_reason 照 M5-20 的形状,**不为这张表发明第二套**。
CREATE TABLE IF NOT EXISTS income (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    amount_cents   INTEGER NOT NULL,
    occurred_at    TEXT    NOT NULL,
    note           TEXT,
    created_at     TEXT    NOT NULL,
    deleted_at     TEXT,
    deleted_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_income_occurred_at ON income(occurred_at);

-- M6-4 预算:**一条线 = 一个 scope + 一个月额度**,`scope` 做主键——同一条线再设一次
-- 就是盖掉那一条,不然"哪一条算"没有答案。
-- **额度不带月份**:用户说的是「餐饮每月 500」,一条线是每个月的额度。按 (年月, scope)
-- 存就得回答"没设那个月怎么办"(继承上个月?那就是第二套规则),而那个问题没人问过。
-- **没有 deleted_at**:这张表是设置,不是账。撤掉一条预算就是真删——流水那边留痕是因为
-- 用户记着 #id 要撤回,而"我上个月设过 500 元"没有任何读者(G6:留着的每一样都在收利息)。
-- 没有索引:最多 8 行,主键够了。
CREATE TABLE IF NOT EXISTS budgets (
    scope       TEXT PRIMARY KEY,
    limit_cents INTEGER NOT NULL,
    updated_at  TEXT    NOT NULL
);
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


# M6-3a 要退休的两列。**逐条写死不拼列名**:拼进 DDL 会被 S608 盯上(而它是对的),
# 而这里本来就只有两列,写死连"可能"都没有(同 `_ADDED_COLUMNS` 的理由)。
_RETIRED_INCOME_COLUMNS = (
    ("kind", "ALTER TABLE income DROP COLUMN kind"),
    ("of_expense_id", "ALTER TABLE income DROP COLUMN of_expense_id"),
)


def _retire_the_refund_columns(conn: sqlite3.Connection) -> None:
    """老库的退休手续:`income` 表上 `kind` / `of_expense_id` 两列拿掉(M6-3a)。

    `CREATE TABLE IF NOT EXISTS` 对**已经建出来的**表是空操作,而这张表 M6-3 当天就推上
    真机了(0 行)。不办这道手续的症状不是"多两列没人看":`kind TEXT NOT NULL` 还在,
    而新的 `finance__record_income` 不写它——于是**每一笔收入都撞 NOT NULL**,用户收到的是一句
    「这笔没记进去(库写入失败……)」,那台机器从此记不了收入。

    **有行也是对的,不重建表、不搬数据**:一条 refund 行拿掉这两列就是一条收入行,
    而那正是新口径要的意思(退款并进收入),不是凑合。金额、时间、备注、`id`、`deleted_at`
    一个字节都不动。

    **这次没有顺序要求**——这一句是想清楚之后的结论,不是没想。`voided_by` 那次顺序是死的,
    因为**列一拿掉,靠它把行藏起来的那个过滤条件就跟着没了**,所以必须先把那些行标成已删:
    那是一次"先改数据、再改结构"。这次一行数据都不改,两个 DROP 也互不相干,断在中间
    (一列掉了、一列还在)对任何一次读写都没影响——新代码两列都不读,而 `of_expense_id`
    本来可空。**位置**倒是有要求:必须排在 `executescript` 之后(表得先在,不然探测不到、
    静默跳过),且排在任何一次工具调用之前(否则就是上面那句 NOT NULL)。两条 DDL 仍然
    包在一个事务里:不是因为中间态会出事,而是让下一次开库只有两种状态要想,不是三种。
    跑完这两列就没了,再开库时探测不到,自然是空操作。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(income)")}
    retiring = [ddl for column, ddl in _RETIRED_INCOME_COLUMNS if column in columns]
    if not retiring:
        return
    with transaction(conn):
        for ddl in retiring:
            conn.execute(ddl)


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
    # M6-3a:income 表那两列的退休手续。和上面那步互不相干(两张表、两次迁移),
    # 但同样必须排在 executescript 之后——表得先在,不然探测不到、静默跳过。
    _retire_the_refund_columns(conn)
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
    助手死掉。`finance__record_expense` 的金额上界 `_MAX_CENTS` 防的是同一个东西(M4-2 补),
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


def _month_bounds(when: date) -> tuple[str, str, str]:
    """那一天所在的**月**:("2026-09", "2026-09-01", "2026-10-01")。

    上界取次月 1 号、开区间,理由同 `finance__query_spending`:存的是 'YYYY-MM-DDTHH:MM:SS',
    闭区间会把月末带时刻的流水吃掉。`+31 天再 replace(day=1)` 对 28~31 天的月份都落在
    次月(2 月 1 号 + 31 天 = 3 月 4 号 → 3 月 1 号),不用算每个月有几天。
    """
    first = when.replace(day=1)
    nxt = (first + timedelta(days=31)).replace(day=1)
    return f"{first:%Y-%m}", first.isoformat(), nxt.isoformat()


def _budgets(conn: sqlite3.Connection) -> dict[str, int]:
    """设过的每条线:scope → 月额度(分)。最多 8 行,一次全取回来。"""
    return {row["scope"]: row["limit_cents"] for row in conn.execute(_BUDGET_SQL["all"])}


def _spent_by_category(conn: sqlite3.Connection, lower: str, upper: str) -> dict[str, int]:
    """一个月里每个类目的支出合计。

    **跑的是 `finance__query_spending` 那条 SQL(`_GROUP_SQL["category"]`),只此一份。** 「已花」和
    印给用户的那个数必须是同一个数,而保证它的办法不是"两份写得一样"——是**只有一份**:
    第二份迟早漏掉 `deleted_at IS NULL` 或者那个区间,而症状是两个数对不上、谁都说不清
    哪个对。总额那条线 = 这些值加起来,和 `finance__query_spending` 的合计行是同一句 Python
    (`sum(r["cents"] for r in groups)`)。
    """
    return {
        row["grp"]: row["cents"] for row in conn.execute(_GROUP_SQL["category"], (lower, upper))
    }


def _budget_line(month: str, scope: str, spent: int, limit_cents: int) -> str:
    """一条线的现状:已花 / 额度 /(超了多少)。**超线那句和 `finance__list_budgets` 是同一句**
    ——两套渲染器必然漂(P1-1),而这一句里三个数的口径全在措辞上。

    **不许说"比上月多"**:账本 2026-09-06 才开张,8 月没有数,环比是编的(用户自己
    说过"8 月没数据,环比做不了")。
    **月份写明白(`2026-09`)而不是"本月"**:10 月 2 号补记一笔 9 月 28 号的账,吃的是
    9 月的额度——那一刻"本月"就是一句假话。
    **正好花完不是超了**:`over > 0` 才有那半句,没有"超了 0.00 元"这种话。
    """
    over = spent - limit_cents
    tail = f",超了 {_yuan(over)} 元" if over > 0 else ""
    return f"{month} {scope}已花 {_yuan(spent)} 元,额度 {_yuan(limit_cents)} 元{tail}。"


def _budget_lines(
    conn: sqlite3.Connection, *, when: date, scopes: Iterable[str], over_only: bool
) -> list[str]:
    """`scopes` 里设过预算的那几条线,按给定顺序渲染成人话;`over_only` 只留超了的。

    **不设就没有提醒**:一条都没设时连那次聚合都不查——没有默认额度,也就没有默认成本。
    可能抛 `sqlite3.Error`,由调用方按 E2 处理(写路径上走 `_budget_note`)。
    """
    limits = _budgets(conn)
    if not limits:
        return []
    month, lower, upper = _month_bounds(when)
    spent = _spent_by_category(conn, lower, upper)
    lines = []
    for scope in scopes:
        limit_cents = limits.get(scope)
        if limit_cents is None:
            continue
        got = sum(spent.values()) if scope == TOTAL_SCOPE else spent.get(scope, 0)
        if over_only and got <= limit_cents:
            continue
        lines.append(_budget_line(month, scope, got, limit_cents))
    return lines


def _budget_note(conn: sqlite3.Connection, *, when: date | None, category: str) -> list[str]:
    """写操作回话的尾巴:这一笔落进的那两条线(它的类目、总额)超了就说。

    **四条写路径共用这一份**(`finance__record_expense` / `finance__amend_expense` / `finance__delete_expense`,
    外加 `finance__set_budget` 自己划线那一次走 `_budget_lines`)。G8:一条不变量有几条路能破它,
    就要在几处守——而把 50 改成 5000、把删掉的那笔 undo 回来,都能让"超了"成立,
    **而那两条路原来是无声的**。守的方式不是拦(底稿是用户的),是把事实说出口。

    **顺序写死:类目先、总额后**,不跟着 `_GROUP_SQL` 的金额降序抖。类目那条更具体
    ——"哪一类吃掉了额度"才是下一步能动手的信息。

    **超线就说,不只是跨过线的那一笔**:「已经超了」这件事在这个月后面每一笔上都成立,
    只在跨线那一笔说等于让后面每一笔装作没事;而判"是不是这一笔跨的"要多查一次
    "这一笔之前的合计",多花一次查询换来的是更少的信息。

    E2:**这里绝不许抛**——上面那一步(记/改/删)已经成了,异常逃出工具边界那一轮就炸,
    用户看到的是助手死掉、而账上那笔明明写进去了。查不成也不许闷掉:闷掉是最坏的,
    用户会以为自己还在线下(G8 那句"不说不是选项")。
    """
    if when is None:
        # `occurred_at` 认不出来(只可能是老库里的脏数据):不知道该算哪个月,所以不说。
        # 编一个月份出来比不说更坏——那会是一个读起来很确定、底下是猜的数。
        return []
    try:
        return _budget_lines(conn, when=when, scopes=(category, TOTAL_SCOPE), over_only=True)
    except sqlite3.Error as exc:
        return [f"(预算这次没查成:{exc};上面那一步是做成了的)"]


def _no_such_scope(scope: str) -> str:
    """预算的 scope 走白名单(L3:模型给的东西是不可信输入),提示里列全合法值——
    照 `finance__record_expense` 那条非法类目的写法,模型才能自己纠正重试而不是吃一次空转。"""
    legal = "|".join(BUDGET_SCOPES)
    return f"没有「{scope}」这条线。预算只能设在:{legal}。"


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
            # 来头——想记的是钱回来了。0 和溢出不指路:那两个不是"方向搞反了"。
            #
            # M6-3a 改了这半句的措辞:原话是「退款或收入用 record_income 记」,而"退款"
            # 已经不是一个单独的东西了(它就记成收入)。**这是回话不是 docstring,
            # 不进前缀**——改它不触发一次前缀重建。
            hint = "钱回来了(收到的退款也算)用 finance__record_income 记成收入,别记成负数的支出。"
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
        head = (
            f"记好了:{category} {_yuan(cents)} 元"
            f"({when.strftime('%m-%d %H:%M')}){_render_note(note)}。"
        )
        # M6-4:超线那句**拼在这句回话里**,不新开一条消息(新开就要走出件箱,那是主动
        # 推送的形状)。没设预算、或者没超,`_budget_note` 回空列表,而 `"\n".join([head])`
        # 就是 head 本身——**这句话逐字节不变**,M4-2 起冻在 test_finance_record.py 的那些
        # 断言一条都不用动。月份按**这一笔的 `occurred_at`** 算,不是"现在"。
        return "\n".join([head, *_budget_note(conn, when=when.date(), category=category)])

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
        include_deleted: bool = False,
    ) -> str:
        """列出流水——全系统唯一返回原始流水的工具,因此硬封顶(上限 20),limit 为负数
        或超大值都钳制到上限。since/until 格式 YYYY-MM-DD、两端都含,缺省为全时段;
        order 取 recent(最近的在前)或 largest(金额从大到小)。回答"某段时间最大的
        一笔"要 order=largest **并且**给上 since/until——只给 order 会答成全时段之最。
        每行开头的 #id 可以直接喂给 finance__amend_expense 或 finance__delete_expense;被删掉的行默认
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
        """改一笔已经记错的流水。expense_id 是 finance__list_recent 每行开头那个 #id;
        只传要改的字段,没传的原样保留。**就地改,#id 不变**——改完还是那一行,
        用户记着的那个号接着能用;已经删掉的行改不了(要记就直接记一笔新的)。
        **这个工具只管"改"。要整笔去掉用 finance__delete_expense,别拿改备注的办法假装删掉**
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
                    return f"没有 #{expense_id} 这笔。先用 finance__list_recent 看一眼有哪些,#id 在每行开头。"
                if row["deleted_at"] is not None:
                    # 不指恢复那条路(M5-26):「删了的账要改成别的数」不是撤销,是记一笔新的。
                    # 绕去恢复只会在账本上多两条没意义的痕迹——删了又活、活了又是另一个数。
                    return (
                        f"#{expense_id} 已经删了,改不了——它不在账上,改了你也看不见。"
                        f"要记就直接记一笔新的(finance__record_expense)。"
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
                done = (
                    f"改了 #{expense_id}:{before} → {after}({stamp.replace('T', ' ')[:16]})"
                    f"{_render_note(new_note)}。还是 #{expense_id},号没变。"
                )
                # M6-4 / G8:**把 50 改成 5000 同样能把你推过线,而这条路原来是无声的。**
                # 检查是和 record_expense 共用的那一个函数(不是复制一份),按**改完之后**
                # 这一笔落在哪条线上算——改类目、改月份都可能把它搬到另一条线上,而它此刻
                # 吃的是新那条的额度。查询在事务里:读到的就是刚写进去的那一版。
                return "\n".join(
                    [done, *_budget_note(conn, when=_parse_day(stamp[:10]), category=new_category)]
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
        expense_id 是 finance__list_recent 每行开头那个 #id。删掉之后正常查询看不见它、
        合计也不算它。**删错了可以撤回**:同一个 id 再调一次、带 undo=True,
        原样回到账上(金额、类目、时间一个字都不变)。
        要改金额或类目用 finance__amend_expense,别先删再重记。"""
        try:
            # M5-31:这里也是「读了再改」,和 amend 同一个洞。判过 `deleted_at` 才动手,
            # 而判和动手之间原来敞着:两个并发的删除都读到"还在账上",都写一遍
            # deleted_at,于是**两边都跟用户说「删了」**、第二次的理由把第一次的盖掉
            # ——上面那句"再删一次不该覆盖第一次的理由"在并发下是空的。和 amend 撞车时
            # 更难看:它报给用户的金额是 amend 改之前那一份,账上却是改之后的。
            with transaction(conn, immediate=True):
                row = (
                    conn.execute(
                        # M6-4 多取了 `occurred_at`:预算按**那一笔所在的月**算,
                        # 而删/恢复都会改变那个月的合计(见下面那段)。
                        "SELECT id, amount_cents, category, note, deleted_at, occurred_at"
                        " FROM expenses WHERE id = ?",
                        (expense_id,),
                    ).fetchone()
                    if _is_bindable_id(expense_id)
                    else None
                )
                # **所有判断都在动手之前**:M5-15 栽过的是反过来——先写后校验,失败时账已经变了。
                if row is None:
                    return (
                        f"没有 #{expense_id} 这笔,什么都没动。"
                        f"先用 finance__list_recent 看一眼有哪些,#id 在每行开头。"
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
                    # M6-3 验收时这里追加过一句「有 N 笔退款指着这笔、它还在从「花了多少」
                    # 里减」。**M6-3a 把它删掉了**:那句话报告的事实不存在了——没有指针就
                    # 没有悬空的引用,收入也不从「花了多少」里减。留着它就是 G6 说的
                    # "修一个不该存在的东西",而这一句回到 M5-20 的原文、逐字节不变。
                    done = (
                        f"删了 #{expense_id}:{what}{_render_reason(reason)}。"
                        f"合计里不算它了。删错的话再调一次 finance__delete_expense、带 undo=True 就能拿回来。"
                    )

                conn.execute(sql, args)
                # M6-4 / G8:`undo=True` 把那笔放回账上,合计跟着回去——**第三条无声的路**。
                # 删除那一支也照说:「已经超了」是关于状态的,删完还超着而一声不吭,
                # 和"只在跨线那一笔说"是同一种装作没事。删到线下了自然就不说了(同一个判据)。
                return "\n".join(
                    [
                        done,
                        *_budget_note(
                            conn,
                            when=_parse_day(row["occurred_at"][:10]),
                            category=row["category"],
                        ),
                    ]
                )
        except sqlite3.Error as exc:  # E2:没成也要让模型知道,别回话说办好了
            verb = "恢复" if undo else "删除"
            return f"这笔没{verb}成(库写入失败:{exc})。"

    def record_income(
        amount: float,
        occurred_at: str | None = None,
        note: str | None = None,
    ) -> str:
        """记一笔收入:生活费、奖学金、兼职、红包,**收到的退款也记这里**。
        金额记正数(单位元)。**收入不算在「花了多少」里**——记一笔生活费不该让这个月的
        支出变少;一笔退款同样不去冲抵原来那笔支出(那笔钱当初确实花了,底稿不动),
        它就是一笔钱回来了。occurred_at 缺省用当前时间,「上周三」这类相对时间要先调
        current_time 换算成 YYYY-MM-DD 再传。**别拿一笔负数支出当收入记**,记在这儿。"""
        cents = _to_cents(amount)
        if cents is None or cents <= 0 or cents > _MAX_CENTS:
            return (
                f"金额不对({amount}):要一个大于 0 的数字,单位是元(比如 600)。收入记正数,这笔没记。"
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

        try:
            # **一条裸 INSERT,没有事务**(M6-3a)。M6-3 这里包着 `transaction(immediate=True)`,
            # 而它存在的理由是 `of_expense_id` 那条"读了再判再写":先 SELECT 那笔支出、判它
            # 在不在账上、再插入。那个临界区随指针一起没了,现在没有任何一个写进去的值来自
            # 上一次读——`isolation_level=None` 下单条 INSERT 本来就是它自己的事务。
            # 而 M5-31 那两处不能跟着去掉:`amend` 写的四个字段全部来自那次 SELECT、
            # `delete` 判过 `deleted_at` 才动手,两条独立语句之间的窗口就是丢更新和
            # "两边都报删了"的所在;`immediate=True` 还防着独立容器形态下先读快照再写撞
            # BUSY_SNAPSHOT——而纯插入压根不先读快照。
            # 留着它不是"多一道保险",是**说了一句不成立的话**:下一个人读到 `transaction`
            # 会去找那个临界区在哪。顺带,隔壁 `record_expense` 的插入本来就是裸的
            # ——同一件事两种写法,迟早有人问哪个才对(P1-1 的形状)。
            conn.execute(
                "INSERT INTO income (amount_cents, occurred_at, note, created_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    cents,
                    when.isoformat(timespec="seconds"),
                    note,
                    datetime.now(tz).replace(tzinfo=None).isoformat(timespec="seconds"),
                ),
            )
        except sqlite3.Error as exc:  # E2:写不进去也要让模型知道这步没成
            return f"这笔没记进去(库写入失败:{exc})。"

        # 回话里**把口径带上**:模型接下来要用这个数说话,而"不减进花了多少"是这个数
        # 唯一容易被搞错的地方。备注走和 list_income 同一个渲染器(P1-1:两套必然漂)。
        return (
            f"记好了:收入 {_yuan(cents)} 元({when.strftime('%m-%d %H:%M')})"
            f"{_render_note(note)}。收入不算在「花了多少」里。"
        )

    def list_income(
        limit: int = 10,
        since: str | None = None,
        until: str | None = None,
    ) -> str:
        """列出收入(支出用 finance__list_recent,两边是两本账)。since/until 格式 YYYY-MM-DD、
        两端都含,缺省为全时段;硬封顶 20 条,limit 为负数或超大值都钳制到上限。
        第一行给的是**全区间**的收入合计和笔数,后面才是流水。
        **收入不算在「花了多少」里**:这个数和 finance__query_spending 那个数不许加减到一起,
        它们回答的不是同一个问题。"""
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
            total = conn.execute(_INCOME_SQL["total"], (lower, upper)).fetchone()
            rows = list(conn.execute(_INCOME_SQL["list"], (lower, upper, n)))
        except sqlite3.Error as exc:  # E2:查不了也要让模型知道,而不是整轮炸掉
            return f"查不了(库读取失败:{exc})。"

        scoped = since is not None or until is not None
        scope = f"{since or '开头'} ~ {until or '现在'} " if scoped else ""
        if not rows:
            # "这段没有" ≠ "一笔都没记过"(照 list_recent 的那条:混成一句会让模型
            # 以为这本账是空的)。
            return f"{scope}没有收入。" if scoped else "还没有记过收入。"

        # 一行都没有时 SUM 回的是 NULL,不是 0——但那一支上面已经返回了,这里只防
        # "以后有人把两条查询的区间改得不一样"(那时 NULL 会一路变成 TypeError)。
        cents, count = total["cents"] or 0, total["n"]
        # **口径那半句必须在**:少了它,「3000」这个数就没有单位——而月度复盘里
        # 「花了多少」和「进了多少」是两个数、两套算法,说错一个整段复盘就是错的。
        # M6-3a 之后这半句是 `list_income` 仅存的两个存在理由之一(另一个是那个合计)。
        lines = [f"{scope}收入 {_yuan(cents)} 元({count} 笔)。收入不算在「花了多少」里。"]
        for r in rows:
            when = r["occurred_at"].replace("T", " ")[:16]
            # 每行都带上「收入」两个字:一条流水会以 tool_result 的身份被 search_history
            # 捞回去,而"3000.00 元"单独摆着和一笔支出长得一模一样。两个字买一个不含糊。
            lines.append(f"- {when} 收入 {_yuan(r['amount_cents'])} 元{_render_note(r['note'])}")
        # 合计那一行是**全区间**的,和列了几行无关;所以截断必须说出口——静默截断读起来
        # 和"就这些"一模一样,模型会拿残缺的流水去解释一个全区间的合计。
        hidden = count - len(rows)
        if hidden:
            lines.append(f"(还有 {hidden} 笔更早的没列出来,上面的合计是全区间的)")
        return "\n".join(lines)

    def set_budget(scope: str, monthly_limit: float) -> str:
        """给一条线设**每月**额度。scope 取某个类目(餐饮|交通|日用|娱乐|医疗|人情|其他)
        或者「总额」,monthly_limit 为元;同一条线再设一次就是改额度。
        **不设就没有提醒**——没有默认额度;设了之后,哪个月哪条线超了,会在那一笔记账
        (或改账)的回话里说出已花、额度、超了多少。撤掉一条线用 finance__remove_budget,别设成 0。"""
        if scope not in BUDGET_SCOPES:
            return _no_such_scope(scope) + "这条没设。"
        cents = _to_cents(monthly_limit)
        if cents is None or cents <= 0 or cents > _MAX_CENTS:
            # 上界同 `record_expense` 的金额:超出 int64 时 sqlite3 在**绑定参数时**抛
            # `OverflowError`,而它不是 `sqlite3.Error` 的子类,会逃出工具边界(M4-2 补)。
            # **0 尤其不许当"撤掉"使**——哨兵值正是那种"后来没人记得它有第二个意思"的
            # 写法,撤掉有自己的工具(同 amend / delete 分开的理由)。
            return f"额度不对({monthly_limit}):要一个大于 0 的数字,单位是元(比如 2000)。这条没设。"
        try:
            conn.execute(
                _BUDGET_SQL["set"],
                (
                    scope,
                    cents,
                    datetime.now(tz).replace(tzinfo=None).isoformat(timespec="seconds"),
                ),
            )
        except sqlite3.Error as exc:  # E2:设不上也要让模型知道这步没成
            return f"这条预算没设上(库写入失败:{exc})。"

        head = f"预算设好了:{scope} {_yuan(cents)} 元/月。"
        # G8 的第四条路,而它是最容易漏的一条:**线是用户自己划下来的**——划在已经花掉的
        # 钱以下,那一刻这条线就超了,而这次动作里没有任何一笔支出。所以回话带上这条线
        # **当下**的状态(所以这里用"现在"那个月:它回答的是"我这条线现在怎么样",
        # 不是某一笔落在哪个月)。少了这半句,用户要等到下一笔记账才知道自己已经超了。
        try:
            status = _budget_lines(
                conn, when=datetime.now(tz).date(), scopes=(scope,), over_only=False
            )
        except sqlite3.Error as exc:  # E2:额度写进去了,别谎称没设上
            status = [f"(这条线现在花到哪儿没查成:{exc})"]
        return "\n".join([head, *status])

    def list_budgets() -> str:
        """列出设过的预算:每条线的月额度 + 那条线**本月**已花(超了会一并说出来),
        没设过就直说没设。改额度用 finance__set_budget,撤掉用 finance__remove_budget。"""
        try:
            status = _budget_lines(
                conn, when=datetime.now(tz).date(), scopes=BUDGET_SCOPES, over_only=False
            )
        except sqlite3.Error as exc:  # E2:查不了也要让模型知道,而不是整轮炸掉
            return f"查不了(库读取失败:{exc})。"
        if not status:
            # "没设"这件事必须说得出来:**不设就没有提醒**是这条线的设计,而模型
            # 看不见库——回一句空话它只能猜,猜出来的往往是一个发明的默认额度。
            return "还没有设预算(不设就没有提醒)。"
        # 顺序由 `BUDGET_SCOPES` 定死(类目按 CATEGORIES、总额垫底),和设的先后无关。
        return "\n".join(
            [f"设了 {len(status)} 条预算(都是一个月的额度):", *(f"- {s}" for s in status)]
        )

    def remove_budget(scope: str) -> str:
        """撤掉一条线的预算(scope 同 finance__set_budget):撤了之后这条线不再提醒,
        回到"没设"那个状态。只想改额度的话用 finance__set_budget,**别拿 0 当撤掉**。"""
        if scope not in BUDGET_SCOPES:
            return _no_such_scope(scope) + "什么都没动。"
        try:
            # `DELETE … RETURNING`:一条语句既删掉又拿回被删的那一行,所以"原来是多少"
            # 不需要先读一次(先读再删就是 M5-31 那个洞的形状:两个并发的撤销会都报
            # 「撤了(原来是 500)」)。
            gone = conn.execute(_BUDGET_SQL["remove"], (scope,)).fetchone()
        except sqlite3.Error as exc:  # E2:撤不掉也要让模型知道这步没成
            return f"这条预算没撤成(库写入失败:{exc})。"
        if gone is None:
            # 不许回"撤了"——那是句假话,而用户会以为自己刚关掉了一个提醒。
            return f"{scope}本来就没有预算,什么都没动。"
        return f"{scope}的预算撤了(原来是 {_yuan(gone['limit_cents'])} 元/月),以后记账不再提醒它。"

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
        # M6-4:预算那三个,同样只追加在末尾,前七位一个没动。
        set_budget,
        list_budgets,
        remove_budget,
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
