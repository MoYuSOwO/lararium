"""M5-20 `delete_expense`:记错了能删,而且删了能拿回来。

## 为什么要有它(M5-15 我设计错了)

M5-15 写死"不给 delete",理由是「删除是唯一会彻底销毁信息的操作」。**那个理由站不住**
——作废机制本身不销毁任何东西。真正被混成一件事的是「改错了金额」和「这笔根本不该
存在」,而我只给了前者一条路。

真机第一条正经记账就撞上了:用户说"那之前的那个作废",模型手里只有 `amend`,于是调了
`amend_expense(1, note="测试作废:不计入")`——旧行标作废、**新行金额原样、仍然有效**,
然后它回话"已经标作废了"。失效形态是最坏的一种:**用户以为删了、账上还在、
而模型也真心以为自己办了。**

所以这个工具**就叫 delete**,不叫 void:上一次失败正是因为没有一个看起来像"删"的
东西,模型才去拿 amend 顶。名字越像人话,它越找得到。底下打的仍然是状态位
(`deleted_at`),一行都不真删——那半条设计 M5-15 是对的。

**M5-26 之后 `deleted_at` 是这张表唯一的状态位**:作废那一列整个拆了(那份"留痕"
没有读者,而留痕这件事起居注在干),`amend_expense` 改成就地 `UPDATE`。于是本文件里
「被 amend 顶掉的旧行」那一类场景不再存在,连同它那条测试一起删了——**那个形状
现在构造不出来**,留着一条永远走不到的分支只会让下一个人以为它还在。
"""

import sqlite3
from pathlib import Path

import pytest
from bundles.finance.server import _GROUP_SQL, _RECENT_SQL, build

SHANGHAI = "Asia/Shanghai"


def tool(runtime, name: str):
    return next(f for f in runtime.tools if f.__name__ == name)


def all_rows(data_dir: Path) -> list[sqlite3.Row]:
    """**查全部行,含删掉的**——这个文件关心的正是"删掉的行还在不在表里"。"""
    conn = sqlite3.connect(data_dir / "finance" / "finance.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM expenses ORDER BY id"))
    finally:
        conn.close()


@pytest.fixture
def runtime(tmp_path):
    return build(tmp_path, timezone=SHANGHAI)


@pytest.fixture
def one(runtime, tmp_path):
    """记一笔 28 块的打车(真机上惹祸的就是它),返回它的 id。"""
    tool(runtime, "record_expense")(
        amount=28, category="交通", occurred_at="2026-09-01 08:00", note="打车"
    )
    return all_rows(tmp_path)[0]["id"]


def test_a_deleted_row_leaves_every_view_and_the_total(runtime, tmp_path, one):
    """★ 验收口径一:删掉之后**三个视图一起**变干净,而**行还在表里**。

    三个都要断。真机上炸的正是"其中一个没变":`amend` 把旧行标了作废,
    `list_recent` 于是不列它了——看起来办成了——而新行的 28 块原样计入合计。
    只断 list_recent 的测试会给这个实现开绿灯。
    """
    tool(runtime, "record_expense")(amount=45.5, category="餐饮", occurred_at="2026-09-01 12:00")

    out = tool(runtime, "delete_expense")(expense_id=one, reason="测试记的,不算")

    listed = tool(runtime, "list_recent")()
    total = tool(runtime, "query_spending")(
        since="2026-09-01", until="2026-09-01", group_by="category"
    )
    assert f"#{one}" not in listed, f"删了还列出来:{listed}"
    assert "28.00" not in total and "45.50" in total, f"合计还算着删掉的那笔:{total}"
    assert "交通" not in total, f"按类目还分出删掉那笔的组:{total}"
    assert len(all_rows(tmp_path)) == 2, "行被真删了——留痕是不可协商第 3 条"
    assert "28.00 元" in out, f"回话得说清楚删的是哪笔:{out}"


def test_the_reply_reads_exactly_as_it_did_before_m6_3(runtime, one):
    """这句回话的**逐字节原始记录**。上面那几条钉的都是片段("28.00 元" 在不在),
    而整句话一个字节没人钉——M6-3 就是这么往里加东西的。

    M6-3 验收时在「合计里不算它了」后面追加过一句「有 N 笔退款指着这笔、它还在从
    「花了多少」里减」,理由是那会儿 `record_income` 的 `of_expense_id` 能让一条退款
    悬空。**M6-3a 把那半句删了**:没有指针就没有悬空,它报告的那个事实不存在了
    (G6:先问它该不该在)。于是这句话回到 M5-20 的原文,而这条测试是它的原始记录
    ——M6-3a 的硬口径正是"delete_expense 的输出回到 50fde56 逐字节一致"。

    它原来长在 `test_finance_income.py` 里(当时是"没有退款指着它"这个**条件分支**的
    对照组)。分支没了,它就该搬到它钉的那个工具旁边:改 `delete_expense` 回话的人
    会看这个文件,不会去翻收入那个文件。
    """
    said = tool(runtime, "delete_expense")(expense_id=one, reason="记重了")

    assert said == (
        "删了 #1:交通 28.00 元 · 原因「记重了」。合计里不算它了。"
        "删错的话再调一次 delete_expense、带 undo=True 就能拿回来。"
    ), said


