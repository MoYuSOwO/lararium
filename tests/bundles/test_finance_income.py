"""M6-3 `record_income` / `list_income`:账本终于有了"钱回来了"这个口子。

## 为什么要有它

真机上那 600 元 ChatGPT 退款**记不进去**,在一个话头里挂了三天。用户的原话:

> 「我的账本只有"花出去"这一个口子,没有收入、没有退款。刚试了记 -600,系统直接挡回来:
> "要一个大于 0 的数字"。所以要么等你给我补个能记退款的口子,要么我按冲抵处理
> ——去动那两笔 GPT 的账。**但那样底稿就不是原始记录了,我不建议,先挂着。**」

她的处置全对,而**那道校验也是对的**:负数支出不是退款,是一笔坏数据。所以这次补的
不是"放宽校验",是一张新表加两个工具;`record_expense` 收到负数时改成**指路**
(`record_income`),而不是继续只说"要一个大于 0 的数字"。

## 退款和收入不是一件事,这个文件大半在钉这一条

    退款  冲抵某一笔支出 → 「这个月花了多少」要**减掉**它
    收入  生活费/兼职/红包 → **根本不该出现在"花了多少"里**

混成一个的症状很具体:记一笔 3000 生活费,然后「这个月花了多少」变成 -3000。
所以这里有一对对称的测试(`..._comes_off_what_you_spent` /
`test_income_does_not_touch_what_you_spent`),**两条都断整份输出**,不断片段
——少一行、多一行、数字对了口径错了,都红(T6 第五种)。

## 为什么是新表,不是给 expenses 加符号位

论证写在 `bundles/finance/server.py` 的 `income` 表旁边。测试这一侧的成本证据是:
本文件里「支出侧输出逐字节不变」那几条断言,在符号位方案下**一条都不成立**
——收入行会带着一个没有意义的 `category` 挤进 `query_spending` 的 GROUP BY。
"""

import sqlite3
from pathlib import Path

import pytest
from bundles.finance.server import _INCOME_SQL, INCOME_KINDS, build

SHANGHAI = "Asia/Shanghai"

# ── M6-3 的"老库":真机现在那份的形状(13 行、其中 3 行已删),**没有 income 表**。
# 已删那 3 行带着 M5-26 迁移写下的理由,和真机一致。
LEGACY_DDL = (
    "CREATE TABLE expenses (id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " amount_cents INTEGER NOT NULL, category TEXT NOT NULL, occurred_at TEXT NOT NULL,"
    " note TEXT, created_at TEXT NOT NULL, deleted_at TEXT, deleted_reason TEXT);"
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
    # 那两笔 GPT 里的大头,600 元退款退的正是它(任务书里的原话)
    (13, 50000, "其他", "2026-09-10T09:00:00", "ChatGPT 年费", None),
)

