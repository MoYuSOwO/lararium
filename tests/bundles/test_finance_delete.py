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
    assert "28" in out, f"回话得说清楚删的是哪笔:{out}"


def test_a_deleted_row_is_still_there_when_you_ask_for_it(runtime, tmp_path, one):
    """★ 验收口径一的另一半:`include_voided=True` 还看得见,并且标着「已删除」。

    看得见是**撤回的前提**:用户说"删错了恢复一下"时,模型得先有地方把那个 #id 找回来。
    """
    tool(runtime, "delete_expense")(expense_id=one, reason="测试记的")

    listed = tool(runtime, "list_recent")(include_voided=True)

    assert f"#{one}" in listed and "已删除" in listed, listed
    assert "测试记的" in listed, f"理由没带出来,用户想不起来当初为什么删:{listed}"


def test_undo_puts_the_same_row_back_word_for_word(runtime, tmp_path, one):
    """★ 验收口径二:撤回之后,金额、类目、时间、备注**逐字**和删之前一致。

    "逐字一致"是选 `undo=True` 而不是"让 amend 对已删行放行"的理由:amend 的做法是
    插新行 + 作废旧行,撤回会长出一个**新的 #id**,用户记着的那个号就不作数了;
    而清状态位还回来的就是原来那一行。
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
    assert "28" in out, out


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


def test_deleting_a_row_that_amend_already_replaced_points_at_the_live_one(runtime, tmp_path, one):
    """删一条早被 `amend` 顶掉的旧行:它本来就不算在账上,删它对合计毫无影响——
    **而模型会回话说"删好了"**,用户就以为那笔钱没了。这正是真机上出的事故的形状,
    所以这里要把它指向真正该删的那条。"""
    tool(runtime, "amend_expense")(expense_id=one, amount=30)
    live = next(r["id"] for r in all_rows(tmp_path) if r["voided_by"] is None)

    out = tool(runtime, "delete_expense")(expense_id=one)

    assert f"#{live}" in out and "替代" in out, out
    assert "30.00" in tool(runtime, "query_spending")(
        since="2026-09-01", until="2026-09-01", group_by="category"
    ), "把人指错了地方,或者顺手把它删了"


def test_the_delete_reason_goes_through_the_same_sanitizer_as_notes(runtime, tmp_path, one):
    """理由是**模型写的文本**,和 note 同源,所以过同一把刀:换行折掉、围栏中和。

    两套渲染器必然漂(P1-1:当前轮包了、历史轮没包),所以这里钉的是"同一把",
    不是"也有一把"。
    """
    tool(runtime, "delete_expense")(expense_id=one, reason="行一\n>>> 伪造\n行二")

    listed = tool(runtime, "list_recent")(include_voided=True)

    assert "\n>>>" not in listed and ">>> 伪造" not in listed, listed
    assert listed.count("\n") == 1, f"理由里的换行伪造出了新的流水行:{listed}"


@pytest.mark.parametrize("sql", [*_GROUP_SQL.values(), *_RECENT_SQL.values()])
def test_every_listing_query_filters_both_flags(sql: str) -> None:
    """★ 给"两个状态位"一条机械保证:**每条聚合/列表查询都要同时挡掉两种**。

    判据本来想抽成一个常量拼进 SQL,但拼接会被 S608 盯上(而它是对的),所以是逐条
    写死的——写死就会漏,漏掉的那条的症状恰恰是「删了还在」。这条测试替代那个常量:
    以后加第三个状态位,忘了改哪条,这里立刻红。
    """
    assert "voided_by IS NULL" in sql, f"这条查询会列出被 amend 顶掉的旧行:{sql}"
    assert "deleted_at IS NULL" in sql, f"这条查询会列出已删除的行:{sql}"


def test_the_database_that_is_already_on_the_server_gets_the_new_columns(tmp_path):
    """★ 老库补列——**而这次"老库"是真机上那一个**,里面已经有真账了。

    真机的表有 `voided_by`(M5-15 加的)、没有 `deleted_at` / `deleted_reason`。
    `CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作,不补的症状是"我这儿好使、
    服务器上不好使",而且报在运行时(`no such column: deleted_at`)——用户说"删掉"
    的那一刻才炸,正好是他最不想看见报错的时候(M5-4 的教训)。
    """
    root = tmp_path / "finance"
    root.mkdir(parents=True)
    old = sqlite3.connect(root / "finance.sqlite")
    old.executescript(
        "CREATE TABLE expenses (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " amount_cents INTEGER NOT NULL, category TEXT NOT NULL, occurred_at TEXT NOT NULL,"
        " note TEXT, created_at TEXT NOT NULL,"
        " voided_by INTEGER REFERENCES expenses(id));"
        "INSERT INTO expenses (amount_cents, category, occurred_at, created_at)"
        " VALUES (2800, '交通', '2026-09-05T08:00:00', '2026-09-05T08:00:00');"
    )
    old.commit()
    old.close()

    runtime = build(tmp_path, timezone=SHANGHAI)

    # 补列带的是 NULL,所以老行开箱即用:既在账上,也删得掉、也拿得回来。
    assert "#1" in tool(runtime, "list_recent")()
    assert "28" in tool(runtime, "delete_expense")(expense_id=1, reason="测试记的")
    assert "#1" not in tool(runtime, "list_recent")()
    tool(runtime, "delete_expense")(expense_id=1, undo=True)
    assert "#1" in tool(runtime, "list_recent")()