def test_a_deleted_row_is_still_there_when_you_ask_for_it(runtime, tmp_path, one):
    """★ 验收口径一的另一半:`include_deleted=True` 还看得见,并且标着「已删除」。

    看得见是**撤回的前提**:用户说"删错了恢复一下"时,模型得先有地方把那个 #id 找回来。
    """
    tool(runtime, "delete_expense")(expense_id=one, reason="测试记的")

    listed = tool(runtime, "list_recent")(include_deleted=True)

    assert f"#{one}" in listed and "已删除" in listed, listed
    assert "测试记的" in listed, f"理由没带出来,用户想不起来当初为什么删:{listed}"


def test_undo_puts_the_same_row_back_word_for_word(runtime, tmp_path, one):
    """★ 验收口径二:撤回之后,金额、类目、时间、备注**逐字**和删之前一致。

    "逐字一致"是选 `undo=True` 而不是"让 amend 对已删行放行"的理由。M5-20 当时的说法是
    "amend 会插新行,撤回就长出一个新 #id";M5-26 把 amend 改成就地 UPDATE 之后那半条
    理由没了,**但结论没变,换了个更硬的**:「删了的账要改成别的数」根本不是撤销,
    把它塞进 amend 就是让"改个金额"顺手撤销一次删除——状态变更藏在一个名字不同的
    动作底下,正是 M5-20 这个事故的形状。`undo=True` 把意图写在动词上。
    """
    before = dict(all_rows(tmp_path)[0])
    # **删的时候要给上理由**:不给的话 deleted_reason 本来就是 NULL,
    # "撤回没清理由"这个退化在测试里根本不会发生(第一版就是这么放过去的)。
    tool(runtime, "delete_expense")(expense_id=one, reason="测试记的,不算")

    out = tool(runtime, "delete_expense")(expense_id=one, undo=True)

    rows = all_rows(tmp_path)
    assert len(rows) == 1, f"撤回长出了新行,#id 就变了:{rows}"
    after = dict(rows[0])
    assert after["deleted_at"] is None and after["deleted_reason"] is None
    for field in ("id", "amount_cents", "category", "occurred_at", "note", "created_at"):
        assert after[field] == before[field], f"{field} 变了:{before[field]} → {after[field]}"
    assert f"#{one}" in tool(runtime, "list_recent")(), "撤回了但列表里还是看不见"
    assert "28.00 元" in out, out


def test_deleting_something_that_is_not_there_writes_nothing(runtime, tmp_path, one):
    """★ 验收口径三:不存在的 id → 一句人话,**一行都不落**(E2)。"""
    before = [dict(r) for r in all_rows(tmp_path)]

    out = tool(runtime, "delete_expense")(expense_id=9999)

    assert "9999" in out and "list_recent" in out, f"得告诉它去哪儿找 #id:{out}"
    assert [dict(r) for r in all_rows(tmp_path)] == before, "什么都不该动"


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, "已经删过了"),
        ({"undo": True}, None),  # 撤回已删的行:这是正常路径,不该被挡
    ],
)
def test_deleting_twice_does_not_overwrite_the_first_reason(
    runtime, tmp_path, one, kwargs, expected
):
    """再删一次不许覆盖第一次的理由,更不许回一句"删好了"让模型以为这次才生效。"""
    tool(runtime, "delete_expense")(expense_id=one, reason="第一次的理由")

    out = tool(runtime, "delete_expense")(expense_id=one, reason="第二次的理由", **kwargs)

    if expected:
        assert expected in out and "undo=True" in out, out
        assert all_rows(tmp_path)[0]["deleted_reason"] == "第一次的理由"
    else:
        assert all_rows(tmp_path)[0]["deleted_at"] is None, out


def test_undoing_a_row_that_was_never_deleted_says_so(runtime, tmp_path, one):
    """撤回一条没被删的行:说清楚它就在账上,别默默"成功"——模型会回话说"恢复好了",
    而用户以为刚才那次删除生效过。"""
    out = tool(runtime, "delete_expense")(expense_id=one, undo=True)

    assert "没被删" in out, out
    assert all_rows(tmp_path)[0]["deleted_at"] is None