# **金样是改动前的代码在这份老库上跑出来的**(照 M5-26 的口径:不是照着新代码誊的)。
# 建了 income 表之后,这三份输出一个字节都不许变。
LEGACY_LIST = "\n".join(
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
LEGACY_BY_CATEGORY = "\n".join(
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
LEGACY_BY_DAY = "\n".join(
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

MONTH = {"since": "2026-09-01", "until": "2026-09-30"}


def tool(runtime, name: str):
    return next(f for f in runtime.tools if f.__name__ == name)


def income_rows(data_dir: Path) -> list[sqlite3.Row]:
    """查全部收入/退款行,含已删的。"""
    conn = sqlite3.connect(data_dir / "finance" / "finance.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM income ORDER BY id"))
    finally:
        conn.close()


def expense_rows(data_dir: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(data_dir / "finance" / "finance.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM expenses ORDER BY id"))
    finally:
        conn.close()


def make_legacy_database(root: Path) -> None:
    (root / "finance").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "finance" / "finance.sqlite")
    conn.executescript(LEGACY_DDL)
    conn.executemany(
        "INSERT INTO expenses (id, amount_cents, category, occurred_at, note, deleted_at,"
        " deleted_reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(*row, LEGACY_RETIRED if row[5] else None, row[3]) for row in LEGACY_ROWS],
    )
    conn.commit()
    conn.close()


@pytest.fixture
def runtime(tmp_path):
    return build(tmp_path, timezone=SHANGHAI)


@pytest.fixture
def gpt(runtime):
    """那两笔 GPT:500.00 + 134.16 = 634.16 元,都记在 2026-09-10。

    金额是真机那两笔的数(任务书里写着 500 + 134.16),留着是因为 634.16 - 600 = 34.16
    ——三个数互不相同、也不互为倍数,谁把口径搞反了都藏不住。
    """
    record = tool(runtime, "record_expense")
    record(amount=500, category="其他", occurred_at="2026-09-10 09:00", note="ChatGPT 年费")
    record(amount=134.16, category="其他", occurred_at="2026-09-10 09:05", note="ChatGPT 加油包")
    return runtime


# ─────────────────────────── ★ 四条核心口径 ───────────────────────────


def test_a_refund_comes_off_what_you_spent_while_the_original_row_stays_untouched(gpt, tmp_path):
    """★ 口径一:记一笔退款指向某笔支出 → 「花了多少」减掉了它,而**底稿一字不变**。

    两件事一起断,因为用户自己就是这么判的:她拒绝"去动那两笔 GPT 的账",理由是
    「那样底稿就不是原始记录了」。所以退款必须在**账面之外**生效——
    `list_recent` 逐字节不变,而合计那边把三个数都说出来。
    """
    before = tool(gpt, "list_recent")()
    rows_before = [dict(r) for r in expense_rows(tmp_path)]

    said = tool(gpt, "record_income")(
        amount=600,
        kind="refund",
        occurred_at="2026-09-12 10:00",
        note="ChatGPT 退的",
        of_expense_id=1,
    )

    assert said == (
        "记好了:退款 600.00 元(09-12 10:00),冲抵 #1(其他 500.00 元)"
        " · 备注「ChatGPT 退的」。「花了多少」里会减掉它。"
    ), said
    assert tool(gpt, "list_recent")() == before, "退款动了底稿——原始记录不许被改"
    assert [dict(r) for r in expense_rows(tmp_path)] == rows_before, "expenses 表被动了"
    assert tool(gpt, "query_spending")(**MONTH, group_by="category") == "\n".join(
        (
            "2026-09-01 ~ 2026-09-30,共 2 笔,合计 634.16 元(按类目):",
            "- 其他 634.16 元(2 笔)",
            "同期收到退款 600.00 元(1 笔):支出 634.16 元,抵掉退款后实际花掉 34.16 元。",
        )
    )


def test_income_does_not_touch_what_you_spent(gpt, tmp_path):
    """★ 口径二:记一笔收入 → 「花了多少」**一个字不变**。

    这是 M6-3 最容易搞错的地方:把退款和收入混成一个,记一笔 3000 生活费之后
    「这个月花了多少」就变成 -3000。所以这里断的是**整份输出相等**,而且金额取 3000
    ——比支出总额大一个数量级,漏进去藏不住。
    """
    before_total = tool(gpt, "query_spending")(**MONTH, group_by="category")
    before_day = tool(gpt, "query_spending")(**MONTH, group_by="day")
    before_list = tool(gpt, "list_recent")()

    said = tool(gpt, "record_income")(
        amount=3000, kind="income", occurred_at="2026-09-10 15:00", note="生活费"
    )

    assert said == (
        "记好了:收入 3000.00 元(09-10 15:00) · 备注「生活费」。收入不算在「花了多少」里。"
    ), said
    assert tool(gpt, "query_spending")(**MONTH, group_by="category") == before_total
    assert tool(gpt, "query_spending")(**MONTH, group_by="day") == before_day
    assert tool(gpt, "list_recent")() == before_list
    assert len(income_rows(tmp_path)) == 1, "收入没落进自己那张表"


def test_pointing_a_refund_at_a_row_that_is_not_there_says_so_and_writes_nothing(gpt, tmp_path):
    """★ 口径三:指向不存在的 `of_expense_id` → 人话,**不落行**。"""
    said = tool(gpt, "record_income")(amount=600, kind="refund", of_expense_id=9999)

    assert "9999" in said and "list_recent" in said, f"得告诉它去哪儿找 #id:{said}"
    assert "of_expense_id" in said, f"得给一条出路(对不上就别传):{said}"
    assert income_rows(tmp_path) == [], "校验没过却落了行"


def test_pointing_a_refund_at_a_deleted_row_says_so_and_writes_nothing(gpt, tmp_path):
    """★ 口径四:指向**已删**的支出 → 人话,不落行(M5-31 给 amend 答过同一个问题)。

    为什么不放过去:已删的那行不在任何合计里,退款"冲抵"它就是冲抵一个不存在的数,
    而回话会说得像办成了——用户以为这个月少花了 600,账上并没有。
    """
    tool(gpt, "delete_expense")(expense_id=1, reason="记重了")

    said = tool(gpt, "record_income")(amount=600, kind="refund", of_expense_id=1)

    assert "#1" in said and "已经删" in said, said
    assert "of_expense_id" in said, f"得给一条出路:{said}"
    assert income_rows(tmp_path) == [], "指向已删行的退款落了库"


# ─────────────────────────── 两个口径怎么说出口 ───────────────────────────


def test_list_income_says_both_numbers_with_what_each_one_means(gpt):
    """两个数都要能说出口径——「进了多少」是收入,而退款是从「花了多少」里减掉的。

    她在真机 [262] 干过一次对的事:「其实是**五天**。账本 9 月 6 号才开张,9/1-9/5
    是空的」。同样的实话精神:两个数摆出来,各自说清它是什么口径,别让模型自己猜。
    """
    tool(gpt, "record_income")(
        amount=3000, kind="income", occurred_at="2026-09-10 15:00", note="生活费"
    )
    tool(gpt, "record_income")(
        amount=600,
        kind="refund",
        occurred_at="2026-09-12 10:00",
        note="ChatGPT 退的",
        of_expense_id=1,
    )

    assert tool(gpt, "list_income")(**MONTH) == "\n".join(
        (
            "2026-09-01 ~ 2026-09-30 收入 3000.00 元(1 笔),退款 600.00 元(1 笔)。"
            "退款从「花了多少」里减掉,收入不算在里面。",
            "- 2026-09-12 10:00 退款 600.00 元(冲抵 #1) · 备注「ChatGPT 退的」",
            "- 2026-09-10 15:00 收入 3000.00 元 · 备注「生活费」",
        )
    )


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("income", "收入 3000.00 元(1 笔)。收入不算在「花了多少」里。"),
        ("refund", "退款 3000.00 元(1 笔)。退款从「花了多少」里减掉。"),
    ],
)
def test_list_income_states_the_kind_it_actually_has(runtime, kind, expected):
    """只有一种的时候只说那一种,而**口径那半句照说**:少了它,「3000」这个数就没有单位。"""
    tool(runtime, "record_income")(amount=3000, kind=kind, occurred_at="2026-09-10 15:00")

    assert tool(runtime, "list_income")().startswith(expected), tool(runtime, "list_income")()


