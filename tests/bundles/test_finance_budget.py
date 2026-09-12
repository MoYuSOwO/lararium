"""M6-4 预算 / 超支提醒:`set_budget` / `list_budgets` / `remove_budget`,以及**超线那句话**。

## 形状(用户定的)

一条线 = 一个 scope(某个类目,或者「总额」)+ 一个**月**额度。提醒**拼在写操作的回话里**,
不新开一条消息(新开就要走出件箱,那是主动推送的形状)。**不设就没有提醒**——没有默认额度。
只说**已花 / 额度 / 超了多少**:账本 9 月 6 号才开张,8 月没有数,环比是编的。

## 这个文件钉的六件事

1. **没设预算时,回话逐字节不变**(`test_finance_record.py` 那些断言是金样,一条没动);
2. **设了没超,回话同样逐字节不变**——「正好花完」也不算超;
3. 超了 → 多一句,**已花 / 额度 / 超了多少三个数都在**;
4. 类目和总额被同一笔越过 → 两条都说,**顺序写死**(类目先、总额后,不跟着 SQL 排序抖);
5. 月份按那笔支出的 `occurred_at` 所在的月算,**不是"现在"**;
6. **超线就说,不只是跨过线的那一笔**:第二笔、第三笔照样说。

## 为什么不只有 `record_expense` 会说(G8)

「本月这条线有没有超」是关于**状态**的不变量,而能改那个状态的动作不止一个:
`amend_expense` 把 50 改成 5000、`delete_expense` 带 `undo=True` 把删掉的那笔放回账上、
`set_budget` 自己把线划在已经花掉的钱以下——**三条路都能让"超了"成立,而三条都是无声的**。
G8 说得很直白:一条不变量有几条路能破它,就要在几处守;守的方式不是拦(底稿是用户的),
是**在那一刻把事实说出口**。所以这个文件对四条路各钉一条。
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from bundles.finance.server import BUDGET_SCOPES, build

SHANGHAI = "Asia/Shanghai"
# 「今天」按**配置时区**取,和 bundle 里 `datetime.now(tz)` 同一个口径:跑在 UTC 的机器上
# 用系统本地日期,月底那一天会和 bundle 差一个月(M1 Task 9 那个 8 小时时差的同一张脸)。
TODAY = datetime.now(ZoneInfo(SHANGHAI)).date()

# 「现在」那个月。`set_budget` / `list_budgets` 报的是**当下**的状态(它们回答"我这条线
# 现在怎么样"),所以这几条测试的日期必须跟着真时钟走,不能写死 2026-09 ——写死的话
# 10 月 1 号整个文件变红,而那不是代码坏了。每个月都有 4 号,所以 `-04` 永远存在。
THIS_MONTH = f"{TODAY:%Y-%m}"
THIS_MM = f"{TODAY:%m}"
# 上一个月的 28 号:跨月那条要的是「`occurred_at` 的月 ≠ 现在的月」,而这个数**任何时候
# 都成立**(每个月都有 28 号)。任务书举的例子是"10 月 2 号补记一笔 9 月 28 号的账"。
_LAST_MONTH_END = TODAY.replace(day=1) - timedelta(days=1)
LAST_MONTH = f"{_LAST_MONTH_END:%Y-%m}"
LAST_MM = f"{_LAST_MONTH_END:%m}"
# 上个月**最后一天**的号(28/29/30/31,跟着真时钟走)。验收补:月界的上界取的是次月
# 1 号、开区间,而**月末那一天带着时刻**是唯一能把这件事测出来的日子。
LAST_MONTH_DD = f"{_LAST_MONTH_END:%d}"

MONTH = {"since": "2026-09-01", "until": "2026-09-30"}
NOTHING_SET = "还没有设预算(不设就没有提醒)。"

# ── 老库:真机现在那个形状(`expenses` 8 列 + `income` 7 列 + `sqlite_sequence`)。
# 13 行支出(3 行已删,带着 M5-26 迁移写下的理由)+ 2 行收入,和真机一致。
LEGACY_DDL = (
    "CREATE TABLE expenses (id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " amount_cents INTEGER NOT NULL, category TEXT NOT NULL, occurred_at TEXT NOT NULL,"
    " note TEXT, created_at TEXT NOT NULL, deleted_at TEXT, deleted_reason TEXT);"
    "CREATE INDEX idx_expenses_occurred_at ON expenses(occurred_at);"
    "CREATE TABLE income (id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " amount_cents INTEGER NOT NULL, occurred_at TEXT NOT NULL, note TEXT,"
    " created_at TEXT NOT NULL, deleted_at TEXT, deleted_reason TEXT);"
    "CREATE INDEX idx_income_occurred_at ON income(occurred_at);"
)
LEGACY_RETIRED = "M5-26 迁移:这行原先被改写顶掉了,本来就不算在账上"
# (id, 分, 类目, occurred_at, 备注, deleted_at)
LEGACY_ROWS = (
    (1, 2800, "交通", "2026-09-05T08:00:00", "打车", "2026-09-11T01:00:00"),
    (2, 4550, "餐饮", "2026-09-05T12:00:00", "午饭", None),
    (3, 2800, "交通", "2026-09-05T08:30:00", "打车(改过时间)", "2026-09-11T01:00:00"),
    (4, 1200, "日用", "2026-09-06T19:30:00", None, None),
    (5, 6600, "餐饮", "2026-09-07T20:00:00", None, "2026-09-11T01:00:00"),
    (6, 6600, "餐饮", "2026-09-07T20:00:00", "和朋友吃饭", None),
    (7, 80000, "医疗", "2026-09-08T10:00:00", "牙医", None),
    (8, 2353, "日用", "2026-09-08T21:00:00", None, None),
    (9, 3000, "交通", "2026-09-09T07:40:00", None, None),
    (10, 1800, "餐饮", "2026-09-09T12:10:00", "煎饼", None),
    (11, 4500, "娱乐", "2026-09-09T20:00:00", None, None),
    (12, 1500, "人情", "2026-09-09T22:22:00", "随份子", None),
    (13, 50000, "其他", "2026-09-10T09:00:00", "ChatGPT 年费", None),
)
# (id, 分, occurred_at, 备注)
LEGACY_INCOME_ROWS = (
    (1, 60000, "2026-09-12T10:00:00", "ChatGPT 退的"),
    (2, 300000, "2026-09-15T20:00:00", "妈妈打的"),
)

# **金样是分支起点那个提交(303937c)的代码在这份库上跑出来的**,不是照着新代码誊的
# (M5-26 / M6-3 / M6-3a 立的口径)。产出它的脚本:`git show 303937c:bundles/finance/server.py`
# 落成一个独立文件,`importlib.util.spec_from_file_location("finance_baseline_303937c", …)`
# 当模块加载,在**同一份库**上跑这四次调用。脚本在交付报告里交代,不进仓库——它要么得
# `subprocess` 去调 git(全局约束「无 shell」的反面),要么得把 927 行旧代码抄进仓库
# 当 fixture(G2 换个姿势)。
#
# 顺带一个独立口径:前三份和 `test_finance_income.py` 里冻着的
# `LEGACY_LIST` / `LEGACY_BY_CATEGORY` / `LEGACY_BY_DAY` **逐字节相同**——那三份是 M6-3a
# 从另一个基线(50fde56)产出的,两条路走到同一个字节串上(T6 第四种:每个数都要有
# 第二个独立口径对账)。
BASELINE_LIST = "\n".join(
    (
        "最近 10 笔:",
        "- #13 2026-09-10 09:00 其他 500.00 元 · 备注「ChatGPT 年费」",
        "- #12 2026-09-09 22:22 人情 15.00 元 · 备注「随份子」",
        "- #11 2026-09-09 20:00 娱乐 45.00 元",
        "- #10 2026-09-09 12:10 餐饮 18.00 元 · 备注「煎饼」",
        "- #9 2026-09-09 07:40 交通 30.00 元",
        "- #8 2026-09-08 21:00 日用 23.53 元",
        "- #7 2026-09-08 10:00 医疗 800.00 元 · 备注「牙医」",
        "- #6 2026-09-07 20:00 餐饮 66.00 元 · 备注「和朋友吃饭」",
        "- #4 2026-09-06 19:30 日用 12.00 元",
        "- #2 2026-09-05 12:00 餐饮 45.50 元 · 备注「午饭」",
    )
)
BASELINE_BY_CATEGORY = "\n".join(
    (
        "2026-09-01 ~ 2026-09-30,共 10 笔,合计 1555.03 元(按类目):",
        "- 医疗 800.00 元(1 笔)",
        "- 其他 500.00 元(1 笔)",
        "- 餐饮 129.50 元(3 笔)",
        "- 娱乐 45.00 元(1 笔)",
        "- 日用 35.53 元(2 笔)",
        "- 交通 30.00 元(1 笔)",
        "- 人情 15.00 元(1 笔)",
    )
)
BASELINE_BY_DAY = "\n".join(
    (
        "2026-09-01 ~ 2026-09-30,共 10 笔,合计 1555.03 元(按天):",
        "- 2026-09-05 45.50 元(1 笔)",
        "- 2026-09-06 12.00 元(1 笔)",
        "- 2026-09-07 66.00 元(1 笔)",
        "- 2026-09-08 823.53 元(2 笔)",
        "- 2026-09-09 108.00 元(4 笔)",
        "- 2026-09-10 500.00 元(1 笔)",
    )
)
BASELINE_INCOME = "\n".join(
    (
        "收入 3600.00 元(2 笔)。收入不算在「花了多少」里。",
        "- 2026-09-15 20:00 收入 3000.00 元 · 备注「妈妈打的」",
        "- 2026-09-12 10:00 收入 600.00 元 · 备注「ChatGPT 退的」",
    )
)


def tool(runtime, name: str):
    return next(f for f in runtime.tools if f.__name__ == name)


def budget_rows(data_dir: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(data_dir / "finance" / "finance.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM budgets ORDER BY scope"))
    finally:
        conn.close()


def table_names(data_dir: Path) -> list[str]:
    conn = sqlite3.connect(data_dir / "finance" / "finance.sqlite")
    try:
        return sorted(
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        )
    finally:
        conn.close()


def row_count(data_dir: Path, table: str) -> int:
    conn = sqlite3.connect(data_dir / "finance" / "finance.sqlite")
    try:
        # 两条写死的字面量,不拼表名(S608 的理由,同 server.py)
        sql = (
            "SELECT count(*) FROM expenses"
            if table == "expenses"
            else "SELECT count(*) FROM income"
        )
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def make_legacy_database(root: Path) -> None:
    """真机现在那份库的形状。和 `test_finance_income.py` 的同名函数是两份:那一份造的是
    M6-3 / M6-3 之前的形状(带 `kind` / `of_expense_id`,或者压根没有 income 表),
    **而这一份造的是 M6-3a 推上去之后的形状**——7 列的 income 表。"""
    (root / "finance").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "finance" / "finance.sqlite")
    conn.executescript(LEGACY_DDL)
    conn.executemany(
        "INSERT INTO expenses (id, amount_cents, category, occurred_at, note, deleted_at,"
        " deleted_reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(*row, LEGACY_RETIRED if row[5] else None, row[3]) for row in LEGACY_ROWS],
    )
    conn.executemany(
        "INSERT INTO income (id, amount_cents, occurred_at, note, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        [(*row, row[2]) for row in LEGACY_INCOME_ROWS],
    )
    conn.commit()
    conn.close()


@pytest.fixture
def runtime(tmp_path):
    return build(tmp_path, timezone=SHANGHAI)


# ───────────────── 硬口径 1、2:没设、没超,回话逐字节不变 ─────────────────


def test_without_a_budget_the_reply_is_the_one_it_has_always_been(runtime):
    """★ 不设就没有提醒。这一句是 M4-2 起的原文,**逐字节**——断整句不断片段(T6 第五种)。

    金样在 `test_finance_record.py` 那几条断言里(一条都没动),这里再钉一次全文:
    那个文件断的是"`45.00 元` 在里面",挡不住"后面多了一行"。
    """
    said = tool(runtime, "record_expense")(
        45, "餐饮", occurred_at="2026-09-04 12:00", note="公司楼下"
    )

    assert said == "记好了:餐饮 45.00 元(09-04 12:00) · 备注「公司楼下」。"


def test_a_budget_you_have_not_crossed_leaves_the_reply_untouched(runtime):
    """★ 设了没超 → 同样一个字节不多。**正好花完也不算超**(没有"超了 0.00 元"这种话),
    而别的类目的线更不该替这一笔说话。"""
    record = tool(runtime, "record_expense")
    tool(runtime, "set_budget")("餐饮", 500)

    under = record(45, "餐饮", occurred_at="2026-09-04 12:00", note="公司楼下")
    exact = record(455, "餐饮", occurred_at="2026-09-05 12:00")
    other = record(900, "交通", occurred_at="2026-09-06 12:00")

    assert under == "记好了:餐饮 45.00 元(09-04 12:00) · 备注「公司楼下」。"
    assert exact == "记好了:餐饮 455.00 元(09-05 12:00)。", "正好花到额度上,不是超了"
    assert other == "记好了:交通 900.00 元(09-06 12:00)。", "餐饮那条线替交通说了话"


# ───────────────── 硬口径 3、4、6:超了怎么说 ─────────────────


def test_crossing_the_line_says_what_you_spent_the_limit_and_the_overflow(runtime):
    """★ 超了 → 多一句,**三个数都在**:已花 620 / 额度 500 / 超了 120。

    三个数互不相同也不互为倍数,谁把口径搞反了都藏不住。
    """
    record = tool(runtime, "record_expense")
    tool(runtime, "set_budget")("餐饮", 500)
    record(300, "餐饮", occurred_at="2026-09-03 12:00")

    said = record(320, "餐饮", occurred_at="2026-09-04 12:00")

    assert said == "\n".join(
        (
            "记好了:餐饮 320.00 元(09-04 12:00)。",
            "2026-09 餐饮已花 620.00 元,额度 500.00 元,超了 120.00 元。",
        )
    )


def test_the_second_expense_over_the_line_says_it_too(runtime):
    """★ **超线就说,不只是跨过线的那一笔。**

    「已经超了」这件事在这个月后面每一笔上都成立;只在跨线那一笔说,等于让后面每一笔
    装作没事。而判"是不是这一笔跨的"要多查一次"这一笔之前的合计"——多花一次查询换来的
    是更少的信息。
    """
    record = tool(runtime, "record_expense")
    tool(runtime, "set_budget")("餐饮", 500)
    record(300, "餐饮", occurred_at="2026-09-03 12:00")
    record(320, "餐饮", occurred_at="2026-09-04 12:00")  # 跨线的那一笔

    said = record(10, "餐饮", occurred_at="2026-09-05 12:00")

    assert said == "\n".join(
        (
            "记好了:餐饮 10.00 元(09-05 12:00)。",
            "2026-09 餐饮已花 630.00 元,额度 500.00 元,超了 130.00 元。",
        )
    )


def test_a_category_and_the_total_crossed_by_one_expense_are_both_said_category_first(runtime):
    """★ 同一笔越过两条线 → 两条都说,**顺序写死:类目先、总额后**。

    顺序不许跟着 SQL 的排序抖(那一份是按金额降序的),所以这里断的是整份回话相等。
    类目那条排在前面是因为它更具体——"哪一类吃掉了预算"是下一步能动手的那个信息。
    """
    record = tool(runtime, "record_expense")
    set_budget = tool(runtime, "set_budget")
    set_budget("餐饮", 500)
    set_budget("总额", 700)
    record(300, "餐饮", occurred_at="2026-09-03 12:00")
    record(100, "交通", occurred_at="2026-09-03 20:00")

    said = record(320, "餐饮", occurred_at="2026-09-04 12:00")

    assert said == "\n".join(
        (
            "记好了:餐饮 320.00 元(09-04 12:00)。",
            "2026-09 餐饮已花 620.00 元,额度 500.00 元,超了 120.00 元。",
            "2026-09 总额已花 720.00 元,额度 700.00 元,超了 20.00 元。",
        )
    )


# ───────────────── 硬口径 5:月份按 occurred_at 算 ─────────────────


def test_the_month_comes_from_the_expense_not_from_today(runtime):
    """★ 跨月:10 月 2 号补记一笔 9 月 28 号的账,吃的是**9 月**的额度。

    两个方向一起钉:补记那一笔要按它自己那个月说话(按"现在"算的话那个月压根没有支出,
    于是一声不吭);而本月这一笔**不许把上个月那 600 算进来**(按"现在"算的实现在这里
    反而会多说一句)。
    """
    record = tool(runtime, "record_expense")
    tool(runtime, "set_budget")("餐饮", 500)

    backfilled = record(600, "餐饮", occurred_at=f"{LAST_MONTH}-28 12:00")
    this_month = record(45, "餐饮", occurred_at=f"{THIS_MONTH}-04 12:00")

    assert backfilled == "\n".join(
        (
            f"记好了:餐饮 600.00 元({LAST_MM}-28 12:00)。",
            f"{LAST_MONTH} 餐饮已花 600.00 元,额度 500.00 元,超了 100.00 元。",
        )
    )
    assert this_month == f"记好了:餐饮 45.00 元({THIS_MM}-04 12:00)。", "上个月那 600 被算进了本月"


def test_an_expense_on_the_last_day_of_the_month_still_counts(runtime):
    """★ 验收补:**月末那一天的账要算进这个月的已花。**

    月界的上界取的是**次月 1 号、开区间**(`occurred_at < upper`),而库里存的是
    `YYYY-MM-DDTHH:MM:SS`。要是谁把上界写成"本月最后一天"(闭区间的那个直觉),
    月末那一天带着时刻的流水就被吃掉——`_month_bounds` 的 docstring 一直在讲这件事,
    **而没有一条测试钉它**:验收时把上界减一天,全套 191 条一条都没红。

    症状是这个功能最坏的那一种:`query_spending` 印「合计 140.00 元」,而预算这边
    一声不吭——**两个数不一致,而只有一个会说话**。「已花只有一份 SQL」那条保证管的是
    聚合,管不到月界,因为月界是另外算的。

    用上个月的最后一天(号数跟着真时钟走,28/29/30/31 都覆盖得到):当月的最后一天
    在跑测试的那天可能还没到,那样这笔账就成了未来的账。
    """
    record = tool(runtime, "record_expense")
    tool(runtime, "set_budget")("餐饮", 100)

    record(90, "餐饮", occurred_at=f"{LAST_MONTH}-01 12:00")
    said = record(50, "餐饮", occurred_at=f"{LAST_MONTH}-{LAST_MONTH_DD} 22:00")

    assert said == "\n".join(
        (
            f"记好了:餐饮 50.00 元({LAST_MM}-{LAST_MONTH_DD} 22:00)。",
            f"{LAST_MONTH} 餐饮已花 140.00 元,额度 100.00 元,超了 40.00 元。",
        )
    ), said


# ───────────────── 「已花」只有一份算法 ─────────────────


def test_what_you_spent_is_the_number_query_spending_prints(runtime):
    """★ 「已花」= 那个月、那个 scope 的支出合计,**和 `query_spending` 印的数一致**
    ——类目预算对它的类目行,总额预算对它的合计行。

    算它的 SQL 只许有一份(实现里跑的就是 `_GROUP_SQL["category"]`),而这条测试是那件事的
    机械保证:库里故意摆了三种"不该算进来"的行——**已删的**、**上个月的**、**别的类目的**。
    第二份 SQL 里漏掉哪一个条件,两个数就对不上:已删那 300 会让餐饮变成 850、
    上个月那 999 会让合计变成 2437。
    """
    record = tool(runtime, "record_expense")
    set_budget = tool(runtime, "set_budget")
    set_budget("餐饮", 500)
    set_budget("总额", 1000)
    record(300, "餐饮", occurred_at="2026-09-03 12:00")  # #1,一会儿删掉
    record(400, "餐饮", occurred_at="2026-09-04 12:00")
    record(999, "餐饮", occurred_at="2026-08-20 12:00")  # 上个月,不进 9 月这条线
    record(888, "交通", occurred_at="2026-09-05 12:00")  # 别的类目,只进总额
    tool(runtime, "delete_expense")(expense_id=1, reason="记重了")

    said = record(150, "餐饮", occurred_at="2026-09-06 12:00")
    by_category = tool(runtime, "query_spending")(**MONTH, group_by="category")

    assert said == "\n".join(
        (
            "记好了:餐饮 150.00 元(09-06 12:00)。",
            "2026-09 餐饮已花 550.00 元,额度 500.00 元,超了 50.00 元。",
            "2026-09 总额已花 1438.00 元,额度 1000.00 元,超了 438.00 元。",
        )
    )
    assert "- 餐饮 550.00 元(2 笔)" in by_category, f"类目那条线和类目行对不上:{by_category}"
    assert "共 3 笔,合计 1438.00 元" in by_category, f"总额那条线和合计行对不上:{by_category}"


# ───────────────── G8:另外三条能让"超了"成立的路 ─────────────────


def test_amending_an_amount_up_over_the_line_says_so(runtime):
    """★ G8 第一条无声的路:把 50 改成 5000 同样能把你推过线,而 `amend_expense` 上没有闸。

    检查是**同一个共用函数**(不许复制一份):`record_expense` 说什么、这里就说什么。
    """
    record = tool(runtime, "record_expense")
    tool(runtime, "set_budget")("餐饮", 500)
    record(50, "餐饮", occurred_at="2026-09-03 12:00")

    said = tool(runtime, "amend_expense")(expense_id=1, amount=5000)

    assert said == "\n".join(
        (
            "改了 #1:餐饮 50.00 元 → 餐饮 5000.00 元(2026-09-03 12:00)。还是 #1,号没变。",
            "2026-09 餐饮已花 5000.00 元,额度 500.00 元,超了 4500.00 元。",
        )
    )


def test_amending_the_category_speaks_for_the_line_the_expense_landed_in(runtime):
    """`amend_expense` 能把一笔支出**搬到另一条线上**(改类目、改月份都算)。
    说的是它**现在落在**哪条线上——那才是这一笔此刻在吃谁的额度。"""
    tool(runtime, "set_budget")("交通", 100)
    tool(runtime, "record_expense")(300, "餐饮", occurred_at="2026-09-03 12:00")

    said = tool(runtime, "amend_expense")(expense_id=1, category="交通")

    assert said == "\n".join(
        (
            "改了 #1:餐饮 300.00 元 → 交通 300.00 元(2026-09-03 12:00)。还是 #1,号没变。",
            "2026-09 交通已花 300.00 元,额度 100.00 元,超了 200.00 元。",
        )
    )


def test_undoing_a_delete_puts_the_line_back_over_and_says_so(runtime):
    """★ G8 第二条无声的路:`delete_expense(undo=True)` 把那笔放回账上,合计跟着回去。

    中间那次删除把这条线降到线下,那一句就该**干干净净**(不超不说)——同一个判据的两面。
    """
    remove = tool(runtime, "delete_expense")
    tool(runtime, "set_budget")("餐饮", 500)
    tool(runtime, "record_expense")(620, "餐饮", occurred_at="2026-09-04 12:00")

    deleted = remove(expense_id=1, reason="记重了")
    restored = remove(expense_id=1, undo=True)

    assert deleted == (
        "删了 #1:餐饮 620.00 元 · 原因「记重了」。合计里不算它了。"
        "删错的话再调一次 delete_expense、带 undo=True 就能拿回来。"
    ), "删完已经在线下了,还在说超支"
    assert restored == "\n".join(
        (
            "恢复了 #1:餐饮 620.00 元 又回到账上了。",
            "2026-09 餐饮已花 620.00 元,额度 500.00 元,超了 120.00 元。",
        )
    )


def test_a_delete_that_leaves_the_line_still_over_says_so(runtime):
    """删掉一笔之后**还是超着**,那就照说。

    「已经超了」是关于状态的,不是关于哪个动作的:删完还超着而一声不吭,和「只在跨线
    那一笔说」是同一种装作没事。
    """
    record = tool(runtime, "record_expense")
    tool(runtime, "set_budget")("餐饮", 500)
    record(620, "餐饮", occurred_at="2026-09-04 12:00")
    record(200, "餐饮", occurred_at="2026-09-05 12:00")

    said = tool(runtime, "delete_expense")(expense_id=2, reason="记重了")

    assert said == "\n".join(
        (
            "删了 #2:餐饮 200.00 元 · 原因「记重了」。合计里不算它了。"
            "删错的话再调一次 delete_expense、带 undo=True 就能拿回来。",
            "2026-09 餐饮已花 620.00 元,额度 500.00 元,超了 120.00 元。",
        )
    )


# ───────────────── 三个工具自己 ─────────────────


def test_setting_a_budget_you_have_already_blown_says_so_right_away(runtime, tmp_path):
    """★ G8 第三条无声的路:**线是你自己划下来的**——划在已经花掉的钱以下,那一刻就超了。

    所以 `set_budget` 的回话带上这条线**当下**的状态(用的是"现在"那个月:它回答的是
    "我这条线现在怎么样",不是某一笔落在哪个月)。少了这半句,用户要等到下一笔记账才知道。
    """
    tool(runtime, "record_expense")(620, "餐饮", occurred_at=f"{THIS_MONTH}-04 12:00")

    said = tool(runtime, "set_budget")("餐饮", 500)

    assert said == "\n".join(
        (
            "预算设好了:餐饮 500.00 元/月。",
            f"{THIS_MONTH} 餐饮已花 620.00 元,额度 500.00 元,超了 120.00 元。",
        )
    )
    assert [(r["scope"], r["limit_cents"]) for r in budget_rows(tmp_path)] == [("餐饮", 50000)]


def test_setting_the_same_scope_again_replaces_the_line(runtime, tmp_path):
    """一条线一个 scope:改额度是**盖掉**那一条,不是再摆一条(否则"哪一条算"没有答案)。"""
    set_budget = tool(runtime, "set_budget")
    set_budget("餐饮", 500)
    set_budget("餐饮", 800)

    assert [(r["scope"], r["limit_cents"]) for r in budget_rows(tmp_path)] == [("餐饮", 80000)]
    assert tool(runtime, "list_budgets")() == "\n".join(
        (
            "设了 1 条预算(都是一个月的额度):",
            f"- {THIS_MONTH} 餐饮已花 0.00 元,额度 800.00 元。",
        )
    )


def test_list_budgets_says_nothing_is_set_before_anything_is_set(runtime):
    """**不设就没有提醒**,那么"没设"这件事也得说得出来——否则模型只能猜。"""
    assert tool(runtime, "list_budgets")() == NOTHING_SET


def test_list_budgets_orders_the_lines_the_same_way_every_time(runtime):
    """顺序写死:类目按 `CATEGORIES` 的顺序、总额垫底,**和设的先后无关**。

    跟着插入顺序或者 SQL 默认顺序走的话,同一份数据每次读回来的次序会抖——
    而这份输出会以 tool_result 的身份进上下文,抖一次就是一次缓存之外的字节变化。
    """
    set_budget = tool(runtime, "set_budget")
    set_budget("总额", 3000)
    set_budget("交通", 100)
    set_budget("餐饮", 500)
    tool(runtime, "record_expense")(45, "餐饮", occurred_at=f"{THIS_MONTH}-04 12:00")

    assert tool(runtime, "list_budgets")() == "\n".join(
        (
            "设了 3 条预算(都是一个月的额度):",
            f"- {THIS_MONTH} 餐饮已花 45.00 元,额度 500.00 元。",
            f"- {THIS_MONTH} 交通已花 0.00 元,额度 100.00 元。",
            f"- {THIS_MONTH} 总额已花 45.00 元,额度 3000.00 元。",
        )
    )


def test_removing_a_budget_stops_the_reminder(runtime, tmp_path):
    """★ 撤。设错了、或者这条线不想要了,得有路回到「没设」那个状态——
    而那个状态正是"没有提醒"的定义。撤完再记一笔,回话逐字节回到原样。"""
    record = tool(runtime, "record_expense")
    tool(runtime, "set_budget")("餐饮", 500)
    record(620, "餐饮", occurred_at="2026-09-04 12:00")

    said = tool(runtime, "remove_budget")("餐饮")

    assert said == "餐饮的预算撤了(原来是 500.00 元/月),以后记账不再提醒它。"
    assert budget_rows(tmp_path) == []
    assert tool(runtime, "list_budgets")() == NOTHING_SET
    assert (
        record(10, "餐饮", occurred_at="2026-09-05 12:00") == "记好了:餐饮 10.00 元(09-05 12:00)。"
    )


def test_removing_a_budget_that_was_never_set_says_so_and_changes_nothing(runtime, tmp_path):
    """没有的东西撤不掉。回一句"本来就没有"而不是"撤了"——后者是句假话,
    而用户会以为自己刚关掉了一个提醒。"""
    tool(runtime, "set_budget")("餐饮", 500)

    said = tool(runtime, "remove_budget")("交通")

    assert said == "交通本来就没有预算,什么都没动。"
    assert [r["scope"] for r in budget_rows(tmp_path)] == ["餐饮"]


@pytest.mark.parametrize("name", ["set_budget", "remove_budget"])
def test_an_unknown_scope_is_refused_with_the_legal_ones_spelled_out(runtime, tmp_path, name):
    """E2:看不懂的 scope 要给人话 + 列全合法值(含「总额」),模型才能自己纠正重试。"""
    said = tool(runtime, name)("外卖", 500) if name == "set_budget" else tool(runtime, name)("外卖")

    assert "外卖" in said
    for legal in BUDGET_SCOPES:
        assert legal in said, f"提示里少了 {legal}:{said}"
    assert budget_rows(tmp_path) == []


@pytest.mark.parametrize("bad", [0, -500, 1e17, 10**19])
def test_a_limit_that_is_not_a_positive_amount_is_refused_instead_of_escaping(
    runtime, tmp_path, bad
):
    """E2:0 / 负数 / 大到 SQLite 存不下的额度都得回人话。

    `1e17` 元换算成分超出 int64,sqlite3 在**绑定参数时**抛 `OverflowError`——而它不是
    `sqlite3.Error` 的子类,`except sqlite3.Error` 接不住,异常直接逃出工具边界
    (M4-2 那个坑的同一张脸)。**0 元额度尤其不许当"撤掉"使**:哨兵值就是那种
    "后来没人记得它有第二个意思"的写法,撤掉有自己的工具。
    """
    said = tool(runtime, "set_budget")("餐饮", bad)

    assert "额度" in said and "这条没设" in said, said
    assert budget_rows(tmp_path) == []


# ───────────────── 迁移:老库开库 ─────────────────


def test_the_database_that_is_already_on_the_server_grows_an_empty_budgets_table(tmp_path):
    """★ 真机那份库(`expenses` + 7 列的 `income` + `sqlite_sequence`)开库:
    `budgets` 自己建出来、**是空的**,现有数据一行不动,四份视图**和基线提交逐字节一致**。

    金样的出处见文件头。"现有输出一个字节没变"是证出来的,不是看出来的。
    """
    make_legacy_database(tmp_path)

    runtime = build(tmp_path, timezone=SHANGHAI)

    assert table_names(tmp_path) == ["budgets", "expenses", "income", "sqlite_sequence"]
    assert budget_rows(tmp_path) == [], "新表建出来了,但里面不该有东西"
    assert tool(runtime, "list_budgets")() == NOTHING_SET
    assert tool(runtime, "list_recent")() == BASELINE_LIST
    assert tool(runtime, "query_spending")(**MONTH, group_by="category") == BASELINE_BY_CATEGORY
    assert tool(runtime, "query_spending")(**MONTH, group_by="day") == BASELINE_BY_DAY
    assert tool(runtime, "list_income")() == BASELINE_INCOME
    assert row_count(tmp_path, "expenses") == len(LEGACY_ROWS), "迁移动了支出的行数"
    assert row_count(tmp_path, "income") == len(LEGACY_INCOME_ROWS), "迁移动了收入的行数"


def test_opening_the_database_again_keeps_the_budgets_that_are_already_set(tmp_path):
    """开第二次库不许把已经设下的预算冲掉(`CREATE TABLE IF NOT EXISTS` 的老坑:
    有人"顺手"改成无条件重建,症状是重启一次预算全没了、而且一声不响)。"""
    make_legacy_database(tmp_path)
    tool(build(tmp_path, timezone=SHANGHAI), "set_budget")("餐饮", 500)

    second = build(tmp_path, timezone=SHANGHAI)

    assert [(r["scope"], r["limit_cents"]) for r in budget_rows(tmp_path)] == [("餐饮", 50000)]
    assert tool(second, "list_recent")() == BASELINE_LIST