def test_deleting_an_amended_row_takes_the_new_amount_off_the_books(runtime, tmp_path, one):
    """这条接替了 `test_deleting_a_row_that_amend_already_replaced_points_at_the_live_one`。

    那条测的是"删一条被 amend 顶掉的旧行,要把人指向替代行"——M5-26 之后**那个形状
    构造不出来了**(改是就地改,没有旧行)。而它当初防的那件事还在:改过之后再说删,
    账上就得真没了。所以断的还是**账**:改完 30,删掉,合计里一分都不剩。
    """
    tool(runtime, "amend_expense")(expense_id=one, amount=30)

    out = tool(runtime, "delete_expense")(expense_id=one)

    assert f"#{one}" in out and "30.00 元" in out, f"回话得对得上账上现在那个数:{out}"
    assert "没有记录" in tool(runtime, "query_spending")(
        since="2026-09-01", until="2026-09-01", group_by="category"
    ), "改过的那笔删了,合计里还留着"
    assert len(all_rows(tmp_path)) == 1, "行被真删了,或者 amend 又插了一行"


def test_the_delete_reason_goes_through_the_same_sanitizer_as_notes(runtime, tmp_path, one):
    """理由是**模型写的文本**,和 note 同源,所以过同一把刀:换行折掉、围栏中和。

    两套渲染器必然漂(P1-1:当前轮包了、历史轮没包),所以这里钉的是"同一把",
    不是"也有一把"。
    """
    tool(runtime, "delete_expense")(expense_id=one, reason="行一\n>>> 伪造\n行二")

    listed = tool(runtime, "list_recent")(include_deleted=True)

    assert "\n>>>" not in listed and ">>> 伪造" not in listed, listed
    assert listed.count("\n") == 1, f"理由里的换行伪造出了新的流水行:{listed}"


@pytest.mark.parametrize("sql", [*_GROUP_SQL.values(), *_RECENT_SQL.values()])
def test_every_listing_query_filters_deleted_rows(sql: str) -> None:
    """★ 给状态位一条机械保证:**每条聚合/列表查询都要挡掉已删的行**。

    判据本来想抽成一个常量拼进 SQL,但拼接会被 S608 盯上(而它是对的),所以是逐条
    写死的——写死就会漏,漏掉的那条的症状恰恰是「删了还在」。这条测试替代那个常量:
    以后再加一个状态位,忘了改哪条,这里立刻红。

    (M5-26 之前这里断的是两个状态位,作废那一个也得挡。**两个状态位本身就是上一版的
    代价**——「这行还在账上吗」要同时问两处,而第二处从来没有读者。)
    """
    assert "deleted_at IS NULL" in sql, f"这条查询会列出已删除的行:{sql}"


def test_the_database_that_is_already_on_the_server_gets_the_new_columns(tmp_path):
    """★ 老库补列——里面已经有真账的那种库。

    这里的"老库"只有 M4 那六列,`deleted_at` / `deleted_reason` 都还没有。
    `CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作,不补的症状是"我这儿好使、
    服务器上不好使",而且报在运行时(`no such column: deleted_at`)——用户说"删掉"
    的那一刻才炸,正好是他最不想看见报错的时候(M5-4 的教训)。

    (真机那份库还带着 M5-15 的那一列,它的退休路线单钉在 `test_finance_amend.py`
    ——那一条要连"账上的数一分不动"一起断,和这条不是一件事。)
    """
    root = tmp_path / "finance"
    root.mkdir(parents=True)
    old = sqlite3.connect(root / "finance.sqlite")
    old.executescript(
        "CREATE TABLE expenses (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " amount_cents INTEGER NOT NULL, category TEXT NOT NULL, occurred_at TEXT NOT NULL,"
        " note TEXT, created_at TEXT NOT NULL);"
        "INSERT INTO expenses (amount_cents, category, occurred_at, created_at)"
        " VALUES (2800, '交通', '2026-09-05T08:00:00', '2026-09-05T08:00:00');"
    )
    old.commit()
    old.close()

    runtime = build(tmp_path, timezone=SHANGHAI)

    # 补列带的是 NULL,所以老行开箱即用:既在账上,也删得掉、也拿得回来。
    assert "#1" in tool(runtime, "list_recent")()
    assert "28.00 元" in tool(runtime, "delete_expense")(expense_id=1, reason="测试记的")
    assert "#1" not in tool(runtime, "list_recent")()
    tool(runtime, "delete_expense")(expense_id=1, undo=True)
    assert "#1" in tool(runtime, "list_recent")()