def test_list_income_on_an_empty_table_does_not_claim_the_ledger_is_empty(runtime):
    """ "这段没有" ≠ "一笔都没记过"(照 `list_recent` 的那条教训,别混成一句)。"""
    assert tool(runtime, "list_income")() == "还没有记过收入或退款。"
    assert tool(runtime, "list_income")(**MONTH) == "2026-09-01 ~ 2026-09-30 没有收入也没有退款。"


@pytest.mark.parametrize("bounds", [{"since": "上周"}, {"until": "九月底"}])
def test_list_income_returns_a_readable_hint_for_a_date_it_cannot_read(runtime, bounds):
    """看不懂的日期要给可读提示,不许抛,也不许悄悄当成"全时段"

    ——悄悄放宽区间的话,「这个月进了多少」会答成"从开天辟地到现在进了多少",
    而那个数看起来完全正常。
    """
    said = tool(runtime, "list_income")(**bounds)

    assert "YYYY-MM-DD" in said and next(iter(bounds.values())) in said, said


def test_list_income_totals_cover_the_whole_range_even_when_rows_are_capped(runtime):
    """合计那一行**始终是全区间的**,和列了几行无关(照 `query_spending` 的那条规矩)。

    静默截断读起来和"就这些"一模一样,模型会拿残缺的合计下结论。
    """
    for day in range(1, 23):
        tool(runtime, "record_income")(
            amount=100, kind="income", occurred_at=f"2026-09-{day:02d} 10:00"
        )

    # limit=50 顺手把硬封顶一起断了:钳到 20,不是"limit 说多少给多少"。
    said = tool(runtime, "list_income")(limit=50, **MONTH)

    assert "收入 2200.00 元(22 笔)" in said, f"合计只算了列出来的那几行:{said}"
    assert len(said.splitlines()) == 22, f"20 行流水 + 表头 + 截断说明:{said}"
    assert "还有 2 笔更早的没列出来" in said, f"截断没说出口:{said}"


