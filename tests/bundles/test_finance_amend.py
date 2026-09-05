"""M5-15 `amend_expense`:记错了能改,而且改不能删。

## 为什么要有它

finance 原来只有加,没有改也没有删。真机实测:模型填错金额后**会主动想更正**,
但手里只有 `record_expense`,于是先试 `amount=-27.0`(被工具挡下),再补记一条
`note「咖啡(金额有误待处理)」` 的标记行——一次填错必然长成两三行垃圾。
**模型的动机是对的,是我们没给路。**

## 形状照抄 `memory/ledger.py`:保留全部历史,让当前视图干净

作废 + 新记,**不就地覆盖**。就地覆盖读起来最干净,但它销毁证据,而"进过系统的一切
留痕"是不可协商第 3 条。改完永远还剩一条替代行——**这就是不给 delete 的底气**。
"""

import sqlite3
from pathlib import Path

import pytest
from bundles.finance.server import build

SHANGHAI = "Asia/Shanghai"


def tool(runtime, name: str):
    return next(f for f in runtime.tools if f.__name__ == name)


def all_rows(data_dir: Path) -> list[sqlite3.Row]:
    """**查全部行,含作废的**——这个文件关心的正是"作废行还在不在"。"""
    conn = sqlite3.connect(data_dir / "finance" / "finance.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM expenses ORDER BY id"))
    finally:
        conn.close()


@pytest.fixture
def runtime(tmp_path):
    return build(tmp_path, timezone=SHANGHAI)


def only_id(runtime, tmp_path) -> int:
    return all_rows(tmp_path)[0]["id"]


def test_amending_leaves_one_live_row_with_the_right_amount(runtime, tmp_path):
    """★ 验收口径一:一次「记错 → 更正」的来回之后,账上**恰好一条有效记录、金额是对的
    那个**,而**作废行还查得到**。

    两半都要断:只断"当前视图对"的话,一个就地覆盖的实现照样过,而它把证据销毁了。
    """
    tool(runtime, "record_expense")(amount=49, category="餐饮", note="咖啡")
    bad = only_id(runtime, tmp_path)

    out = tool(runtime, "amend_expense")(expense_id=bad, amount=22)

    rows = all_rows(tmp_path)
    live = [r for r in rows if r["voided_by"] is None]
    assert len(live) == 1 and live[0]["amount_cents"] == 2200, rows
    assert len(rows) == 2, "作废行没留下来——改成了就地覆盖?那就是销毁证据"
    voided = next(r for r in rows if r["voided_by"] is not None)
    assert voided["amount_cents"] == 4900 and voided["voided_by"] == live[0]["id"]
    assert "49" in out and "22" in out, f"回话得说清楚改了什么:{out}"


def test_amend_carries_over_what_you_did_not_change(runtime, tmp_path):
    """只给要改的那一项,其余原样带过来——不然每次更正都要模型把四个字段重打一遍,
    而重打就是又一次抄错的机会。"""
    tool(runtime, "record_expense")(
        amount=49, category="餐饮", occurred_at="2026-09-01 12:30", note="咖啡"
    )

    tool(runtime, "amend_expense")(expense_id=only_id(runtime, tmp_path), amount=22)

    live = next(r for r in all_rows(tmp_path) if r["voided_by"] is None)
    assert (live["category"], live["note"]) == ("餐饮", "咖啡")
    assert live["occurred_at"].startswith("2026-09-01T12:30")


def test_amend_cannot_conjure_a_row_out_of_nothing(runtime, tmp_path):
    """★ 验收口径二:更正工具**不许凭空造出一笔**,只能作用在已存在的行上。

    能凭空造的话它就是第二个 `record_expense`,而且是个没有金额校验心智负担的
    ——模型会拿它当后门用。
    """
    out = tool(runtime, "amend_expense")(expense_id=999, amount=22)

    assert all_rows(tmp_path) == []
    assert "999" in out and ("没有" in out or "找不到" in out)


def test_amending_an_already_voided_row_is_refused_in_plain_words(runtime, tmp_path):
    """改一条已经被作废的行:说清楚它已经被谁替代了,别让模型顺着旧 id 一路改下去
    ——那会长出一条谁也读不懂的链子。"""
    tool(runtime, "record_expense")(amount=49, category="餐饮")
    bad = only_id(runtime, tmp_path)
    tool(runtime, "amend_expense")(expense_id=bad, amount=22)

    out = tool(runtime, "amend_expense")(expense_id=bad, amount=30)

    live = [r for r in all_rows(tmp_path) if r["voided_by"] is None]
    assert len(live) == 1 and live[0]["amount_cents"] == 2200, "顺着旧 id 又改了一遍"
    assert "已经" in out or "改过" in out


