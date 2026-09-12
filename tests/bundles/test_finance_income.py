"""M6-3 `record_income` / `list_income`,以及 M6-3a **把退款并进收入**之后剩下的那一半。

## 为什么有这个口子

真机上那 600 元 ChatGPT 退款**记不进去**,在一个话头里挂了三天。用户的原话:

> 「我的账本只有"花出去"这一个口子,没有收入、没有退款。刚试了记 -600,系统直接挡回来:
> "要一个大于 0 的数字"。所以要么等你给我补个能记退款的口子,要么我按冲抵处理
> ——去动那两笔 GPT 的账。**但那样底稿就不是原始记录了,我不建议,先挂着。**」

她的处置全对,而**那道校验也是对的**:负数支出不是退款,是一笔坏数据。所以补的不是
"放宽校验",是一张新表加两个工具;`record_expense` 收到负数时改成**指路**(`record_income`)。

## M6-3a:退款不再是一个单独的东西

M6-3 给这张表加了 `kind`(refund | income)和 `of_expense_id`(冲抵哪一笔),
「花了多少」减 refund、不减 income。用户验收完当天把这一半推翻了:

> 「我觉得没必要退款啊,**很难说全退**,所以我觉得没啥用。退款就弄成收入就行。」

**他的理由比 M6-3 里写的任何一条都硬**:`of_expense_id`「冲抵某一笔支出」这个建模
**假设退款是全额的**。真实退款经常是部分退、几笔合并退、退到代金券,那个指针大多数时候
指不准;而「抵掉退款后实际花掉 Z」这个数**整个建在那个指针上**——前提不成立,派生出来
的数就**比没有更坏**:一个读起来很确定的数字,底下是个猜的对应关系。

于是这里只剩一个数、一个口径:**收入不算在「花了多少」里**。退款到账也记成收入。
本文件被删掉的那些测试(退款怎么减、指针怎么校验、悬空引用怎么说出口)在交付报告里
逐条交代过——它们钉的行为不是"红了",是**不存在了**。
"""

import sqlite3
from pathlib import Path

import pytest
from bundles.finance.server import _INCOME_SQL, build

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
# 建了 income 表之后,这三份输出一个字节都不许变;M6-3a 把退款那一摊拆掉之后,
# **它们还是这三份**——基线就是 M6-3 之前那个提交(50fde56)。
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

# ── M6-3a 的"老库":M6-3 已经推上真机的那张 `income` 表(带 `kind` / `of_expense_id`)。
# 真机那张是 0 行,但**有行的情况也要对**,所以这里塞的是最难看的三行:一条退款指着
# **已删**的 #1(`record_income` 拒绝这么记,而从 `delete_expense` 那个方向进来照样能
# 落成这样——M6-3 验收时抓到的正是这个)、一条退款指着活着的 #13、一条普通收入。
M63_INCOME_DDL = (
    "CREATE TABLE income (id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " amount_cents INTEGER NOT NULL, kind TEXT NOT NULL, occurred_at TEXT NOT NULL,"
    " note TEXT, of_expense_id INTEGER, created_at TEXT NOT NULL,"
    " deleted_at TEXT, deleted_reason TEXT);"
    "CREATE INDEX idx_income_occurred_at ON income(occurred_at);"
)
# (id, 分, kind, occurred_at, 备注, of_expense_id, deleted_at)
M63_INCOME_ROWS = (
    (1, 60000, "refund", "2026-09-12T10:00:00", "ChatGPT 退的", 13, None),
    (2, 300000, "income", "2026-09-15T20:00:00", "妈妈打的", None, None),
    (3, 1500, "refund", "2026-09-13T11:00:00", "打车退的", 1, None),
)


def tool(runtime, name: str):
    return next(f for f in runtime.tools if f.__name__ == name)


def income_rows(data_dir: Path) -> list[sqlite3.Row]:
    """查全部收入行,含已删的。"""
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


def income_columns(data_dir: Path) -> list[str]:
    conn = sqlite3.connect(data_dir / "finance" / "finance.sqlite")
    try:
        return [row[1] for row in conn.execute("PRAGMA table_info(income)")]
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