def test_a_refund_bigger_than_the_spending_says_it_was_a_net_return(gpt):
    """退款比支出还多:说"净收回",不说"实际花掉 -965.84 元"。

    负数的"花了多少"是一句读不懂的话,而这个区间确实是钱变多了。
    """
    tool(gpt, "record_income")(
        amount=1600, kind="refund", occurred_at="2026-09-12 10:00", of_expense_id=1
    )

    said = tool(gpt, "query_spending")(**MONTH, group_by="category")

    assert said.endswith(
        "同期收到退款 1600.00 元(1 笔):支出 634.16 元,抵掉退款后净收回 965.84 元。"
    ), said


def test_a_range_with_only_refunds_does_not_say_there_are_no_records(runtime):
    """区间里没有支出、只有退款:不许回"没有记录"——那是在说这段时间什么都没发生。"""
    tool(runtime, "record_income")(amount=600, kind="refund", occurred_at="2026-09-12 10:00")

    assert tool(runtime, "query_spending")(**MONTH, group_by="category") == (
        "2026-09-01 ~ 2026-09-30 没有支出,同期收到退款 600.00 元(1 笔)。"
    )


def test_a_refund_outside_the_range_is_not_netted(gpt):
    """退款按**它自己的日期**落进区间,不跟着它指的那笔支出跑。

    不然「9 月花了多少」会被 10 月才到账的退款改写,而那个数昨天还是另一个。
    """
    tool(gpt, "record_income")(
        amount=600, kind="refund", occurred_at="2026-10-03 10:00", of_expense_id=1
    )

    assert tool(gpt, "query_spending")(**MONTH, group_by="category") == "\n".join(
        (
            "2026-09-01 ~ 2026-09-30,共 2 笔,合计 634.16 元(按类目):",
            "- 其他 634.16 元(2 笔)",
        )
    ), "9 月的合计被 10 月的退款改写了"


def test_an_expense_with_no_refunds_reads_exactly_as_it_did_before(gpt):
    """没有退款时,`query_spending` 一个字节都不许多——这是本任务的硬口径。"""
    for group_by in ("category", "day"):
        said = tool(gpt, "query_spending")(**MONTH, group_by=group_by)
        assert "退款" not in said and "实际花掉" not in said, said


# ─────────────────────────── 那笔支出后来被删了 ───────────────────────────


def test_deleting_the_expense_a_refund_points_at_leaves_the_refund_alone(gpt, tmp_path):
    """删掉被冲抵的那笔支出,**不许偷偷动那条退款**。

    退款是它自己的一条记录(收到钱这件事发生过),而 `delete_expense` 的职责是
    "这笔支出不该在账上"。让它顺手改掉别的表,正是 M5-20 那个事故的形状
    ——状态变更藏在一个名字不同的动作底下。两个数都还在、都看得见,用户自己判。
    """
    tool(gpt, "record_income")(
        amount=600, kind="refund", occurred_at="2026-09-12 10:00", of_expense_id=1
    )
    before = [dict(r) for r in income_rows(tmp_path)]

    tool(gpt, "delete_expense")(expense_id=1, reason="记重了")

    assert [dict(r) for r in income_rows(tmp_path)] == before, "删支出改动了退款行"
    assert "退款 600.00 元" in tool(gpt, "list_income")(), "退款不见了"


# ─────────────────────────── E2:工具边界不许抛 ───────────────────────────


def test_income_cannot_point_at_an_expense(gpt, tmp_path):
    """`of_expense_id` 只给退款用。收入不冲抵任何支出,传了就是把两件事混了。

    不许悄悄接受:那会让模型以为自己记了一笔退款,而合计里一分都没减。
    """
    said = tool(gpt, "record_income")(amount=3000, kind="income", of_expense_id=1)

    assert "refund" in said and "of_expense_id" in said, said
    assert income_rows(tmp_path) == [], "混了口径却落了行"


def test_an_unknown_kind_is_refused_with_both_meanings_spelled_out(runtime, tmp_path):
    """看不懂的 kind 要把**两种的意思都写出来**,模型才能自己选对再重试(E2)。

    只列合法值不够:`income` 和 `refund` 这两个词本身不解释"哪个会减掉花了多少"。
    """
    said = tool(runtime, "record_income")(amount=600, kind="退货")

    assert "退货" in said
    for legal in INCOME_KINDS:
        assert legal in said, f"提示里必须列全合法值:{said}"
    assert "减掉" in said and "不算" in said, f"两种口径都得说出来:{said}"
    assert income_rows(tmp_path) == []


