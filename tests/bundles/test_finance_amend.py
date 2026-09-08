"""M5-26 `amend_expense`:记错了就地改,`#id` 不变。

## M5-15 那半条设计是错的,这次翻过来

M5-15 选的是「插一条新行 + 把旧行标成作废」,理由写着"留痕"。按 G6 问下来
——**谁会读到它?什么时候读?读到之后能做什么?**——那份痕迹没有读者:真机两天、
13 次工具调用,那个"连作废行一起看"的开关**一次都没有被调用过**。而"进过系统的
一切留痕"由起居注管着(不可协商第 3 条),账本里那份是第二份,从来没被读过。

代价倒是收到两张账单:

1. **删了的行还能被 amend 改活**——`amend` 不看 `deleted_at`,一个改金额的动作
   副作用是撤销一次删除。就地编辑之后这个洞根本不存在:改一条已删的行,它还是已删的。
2. 库里允许存在「自己作废自己、没有替代行」的形状(真机上真有一行)。列没了就构造不出来。

所以:`UPDATE` 就地改,表里还是一行,作废那一列整个拿掉。

## 这个文件里一个裸数字断言都没有(M5-25)

`list_recent` / `amend_expense` 渲染的是 `YYYY-MM-DD HH:MM`,而旧版本拿 `"49"`
`"22"` 这种**裸数字**断金额:23:49 记的那笔会假红(9-8 门禁真红过一次),
22:22 记的那笔会假绿(记 71 元、amend 一次都不调,`assert "22" in out` 照样过)。
断金额就断渲染出来的金额片段(`"22.00 元"`),或者直接断 `amount_cents`。
文件里几处**故意**把 `occurred_at` 摆在 23:49 / 22:22,让这两个坑长住在测试里。
"""

import contextlib
import sqlite3
from pathlib import Path

import pytest
from bundles.finance.server import build

SHANGHAI = "Asia/Shanghai"

# 老库里那一列的名字。M5-26 之后它只活在**迁移路径**上:这个文件、以及 server.py 里
# 那个把它退休掉的函数。真机的库跑过一次迁移之后,两边都可以删。
LEGACY_VOID_COLUMN = "voided_by"

# 照真机造的老库:12 行,其中 3 行已作废(#1、#3 被 #3 顶掉,#5 被 #6 顶掉),
# 剩下 9 行有效、合计 1055.03 元——**和真机当前值一致**。
LEGACY_DDL = (
    "CREATE TABLE expenses (id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " amount_cents INTEGER NOT NULL, category TEXT NOT NULL, occurred_at TEXT NOT NULL,"
    " note TEXT, created_at TEXT NOT NULL,"
    " voided_by INTEGER REFERENCES expenses(id), deleted_at TEXT, deleted_reason TEXT);"
)
LEGACY_ROWS = (
    (1, 2800, "交通", "2026-09-05T08:00:00", "打车", 3),
    (2, 4550, "餐饮", "2026-09-05T12:00:00", "午饭", None),
    (3, 2800, "交通", "2026-09-05T08:30:00", "打车(改过时间)", 3),
    (4, 1200, "日用", "2026-09-06T19:30:00", None, None),
    (5, 6600, "餐饮", "2026-09-07T20:00:00", None, 6),
    (6, 6600, "餐饮", "2026-09-07T20:00:00", "和朋友吃饭", None),
    (7, 80000, "医疗", "2026-09-08T10:00:00", "牙医", None),
    (8, 2353, "日用", "2026-09-08T21:00:00", None, None),
    (9, 3000, "交通", "2026-09-09T07:40:00", None, None),
    (10, 1800, "餐饮", "2026-09-09T12:10:00", "煎饼", None),
    (11, 4500, "娱乐", "2026-09-09T20:00:00", None, None),
    (12, 1500, "人情", "2026-09-09T22:22:00", "随份子", None),
)