def make_m63_database(root: Path) -> None:
    """M6-3 形状的库:13 行支出 + 一张带 `kind` / `of_expense_id` 的 `income` 表。"""
    make_legacy_database(root)
    conn = sqlite3.connect(root / "finance" / "finance.sqlite")
    conn.executescript(M63_INCOME_DDL)
    conn.executemany(
        "INSERT INTO income (id, amount_cents, kind, occurred_at, note, of_expense_id,"
        " deleted_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(*row, row[3]) for row in M63_INCOME_ROWS],
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


# ─────────────────────── ★ M6-3a 的硬口径:支出侧回到基线 ───────────────────────


def test_the_expense_side_reads_byte_for_byte_as_the_baseline_commit_printed(tmp_path):
    """★ 支出侧三个视图回到 **M6-3 之前(50fde56)逐字节一样**。

    库里躺着三条"退款"——其中一条还指着一笔已删的支出——而这三份输出里**一个字都不许
    提到它们**:退款并进收入之后,「花了多少」只回答支出,不再被任何东西改写。
    这也是 M6-3a 唯一一条"拆之前必然红"的测试:拆之前这里会多出一行
    「同期收到退款 615.00 元(2 笔):支出 1555.03 元,抵掉退款后实际花掉 940.03 元。」

    金样的出处(照 M5-26 / M6-3 立的口径:**金样必须是旧代码产出的,不是照着新代码誊的**):
    用 `importlib.util.spec_from_file_location` 把 `git show 50fde56:bundles/finance/server.py`
    当一个独立模块加载,在**同一份库**(带 income 表、带那三行)上跑同样三次调用,输出就是
    上面那三个常量——它们从 M6-3 起一个字节没动,而基线代码压根不认识 income 表,
    所以"库里有没有退款行"对它没有任何影响(这一条脚本也验了)。

    为什么那段 importlib 不搬进本文件:它要么得 `subprocess` 去调 git(全局约束里
    「无 shell」的反面,而且浅克隆下那个 commit 可能压根不在),要么得把 640 行旧代码抄进
    仓库当 fixture(G2 换个姿势)。所以走 M5-26 那条路——**金样冻在这里,产出它的脚本
    在交付报告里交代**。
    """
    make_m63_database(tmp_path)

    runtime = build(tmp_path, timezone=SHANGHAI)

    assert tool(runtime, "list_recent")() == LEGACY_LIST
    assert tool(runtime, "query_spending")(**MONTH, group_by="category") == LEGACY_BY_CATEGORY
    assert tool(runtime, "query_spending")(**MONTH, group_by="day") == LEGACY_BY_DAY


def test_income_does_not_touch_what_you_spent(gpt, tmp_path):
    """★ 剩下的那一条口径:记一笔收入 → 「花了多少」**一个字不变**、底稿一个字不变。

    金额取 3000——比支出总额大一个数量级,漏进去藏不住。断的是**整份输出相等**,
    不断片段:少一行、多一行、数字对了口径错了,都红(T6 第五种)。
    """
    before_total = tool(gpt, "query_spending")(**MONTH, group_by="category")
    before_day = tool(gpt, "query_spending")(**MONTH, group_by="day")
    before_list = tool(gpt, "list_recent")()
    rows_before = [dict(r) for r in expense_rows(tmp_path)]

    said = tool(gpt, "record_income")(amount=3000, occurred_at="2026-09-10 15:00", note="生活费")

    assert said == (
        "记好了:收入 3000.00 元(09-10 15:00) · 备注「生活费」。收入不算在「花了多少」里。"
    ), said
    assert tool(gpt, "query_spending")(**MONTH, group_by="category") == before_total
    assert tool(gpt, "query_spending")(**MONTH, group_by="day") == before_day
    assert tool(gpt, "list_recent")() == before_list
    assert [dict(r) for r in expense_rows(tmp_path)] == rows_before, "expenses 表被动了"
    assert len(income_rows(tmp_path)) == 1, "收入没落进自己那张表"


# ─────────────────────────── 那一个数怎么说出口 ───────────────────────────


def test_list_income_says_the_total_with_the_caliber_spelled_out(gpt):
    """★ `list_income` 只剩一个数:合计 + 笔数,**而口径那半句必须跟着**。

    少了「收入不算在「花了多少」里」,「3000」这个数就没有单位——而月度复盘里
    「花了多少」和「进了多少」是两个数、两套算法,说错一个整段复盘就是错的。
    M6-3a 之后这半句是这个工具仅存的两个存在理由之一(另一个是那个全区间合计)。
    """
    tool(gpt, "record_income")(amount=3000, occurred_at="2026-09-10 15:00", note="生活费")
    tool(gpt, "record_income")(amount=600, occurred_at="2026-09-12 10:00", note="ChatGPT 退的")

    assert tool(gpt, "list_income")(**MONTH) == "\n".join(
        (
            "2026-09-01 ~ 2026-09-30 收入 3600.00 元(2 笔)。收入不算在「花了多少」里。",
            "- 2026-09-12 10:00 收入 600.00 元 · 备注「ChatGPT 退的」",
            "- 2026-09-10 15:00 收入 3000.00 元 · 备注「生活费」",
        )
    )


def test_list_income_on_an_empty_table_does_not_claim_the_ledger_is_empty(runtime):
    """ "这段没有" ≠ "一笔都没记过"(照 `list_recent` 的那条教训,别混成一句)。"""
    assert tool(runtime, "list_income")() == "还没有记过收入。"
    assert tool(runtime, "list_income")(**MONTH) == "2026-09-01 ~ 2026-09-30 没有收入。"


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
        tool(runtime, "record_income")(amount=100, occurred_at=f"2026-09-{day:02d} 10:00")

    # limit=50 顺手把硬封顶一起断了:钳到 20,不是"limit 说多少给多少"。
    said = tool(runtime, "list_income")(limit=50, **MONTH)

    assert "收入 2200.00 元(22 笔)" in said, f"合计只算了列出来的那几行:{said}"
    assert len(said.splitlines()) == 22, f"20 行流水 + 表头 + 截断说明:{said}"
    assert "还有 2 笔更早的没列出来" in said, f"截断没说出口:{said}"


# ─────────────────────────── E2:工具边界不许抛 ───────────────────────────


def test_a_non_positive_amount_is_refused_and_records_nothing(runtime, tmp_path):
    """收入也记**正数**。0 和负数一样挡掉、一样不落行(照 `record_expense` 那条)。"""
    for bad in (0, -600):
        said = tool(runtime, "record_income")(amount=bad)
        assert "金额" in said and "这笔没记" in said, said

    assert income_rows(tmp_path) == []


def test_an_unparseable_time_is_refused_and_records_nothing(runtime, tmp_path):
    """看不懂的时间不许悄悄退回"现在"(照 `record_expense` 的那条教训)。"""
    said = tool(runtime, "record_income")(amount=600, occurred_at="上周三")

    assert "上周三" in said and "YYYY-MM-DD" in said
    assert income_rows(tmp_path) == []


def test_absurdly_large_amount_returns_readable_hint_instead_of_escaping(runtime, tmp_path):
    """大到 SQLite 存不下的金额:`OverflowError` 不是 `sqlite3.Error`,会**逃出工具边界**。

    和 `record_expense` 同一个坑,所以同一道上界。E2 的意义正是边界上不推演可能性。
    """
    for bad in (1e17, 10**19):
        said = tool(runtime, "record_income")(amount=bad)
        assert "金额" in said

    assert income_rows(tmp_path) == []


def test_record_expense_points_at_record_income_when_the_amount_is_negative(runtime, tmp_path):
    """★ 真机那一刻:记 -600 被挡回来。**校验照旧挡,但这次得指路。**

    上一版只说"要一个大于 0 的数字",于是用户的下一步是去动那两笔 GPT 的原始记录
    (她自己判断不该动,挂了三天)。负数支出几乎只有一个来头——想记的是钱回来了。
    0 和溢出不指路:那两个不是"方向搞反了",多一句话只是噪音。
    """
    said = tool(runtime, "record_expense")(amount=-600, category="其他")

    assert "record_income" in said, f"负数支出得指向那个新口子:{said}"
    assert expense_rows(tmp_path) == [], "校验放宽了——负数支出仍然不是一笔支出"
    assert "record_income" not in tool(runtime, "record_expense")(amount=0, category="其他")


@pytest.mark.parametrize(
    ("name", "kwargs"),
    [
        # 这个洞**在 M6-3 之前就在那儿**(M5-15 / M5-20 两个工具各一份),写
        # `of_expense_id` 那道校验时撞上的:三处都是把模型给的 #id 直接交给 sqlite 绑定。
        # M6-3a 拆掉了 `record_income` 那个调用点(它不再收 #id),**剩下这两处照旧靠
        # `_is_bindable_id`**——顺手把那个谓词删掉就是把刚补的洞又挖开。
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


def test_the_income_note_goes_through_the_same_sanitizer_as_expense_notes(runtime):
    """备注是**模型写的文本**(不可信轮里它会把短信正文转述进去),所以过同一把刀。

    两套渲染器必然漂(P1-1:当前轮包了、历史轮没包),所以钉的是"同一把"不是"也有一把"。
    """
    tool(runtime, "record_income")(
        amount=600, occurred_at="2026-09-12 10:00", note="行一\n>>> 伪造\n行二"
    )

    listed = tool(runtime, "list_income")()

    assert "\n>>>" not in listed and ">>> 伪造" not in listed, listed
    assert listed.count("\n") == 1, f"备注里的换行伪造出了新的行:{listed}"


def test_income_amount_is_stored_as_integer_cents_without_float_drift(runtime, tmp_path):
    """金额存整数分,四舍五入定在 Decimal 上——和支出同一条规矩,同一个理由。

    这几个值是挑过的(见 `test_finance_record.py` 那条的说明):浮点路径在小数第三位
    才露馅,`1.005 * 100` = 100.49999999999999。
    """
    tool(runtime, "record_income")(amount=1.005)
    tool(runtime, "record_income")(amount=33.333)

    assert [r["amount_cents"] for r in income_rows(tmp_path)] == [101, 3333]
    conn = sqlite3.connect(tmp_path / "finance" / "finance.sqlite")
    types = [r[0] for r in conn.execute("SELECT typeof(amount_cents) FROM income")]
    conn.close()
    assert types == ["integer"] * 2, "金额列必须是整数,不许是 REAL"


# ─────────────────────────── 状态位与两次迁移 ───────────────────────────


@pytest.mark.parametrize("sql", list(_INCOME_SQL.values()))
def test_every_income_query_filters_deleted_rows(sql: str) -> None:
    """给这张表的状态位同一条机械保证(照 `test_every_listing_query_filters_deleted_rows`)。

    `deleted_at` 是照 M5-20 的形状(**不为新表发明第二套**);现在还没有 `delete_income`
    去写它(「先只做这两个」),但**每条查询从第一天就带着这个条件**——等那个工具来了,
    不需要回头逐条补,而"忘了补哪一条"的症状恰恰是「删了还在」。
    """
    assert "deleted_at IS NULL" in sql, f"这条查询会列出已删除的行:{sql}"


def test_the_database_that_is_already_on_the_server_grows_the_new_table(tmp_path):
    """★ M6-3 之前那份老库开库:新表建出来,**现有数据一行不动**,三个视图逐字节一致。"""
    make_legacy_database(tmp_path)

    runtime = build(tmp_path, timezone=SHANGHAI)

    assert tool(runtime, "list_recent")() == LEGACY_LIST
    assert tool(runtime, "query_spending")(**MONTH, group_by="category") == LEGACY_BY_CATEGORY
    assert tool(runtime, "query_spending")(**MONTH, group_by="day") == LEGACY_BY_DAY
    assert len(expense_rows(tmp_path)) == len(LEGACY_ROWS), "迁移动了行数"
    assert income_rows(tmp_path) == [], "新表建出来了,但里面不该有东西"
    assert tool(runtime, "list_income")() == "还没有记过收入。"


def test_the_two_retired_columns_turn_every_refund_row_into_plain_income(tmp_path):
    """★ M6-3a 的退休手续:`kind` / `of_expense_id` 两列拿掉,**三行一条不少**。

    `CREATE TABLE IF NOT EXISTS` 对已经建出来的表是空操作,所以必须有一步真的手续
    (`_retire_the_refund_columns`)。真机那张表是 0 行,但**有行的情况也要对**:一条
    refund 行拿掉这两列就是一条收入行——而那正是新口径要的意思,不是凑合。所以这里断的是
    「都当收入列出来、金额和时间一个字节不差、`id` 没变、`deleted_at` 没被动」。

    最后那句 `record_income` 不是顺手加的:不办这道手续时 `kind TEXT NOT NULL` 还在,
    而新代码不写它——**每一笔新收入都撞 NOT NULL**,用户收到的是「这笔没记进去」。
    那是"迁移整个不跑"在真机上的确切症状,所以它必须在断言里。
    """
    make_m63_database(tmp_path)

    runtime = build(tmp_path, timezone=SHANGHAI)

    assert income_columns(tmp_path) == [
        "id",
        "amount_cents",
        "occurred_at",
        "note",
        "created_at",
        "deleted_at",
        "deleted_reason",
    ]
    assert tool(runtime, "list_income")() == "\n".join(
        (
            "收入 3615.00 元(3 笔)。收入不算在「花了多少」里。",
            "- 2026-09-15 20:00 收入 3000.00 元 · 备注「妈妈打的」",
            "- 2026-09-13 11:00 收入 15.00 元 · 备注「打车退的」",
            "- 2026-09-12 10:00 收入 600.00 元 · 备注「ChatGPT 退的」",
        )
    )
    rows = income_rows(tmp_path)
    assert [r["id"] for r in rows] == [1, 2, 3], "id 变了——用户记着的号不许动"
    assert [r["amount_cents"] for r in rows] == [60000, 300000, 1500]
    assert [r["occurred_at"] for r in rows] == [row[3] for row in M63_INCOME_ROWS]
    assert [r["note"] for r in rows] == [row[4] for row in M63_INCOME_ROWS]
    assert [r["deleted_at"] for r in rows] == [None] * 3, "迁移动了状态位"
    assert "记好了" in tool(runtime, "record_income")(amount=100, occurred_at="2026-09-20 10:00")


def test_opening_the_migrated_database_again_is_a_no_op(tmp_path):
    """开第二次库不许再退休一次(那两列已经没了,再 DROP 一次是 OperationalError),
    更不许把已经记下的收入冲掉。**幂等靠的是那次 `PRAGMA table_info` 探测**,
    不是"反正只跑一次"。"""
    make_m63_database(tmp_path)
    first = build(tmp_path, timezone=SHANGHAI)
    tool(first, "record_income")(amount=600, occurred_at="2026-09-20 10:00")

    second = build(tmp_path, timezone=SHANGHAI)

    assert len(income_rows(tmp_path)) == len(M63_INCOME_ROWS) + 1, "重新开库把收入冲掉了"
    assert tool(second, "list_recent")() == LEGACY_LIST
    assert "kind" not in income_columns(tmp_path)


def test_the_two_sides_live_in_two_tables(runtime, tmp_path):
    """新表,不是给 `expenses` 加符号位:收入一行都不许落进 `expenses`。

    论证在 `server.py` 的表定义旁边。这条是它的机械保证——哪天有人"顺手统一"成
    一张表,`query_spending` 的 GROUP BY 就会分出一个带着无意义类目的收入组,
    而这条测试先红。
    """
    tool(runtime, "record_income")(amount=3000)
    tool(runtime, "record_income")(amount=600)

    assert expense_rows(tmp_path) == [], "收入落进了支出表"
    assert len(income_rows(tmp_path)) == 2
    assert tool(runtime, "list_recent")() == "还没有记过账。"