def test_a_non_positive_amount_points_at_the_sign_instead_of_the_direction(runtime, tmp_path):
    """收入和退款也都记**正数**,方向由 kind 决定。0 和负数一样挡掉、一样不落行。"""
    for bad in (0, -600):
        said = tool(runtime, "record_income")(amount=bad, kind="refund")
        assert "金额" in said and "kind" in said, said

    assert income_rows(tmp_path) == []


def test_an_unparseable_time_is_refused_and_records_nothing(runtime, tmp_path):
    """看不懂的时间不许悄悄退回"现在"(照 `record_expense` 的那条教训)。"""
    said = tool(runtime, "record_income")(amount=600, kind="refund", occurred_at="上周三")

    assert "上周三" in said and "YYYY-MM-DD" in said
    assert income_rows(tmp_path) == []


def test_absurdly_large_amount_returns_readable_hint_instead_of_escaping(runtime, tmp_path):
    """大到 SQLite 存不下的金额:`OverflowError` 不是 `sqlite3.Error`,会**逃出工具边界**。

    和 `record_expense` 同一个坑,所以同一道上界。E2 的意义正是边界上不推演可能性。
    """
    for bad in (1e17, 10**19):
        said = tool(runtime, "record_income")(amount=bad, kind="income")
        assert "金额" in said

    assert income_rows(tmp_path) == []


def test_record_expense_points_at_record_income_when_the_amount_is_negative(runtime, tmp_path):
    """★ 真机那一刻:记 -600 被挡回来。**校验照旧挡,但这次得指路。**

    上一版只说"要一个大于 0 的数字",于是用户的下一步是去动那两笔 GPT 的原始记录
    (她自己判断不该动,挂了三天)。负数支出几乎只有一个来头——想记退款或收入。
    0 和溢出不指路:那两个不是"方向搞反了",多一句话只是噪音。
    """
    said = tool(runtime, "record_expense")(amount=-600, category="其他")

    assert "record_income" in said, f"负数支出得指向那个新口子:{said}"
    assert expense_rows(tmp_path) == [], "校验放宽了——负数支出仍然不是一笔支出"
    assert "record_income" not in tool(runtime, "record_expense")(amount=0, category="其他")


@pytest.mark.parametrize(
    ("name", "kwargs"),
    [
        ("record_income", {"amount": 600, "kind": "refund", "of_expense_id": 10**19}),
        # 同一个洞,**在 M6-3 之前就在那儿**(M5-15 / M5-20 两个工具各一份)。写
        # `of_expense_id` 那道校验时撞上的:三处都是把模型给的 #id 直接交给 sqlite 绑定。
        # 只补新的那一处等于"同一个假设写在三处"(M5-8 的原话),所以三处一起修。
        ("amend_expense", {"expense_id": 10**19, "amount": 5}),
        ("delete_expense", {"expense_id": 10**19}),
    ],
)
def test_an_id_too_big_for_sqlite_is_a_plain_no_not_an_escaped_exception(gpt, name, kwargs):
    """模型给的 #id 大到 SQLite 存不下时,要回一句"没有这笔",不许把异常扔出工具边界。

    `10**19` 超出 int64,sqlite3 在**绑定参数时**抛 `OverflowError`——而它不是
    `sqlite3.Error` 的子类,`except sqlite3.Error` 接不住。和 `record_expense` 的金额
    上界(M4-2 补)是同一个坑的同一张脸:异常逃出去那条信封被标 failed 再冒泡,
    worker 活着但它是**无声**死的,模型连自我纠正的机会都没有。
    """
    said = tool(gpt, name)(**kwargs)

    assert "没有" in said and "list_recent" in said, said


@pytest.mark.parametrize("written", ["收入", "进账", "INCOME", " income "])
def test_kind_accepts_the_forms_the_model_actually_writes(runtime, tmp_path, written):
    """模型用中文思考。让它因为写了"收入"而吃一次 E2 往返是白烧钱(照 `group_by` 的做法)。

    **存下去的仍然是规范值**:库里混着"收入"和"income",按 kind 的合计就聚不出东西来。
    """
    said = tool(runtime, "record_income")(amount=3000, kind=written)

    assert "收入" in said and "不算在" in said, said
    assert [r["kind"] for r in income_rows(tmp_path)] == ["income"]