# **金样是改动前的代码在这份老库上跑出来的**(不是我照着新代码誊的),
# 迁移之后一个字节都不许变。
LEGACY_LIST = "\n".join(
    (
        "最近 9 笔:",
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
LEGACY_TOTAL = "\n".join(
    (
        "2026-09-01 ~ 2026-09-30,共 9 笔,合计 1055.03 元(按类目):",
        "- 医疗 800.00 元(1 笔)",
        "- 餐饮 129.50 元(3 笔)",
        "- 娱乐 45.00 元(1 笔)",
        "- 日用 35.53 元(2 笔)",
        "- 交通 30.00 元(1 笔)",
        "- 人情 15.00 元(1 笔)",
    )
)


def tool(runtime, name: str):
    return next(f for f in runtime.tools if f.__name__ == name)


def all_rows(data_dir: Path) -> list[sqlite3.Row]:
    """**查全部行,含已删的**——这个文件关心的正是"表里到底有几行"。"""
    conn = sqlite3.connect(data_dir / "finance" / "finance.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM expenses ORDER BY id"))
    finally:
        conn.close()


def columns(data_dir: Path) -> set[str]:
    conn = sqlite3.connect(data_dir / "finance" / "finance.sqlite")
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(expenses)")}
    finally:
        conn.close()


def make_legacy_database(root: Path) -> None:
    conn = sqlite3.connect(root / "finance.sqlite")
    conn.executescript(LEGACY_DDL)
    conn.executemany(
        "INSERT INTO expenses (id, amount_cents, category, occurred_at, note, voided_by,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(*row, row[3]) for row in LEGACY_ROWS],
    )
    conn.commit()
    conn.close()


@pytest.fixture
def runtime(tmp_path):
    return build(tmp_path, timezone=SHANGHAI)


def only_id(runtime, tmp_path) -> int:
    return all_rows(tmp_path)[0]["id"]


def test_amending_changes_the_row_in_place_and_keeps_its_id(runtime, tmp_path):
    """★ 验收口径一:一次「记错 → 更正」之后**表里还是一行**,`id` / `created_at`
    一个字节没动,只有改的那个字段变了。

    行数那一条是这次改动的全部要害:插新行的实现在这里就红,而它正是被拆掉的那个。
    (occurred_at 摆在 23:49 是故意的——渲染出来带 `23:49`,裸数字断言当场露馅。)
    """
    tool(runtime, "record_expense")(
        amount=49, category="餐饮", occurred_at="2026-09-08 23:49", note="咖啡"
    )
    before = dict(all_rows(tmp_path)[0])

    out = tool(runtime, "amend_expense")(expense_id=before["id"], amount=22)

    rows = all_rows(tmp_path)
    assert len(rows) == 1, f"长出了第二行——就地编辑不该插新行:{[dict(r) for r in rows]}"
    after = dict(rows[0])
    assert after["amount_cents"] == 2200
    assert (after["id"], after["created_at"]) == (before["id"], before["created_at"])
    assert "49.00 元" in out and "22.00 元" in out, f"回话得说清楚改了什么:{out}"
    assert f"#{before['id']}" in out, f"#id 没变这件事要说出来,用户还要拿它做别的事:{out}"


def test_amend_carries_over_what_you_did_not_change(runtime, tmp_path):
    """只给要改的那一项,其余原样带过来——不然每次更正都要模型把四个字段重打一遍,
    而重打就是又一次抄错的机会。"""
    tool(runtime, "record_expense")(
        amount=49, category="餐饮", occurred_at="2026-09-01 12:30", note="咖啡"
    )

    tool(runtime, "amend_expense")(expense_id=only_id(runtime, tmp_path), amount=22)

    row = all_rows(tmp_path)[0]
    assert (row["category"], row["note"]) == ("餐饮", "咖啡")
    assert row["occurred_at"].startswith("2026-09-01T12:30")
    assert row["amount_cents"] == 2200


def test_amend_cannot_conjure_a_row_out_of_nothing(runtime, tmp_path):
    """★ 验收口径二:更正工具**不许凭空造出一笔**,只能作用在已存在的行上。

    能凭空造的话它就是第二个 `record_expense`,而且是个没有金额校验心智负担的
    ——模型会拿它当后门用。
    """
    out = tool(runtime, "amend_expense")(expense_id=999, amount=22)

    assert all_rows(tmp_path) == []
    assert "999" in out and ("没有" in out or "找不到" in out)


def test_amending_a_deleted_row_is_refused_and_points_at_recording_a_new_one(runtime, tmp_path):
    """★ 原 M5-20 補:已删的行仍然拒绝 amend,理由变了——不再是"会复活"
    (就地编辑之后复活不了),而是**"改了你也看不见"**。

    回话**指向 `record_expense`,不许指向 `undo`**:「删了的账要改成别的数」不是撤销,
    是记一笔新的;绕去恢复只会在账本上多两条没意义的痕迹。
    """
    tool(runtime, "record_expense")(
        amount=49, category="餐饮", occurred_at="2026-09-08 22:22", note="咖啡"
    )
    gone = only_id(runtime, tmp_path)
    tool(runtime, "delete_expense")(expense_id=gone, reason="测试记的")
    before = [dict(r) for r in all_rows(tmp_path)]

    out = tool(runtime, "amend_expense")(expense_id=gone, amount=71)

    assert [dict(r) for r in all_rows(tmp_path)] == before, "账上动了——已删的行不该被改"
    assert "record_expense" in out, f"得告诉它该走哪条路:{out}"
    assert "undo" not in out, f"把人指去恢复,账本上就多两条没意义的痕迹:{out}"


def test_amending_works_again_after_the_delete_is_undone(runtime, tmp_path):
    """撤回之后这行又在账上了,当然还能改——拒绝的判据是"现在删着",不是"删过"。"""
    tool(runtime, "record_expense")(amount=49, category="餐饮", occurred_at="2026-09-01 12:30")
    back = only_id(runtime, tmp_path)
    tool(runtime, "delete_expense")(expense_id=back, reason="手滑")
    tool(runtime, "delete_expense")(expense_id=back, undo=True)

    out = tool(runtime, "amend_expense")(expense_id=back, amount=22)

    assert all_rows(tmp_path)[0]["amount_cents"] == 2200, out
    assert "22.00 元" in out, out


def test_a_bad_new_amount_changes_nothing(runtime, tmp_path):
    """E2:新金额不合法就整条不动——**先校验再动手**,不许写一半。"""
    tool(runtime, "record_expense")(amount=49, category="餐饮", occurred_at="2026-09-01 12:30")
    before = [dict(r) for r in all_rows(tmp_path)]

    out = tool(runtime, "amend_expense")(expense_id=before[0]["id"], amount=-27)

    assert [dict(r) for r in all_rows(tmp_path)] == before, "校验没过却已经改了库"
    assert "没改" in out or "这笔没" in out


def test_a_bad_new_category_changes_nothing(runtime, tmp_path):
    """类目那条校验同理,而且它在金额之后——两条都要各钉一次,不然改动顺序时会漏。"""
    tool(runtime, "record_expense")(amount=49, category="餐饮", occurred_at="2026-09-01 12:30")
    before = [dict(r) for r in all_rows(tmp_path)]

    out = tool(runtime, "amend_expense")(expense_id=before[0]["id"], category="吃饭")

    assert [dict(r) for r in all_rows(tmp_path)] == before, "非法类目却已经写进库了"
    assert "吃饭" in out and "这笔没改" in out


def test_totals_by_category_follow_the_amended_amount(runtime, tmp_path):
    """★ 验收口径三之一:`query_spending` 按类目的合计,改完就是新数,旧数一分不留。

    断的是**渲染出来的金额片段**而不是裸数字:`"49"` 会被 `2026-09-08 23:49` 里的
    时间戳喂饱(M5-25),而合计行里根本没有时间戳时它又什么都挡不住。
    """
    tool(runtime, "record_expense")(amount=49, category="餐饮", occurred_at="2026-09-01 23:49")
    tool(runtime, "amend_expense")(expense_id=only_id(runtime, tmp_path), amount=22)

    out = tool(runtime, "query_spending")(
        since="2026-09-01", until="2026-09-30", group_by="category"
    )

    assert "合计 22.00 元" in out and "餐饮 22.00 元" in out, out
    assert "49.00" not in out and "71.00" not in out, f"旧金额还算在合计里:{out}"


def test_totals_by_day_follow_the_amended_amount(runtime, tmp_path):
    """★ 验收口径三之二:按天那条分支也得跟着走。

    两条聚合 SQL 是分开写死的字面量(不拼列名),**改条件时会漏掉一条**——而漏掉的
    表现是"按类目对、按天不对",没有任何报错,只有月底对账时的一句"怎么又不一样"。
    这一节栽过一次,所以两个出口各钉一次。
    """
    tool(runtime, "record_expense")(amount=49, category="餐饮", occurred_at="2026-09-01 23:49")
    tool(runtime, "amend_expense")(expense_id=only_id(runtime, tmp_path), amount=22)

    out = tool(runtime, "query_spending")(since="2026-09-01", until="2026-09-30", group_by="day")

    assert "合计 22.00 元" in out and "2026-09-01 22.00 元" in out, out
    assert "49.00" not in out and "71.00" not in out, f"旧金额还算在合计里:{out}"


def test_list_recent_shows_the_amended_row_under_the_same_id(runtime, tmp_path):
    """★ 验收口径三之三:列表这个出口也一起断,而且**#id 还是那一个**
    ——用户接下来可能还要拿这个号做别的事。"""
    tool(runtime, "record_expense")(
        amount=49, category="餐饮", occurred_at="2026-09-08 23:49", note="咖啡"
    )
    kept = only_id(runtime, tmp_path)
    tool(runtime, "amend_expense")(expense_id=kept, amount=22)

    listed = tool(runtime, "list_recent")()

    assert listed.count("\n") == 1, f"列表里冒出了第二行流水:{listed}"
    assert f"- #{kept} 2026-09-08 23:49 餐饮 22.00 元" in listed, listed
    assert "49.00 元" not in listed, f"旧金额还列着:{listed}"


def test_list_recent_shows_an_id_you_can_point_at(runtime, tmp_path):
    """★ `list_recent` 要吐出可指认的 id。

    没有它,用户说「第三笔记错了」时模型手上没有任何可指的东西——只能靠金额和备注去猜,
    而这正是更正功能能不能被用起来的前提。**吐出来的 id 必须真的能喂回 amend_expense。**
    """
    tool(runtime, "record_expense")(amount=49, category="餐饮", note="咖啡")
    listed = tool(runtime, "list_recent")()

    shown = all_rows(tmp_path)[0]["id"]
    assert f"#{shown}" in listed, f"列表里没有可指认的 id:{listed}"

    out = tool(runtime, "amend_expense")(expense_id=shown, amount=22)
    assert "没有" not in out and "找不到" not in out, f"吐出来的 id 喂不回去:{out}"


def test_no_tool_ever_removes_a_row_from_the_table(runtime, tmp_path):
    """这条原来叫 `test_finance_still_offers_no_way_to_delete`,断的是"没有 delete 这个
    工具"。**那个理由站不住,而这条测试把一个设计错误焊死了整整一个里程碑**(M5-20)。

    真正该断的是**没有任何一条路会让行从表里消失**——`delete_expense` 打的是状态位,
    `amend_expense` 就地改,两条路一行都不少。M5-26 之后还多一条:**也不许多**。
    """
    tool(runtime, "record_expense")(amount=49, category="餐饮")
    tool(runtime, "record_expense")(amount=28, category="交通")
    seen = {r["id"] for r in all_rows(tmp_path)}

    for fn in runtime.tools:
        for expense_id in sorted(seen):
            with contextlib.suppress(TypeError):  # 缺必填参数的工具自然拒收
                fn(expense_id=expense_id)  # type: ignore[call-arg]

    # 盲扫会把两行都删掉(`delete_expense` 收得下光一个 expense_id),而已删的行
    # `amend` 是拒收的——不撤回就等于**下面那两句根本没跑到写入路径**(T6 第三种)。
    for expense_id in sorted(seen):
        tool(runtime, "delete_expense")(expense_id=expense_id, undo=True)
    tool(runtime, "amend_expense")(expense_id=min(seen), amount=1)
    tool(runtime, "delete_expense")(expense_id=max(seen), reason="删掉试试")

    assert {r["id"] for r in all_rows(tmp_path)} == seen, "有工具让行从表里消失、或者凭空多出来"


def test_an_old_database_retires_its_voided_rows_without_moving_the_books(tmp_path):
    """★ 迁移的硬口径:**用户看到的账,迁移前后逐字节一致**。

    `voided_by` 一拿掉,`voided_by IS NULL` 这个过滤条件就没了,真机上那 3 行已作废的
    会**重新冒到账上**——账上凭空多三笔,而用户不会知道为什么。所以删列之前先把它们
    标成已删:仍然查得到(`include_deleted=True`),但不进任何视图、不进任何合计。

    金样(`LEGACY_LIST` / `LEGACY_TOTAL`)是**改动前的代码**在这份老库上跑出来的,
    不是照着新代码誊的。断全量而不是断片段:多一行、少一行、顺序变了都得红(T6 第五种)。
    """
    root = tmp_path / "finance"
    root.mkdir(parents=True)
    make_legacy_database(root)

    runtime = build(tmp_path, timezone=SHANGHAI)

    assert tool(runtime, "list_recent")() == LEGACY_LIST
    assert (
        tool(runtime, "query_spending")(since="2026-09-01", until="2026-09-30", group_by="category")
        == LEGACY_TOTAL
    )
    rows = all_rows(tmp_path)
    assert len(rows) == len(LEGACY_ROWS), "迁移把行删掉了——留痕是不可协商第 3 条"
    retired = {r["id"]: r["deleted_reason"] for r in rows if r["deleted_at"] is not None}
    assert set(retired) == {1, 3, 5}, f"该退休的和实际退休的对不上:{retired}"
    assert all("迁移" in reason for reason in retired.values()), retired
    assert LEGACY_VOID_COLUMN not in columns(tmp_path), "列还在,那个形状就还构造得出来"


def test_the_retired_rows_are_still_findable_after_the_migration(tmp_path):
    """退休不是抹掉:`include_deleted=True` 还看得见,而且标着「已删除」+ 迁移的理由
    ——用户哪天问"我那笔 28 的打车呢",得有地方答得上来。"""
    root = tmp_path / "finance"
    root.mkdir(parents=True)
    make_legacy_database(root)

    runtime = build(tmp_path, timezone=SHANGHAI)
    listed = tool(runtime, "list_recent")(limit=20, include_deleted=True)

    assert "#1 " in listed and "#3 " in listed and "#5 " in listed, listed
    assert listed.count("已删除") == 3, listed


def test_opening_a_migrated_database_again_is_a_no_op(tmp_path):
    """迁移要能重复开库——第二次开的时候列已经没了,不许炸,也不许再动一次账。"""
    root = tmp_path / "finance"
    root.mkdir(parents=True)
    make_legacy_database(root)
    build(tmp_path, timezone=SHANGHAI)
    once = [dict(r) for r in all_rows(tmp_path)]

    runtime = build(tmp_path, timezone=SHANGHAI)

    assert [dict(r) for r in all_rows(tmp_path)] == once
    assert tool(runtime, "list_recent")() == LEGACY_LIST


def test_a_migrated_row_can_still_be_amended(tmp_path):
    """迁移之后账照记照改——退休的是列,不是这张表。"""
    root = tmp_path / "finance"
    root.mkdir(parents=True)
    make_legacy_database(root)

    runtime = build(tmp_path, timezone=SHANGHAI)
    out = tool(runtime, "amend_expense")(expense_id=2, amount=50)

    assert "50.00 元" in out and "#2" in out, out
    assert [r["amount_cents"] for r in all_rows(tmp_path) if r["id"] == 2] == [5000]
    assert len(all_rows(tmp_path)) == len(LEGACY_ROWS), "改一笔长出了新行"