def test_a_bad_new_amount_changes_nothing(runtime, tmp_path):
    """E2:新金额不合法就整条不动——**不许先作废再失败**,那会把一笔好记录改没了。"""
    tool(runtime, "record_expense")(amount=49, category="餐饮")
    bad = only_id(runtime, tmp_path)

    out = tool(runtime, "amend_expense")(expense_id=bad, amount=-27)

    rows = all_rows(tmp_path)
    assert len(rows) == 1 and rows[0]["voided_by"] is None, "作废了却没插上新的,记录没了"
    assert "没改" in out or "这笔没" in out


def test_totals_do_not_count_voided_rows(runtime, tmp_path):
    """★ 验收口径三:`query_spending` 的总额不许把作废行算进去。

    算进去的结果是"改完之后这个月凭空多花了 49 块",而它不会报错——月底对不上账时
    你也想不到是这里。
    """
    tool(runtime, "record_expense")(amount=49, category="餐饮", occurred_at="2026-09-01")
    tool(runtime, "amend_expense")(expense_id=only_id(runtime, tmp_path), amount=22)

    out = tool(runtime, "query_spending")(
        since="2026-09-01", until="2026-09-30", group_by="category"
    )

    assert "22" in out and "49" not in out and "71" not in out, out


def test_list_recent_hides_voided_rows_but_can_show_them(runtime, tmp_path):
    """默认只看有效的;想看全部另给参数——**留痕的意义是查得到**,查不到就等于没留。"""
    tool(runtime, "record_expense")(amount=49, category="餐饮", note="咖啡")
    tool(runtime, "amend_expense")(expense_id=only_id(runtime, tmp_path), amount=22)

    default = tool(runtime, "list_recent")()
    everything = tool(runtime, "list_recent")(include_voided=True)

    assert "49" not in default and "22" in default
    assert "49" in everything and "22" in everything


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


def test_finance_still_offers_no_way_to_delete(runtime):
    """**不给 delete。** 删除是唯一会彻底销毁用户可能想要的信息的操作。

    改完永远还剩一条替代行,所以"改"是安全的;"删"没有这个性质,所以干脆不提供
    ——安全靠**改不能删 + 全程留痕**,不靠在主控里加守卫拦(M5-11 就是那么丢的 5 笔账)。
    """
    names = [f.__name__ for f in runtime.tools]

    assert not [n for n in names if "delete" in n or "remove" in n or "drop" in n], names


def test_totals_by_day_also_skip_voided_rows(runtime, tmp_path):
    """按天那条分支也得跳过作废行。

    两条聚合 SQL 是分开写的(写死字面量、不拼列名),所以**加条件时会漏掉一条**
    ——而漏掉的表现是"按类目对、按天不对",没有任何报错,只有月底对账时的一句
    "怎么又不一样"。这条和上面那条是同一条规则的两个出口,必须各钉一次。
    """
    tool(runtime, "record_expense")(amount=49, category="餐饮", occurred_at="2026-09-01")
    tool(runtime, "amend_expense")(expense_id=only_id(runtime, tmp_path), amount=22)

    out = tool(runtime, "query_spending")(since="2026-09-01", until="2026-09-30", group_by="day")

    assert "22" in out and "49" not in out and "71" not in out, out


def test_an_old_database_gets_the_voided_column(tmp_path):
    """老库要补上 `voided_by`。

    `CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作——不补的症状是"新装的机器好使,
    你自己那台不好使",而且报在运行时(`no such column`),不报在启动时。
    这是最难查的那一类,所以补列要机械化、开库时就做(M5-4 的教训,同一份机制)。
    """
    root = tmp_path / "finance"
    root.mkdir(parents=True)
    old = sqlite3.connect(root / "finance.sqlite")
    old.executescript(
        "CREATE TABLE expenses (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " amount_cents INTEGER NOT NULL, category TEXT NOT NULL, occurred_at TEXT NOT NULL,"
        " note TEXT, created_at TEXT NOT NULL);"
        "INSERT INTO expenses (amount_cents, category, occurred_at, created_at)"
        " VALUES (4900, '餐饮', '2026-09-01T12:00:00', '2026-09-01T12:00:00');"
    )
    old.commit()
    old.close()

    runtime = build(tmp_path, timezone=SHANGHAI)

    # 老行照样能被改——补列带了 NULL 默认,不是留一堆读不出来的东西
    out = tool(runtime, "amend_expense")(expense_id=1, amount=22)
    assert "22" in out, out
    assert {r["id"]: r["voided_by"] for r in all_rows(tmp_path)} == {1: 2, 2: None}