def test_the_income_note_goes_through_the_same_sanitizer_as_expense_notes(runtime):
    """备注是**模型写的文本**(不可信轮里它会把短信正文转述进去),所以过同一把刀。

    两套渲染器必然漂(P1-1:当前轮包了、历史轮没包),所以钉的是"同一把"不是"也有一把"。
    """
    tool(runtime, "record_income")(
        amount=600, kind="refund", occurred_at="2026-09-12 10:00", note="行一\n>>> 伪造\n行二"
    )

    listed = tool(runtime, "list_income")()

    assert "\n>>>" not in listed and ">>> 伪造" not in listed, listed
    assert listed.count("\n") == 1, f"备注里的换行伪造出了新的行:{listed}"


def test_income_amount_is_stored_as_integer_cents_without_float_drift(runtime, tmp_path):
    """金额存整数分,四舍五入定在 Decimal 上——和支出同一条规矩,同一个理由。

    这几个值是挑过的(见 `test_finance_record.py` 那条的说明):浮点路径在小数第三位
    才露馅,`1.005 * 100` = 100.49999999999999。
    """
    tool(runtime, "record_income")(amount=1.005, kind="income")
    tool(runtime, "record_income")(amount=33.333, kind="refund")

    assert [r["amount_cents"] for r in income_rows(tmp_path)] == [101, 3333]
    conn = sqlite3.connect(tmp_path / "finance" / "finance.sqlite")
    types = [r[0] for r in conn.execute("SELECT typeof(amount_cents) FROM income")]
    conn.close()
    assert types == ["integer"] * 2, "金额列必须是整数,不许是 REAL"


# ─────────────────────────── 状态位与迁移 ───────────────────────────


@pytest.mark.parametrize("sql", list(_INCOME_SQL.values()))
def test_every_income_query_filters_deleted_rows(sql: str) -> None:
    """给新表的状态位同一条机械保证(照 `test_every_listing_query_filters_deleted_rows`)。

    `deleted_at` 是照 M5-20 的形状(**不为新表发明第二套**);现在还没有 `delete_income`
    去写它(「先只做这两个」),但**每条查询从第一天就带着这个条件**——等那个工具来了,
    不需要回头逐条补,而"忘了补哪一条"的症状恰恰是「删了还在」。
    """
    assert "deleted_at IS NULL" in sql, f"这条查询会列出已删除的行:{sql}"


def test_the_database_that_is_already_on_the_server_grows_the_new_table(tmp_path):
    """★ 老库开库:新表建出来,**现有数据一行不动**,两个视图逐字节一致。

    金样是**改动前的代码**在这份库上跑出来的(M5-26 的口径)。不是断片段:多一行、
    少一行、顺序变了、某个数变了,都红(T6 第五种)。
    """
    make_legacy_database(tmp_path)

    runtime = build(tmp_path, timezone=SHANGHAI)

    assert tool(runtime, "list_recent")() == LEGACY_LIST
    assert tool(runtime, "query_spending")(**MONTH, group_by="category") == LEGACY_BY_CATEGORY
    assert tool(runtime, "query_spending")(**MONTH, group_by="day") == LEGACY_BY_DAY
    assert len(expense_rows(tmp_path)) == len(LEGACY_ROWS), "迁移动了行数"
    assert income_rows(tmp_path) == [], "新表建出来了,但里面不该有东西"
    assert tool(runtime, "list_income")() == "还没有记过收入或退款。"


def test_opening_the_migrated_database_again_is_a_no_op(tmp_path):
    """开第二次库不许把已经记下的收入冲掉(`CREATE TABLE IF NOT EXISTS` 的那半条)。"""
    make_legacy_database(tmp_path)
    first = build(tmp_path, timezone=SHANGHAI)
    tool(first, "record_income")(amount=600, kind="refund", occurred_at="2026-09-12 10:00")

    second = build(tmp_path, timezone=SHANGHAI)

    assert len(income_rows(tmp_path)) == 1, "重新开库把收入表清掉了"
    assert tool(second, "list_recent")() == LEGACY_LIST


def test_the_two_sides_live_in_two_tables(runtime, tmp_path):
    """新表,不是给 `expenses` 加符号位:收入和退款一行都不许落进 `expenses`。

    论证在 `server.py` 的表定义旁边。这条是它的机械保证——哪天有人"顺手统一"成
    一张表,`query_spending` 的 GROUP BY 就会分出一个带着无意义类目的收入组,
    而这条测试先红。
    """
    tool(runtime, "record_income")(amount=3000, kind="income")
    tool(runtime, "record_income")(amount=600, kind="refund")

    assert expense_rows(tmp_path) == [], "收入/退款落进了支出表"
    assert len(income_rows(tmp_path)) == 2
    assert tool(runtime, "list_recent")() == "还没有记过账。"
