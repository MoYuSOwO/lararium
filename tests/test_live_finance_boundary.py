"""M4-5 验收:账本与流水的边界。**真模型,不是假模型。**

两条,一条防漏一条防过:

1. 连着记十笔日常消费 → `propose_fact` **零次调用**、账本**逐字节未变**;
2. 说一句真正的月级事实(「我房租每月 3800」)→ 它**该 propose 还是 propose**。

第二条不是凑数,是防矫枉过正:边界是「**流水不进**」,不是「财务的事都不进」。
只测第一条的话,把模型吓到再也不敢入档一样能全绿,而那是另一个方向的坏。

第三条是写这两条时**撞出来的**,和边界无关、但比边界严重:模型对每一笔都回「记好了」,
实际只调了一部分工具。它单独一条、**故意留红**——和边界混在一条里,两边都说不清。

为什么必须真模型:这里要证的是**模型的行为倾向**,不是代码分支。假模型只会照剧本
调它被安排调的工具——那证明的是剧本。

跑法(默认跳过,不进日常门禁):

    set -a && source .env && set +a && uv run pytest tests/test_live_finance_boundary.py -v -s -m live
"""

import sqlite3

import pytest

from lararium.envelope import Envelope

pytestmark = pytest.mark.live

# 十笔日常消费,覆盖固定七类目里的六类。都是"事件",没有一条是"安排"。
DAILY_EXPENSES = (
    "打车 28,记一下",
    "今天中午吃饭 45",
    "楼下便利店买了瓶水,3 块",
    "地铁 6 块",
    "买了包纸巾 12 块",
    "晚上看了个电影 60",
    "药店买感冒药 35",
    "同事结婚随了 200",
    "打车回家 32",
    "早饭豆浆油条 9 块",
)


def _recorded(data_dir) -> list[tuple[int, str]]:
    conn = sqlite3.connect(data_dir / "finance" / "finance.sqlite")
    try:
        return list(conn.execute("SELECT amount_cents, category FROM expenses"))
    finally:
        conn.close()


def _tools_called(steward, envelope_id: str) -> list[str]:
    return [
        e["payload"]["tool"]
        for e in steward.journal.replay(envelope_id)
        if e["kind"] == "tool_call"
    ]


async def _say(steward, text: str) -> tuple[str, list[str]]:
    env = Envelope.new(source="user", channel="cli", content=text)
    steward.submit(env)
    outcome = await steward.process_next()
    assert outcome.kind == "replied", f"这一轮没走到终态:{outcome}"
    return outcome.text or "", _tools_called(steward, env.id)


async def test_ten_daily_expenses_never_reach_the_ledger(live_steward, tmp_path):
    """十笔流水,一次 propose 都不许有,账本逐字节未变。

    为什么两条都要断:`propose_fact` 零调用是**意图**层面的;账本未变是**结果**层面的。
    而 `provenance="user_stated"` 的提案在 `gate.propose` 里当场就是 `passed`,
    worker 空闲时自动 `settle()` 就进账本了——所以这里显式调一次 `settle_if_needed()`,
    把 worker 会做的事做掉,再比账本。不这么做,账本"没变"只是因为没人结算。
    """
    before = live_steward.ledger.read()
    proposed: list[tuple[str, list[str]]] = []

    for line in DAILY_EXPENSES:
        reply, tools = await _say(live_steward, line)
        print(f"\n[{line}] → {tools}\n    {reply}")
        if "propose_fact" in tools:
            proposed.append((line, tools))

    live_steward.settle_if_needed()  # worker 空闲时就是这么干的

    recorded = _recorded(tmp_path)
    print(f"\n[落库] {len(recorded)}/{len(DAILY_EXPENSES)} 笔:{recorded}")
    # 至少得真发生过:模型一笔都不记,"零 propose"就是白拿的绿。
    # (它**应该**十笔全记,那条单独测,见下面那个故意留红的。)
    assert recorded, "十轮下来一笔都没记成,这一轮不能算数"

    assert live_steward.ledger.read() == before, "账本被流水改写了"
    assert not proposed, f"{len(proposed)} 笔流水被 propose 进了账本:{proposed}"


async def test_every_expense_the_model_says_it_recorded_is_actually_recorded(
    live_steward, tmp_path
):
    """**故意留红。** 模型对每一笔都回「记好了」,实际只调了一部分工具。

    2026-08-21 实测(mimo-v2.5),十笔日常消费:

        乙(硬边界进 persona)之前   4/10 真的落库
        乙之后                       4/10

    落空的那几轮,回话是「纸巾 12 块,记上了」「感冒药 35,记上了。今天累计 189」
    ——**连累计都是拿没记成的账算出来的**。用户看到的是记上了,账上什么都没有,
    而他不会再说第二遍。这是静默数据丢失 + 虚假确认,比 propose 越界严重。

    persona 已经加了「说"记好了"之前先真的把工具调了」,**没兜住**——和 M4-2 那次
    (33% 的 read_skill 到达率)是同一个结论:prompt 不是机制。这条红着,是这个缺陷
    还没有机制去挡;把它改绿的正确方式是找到那个机制,不是放松断言。
    """
    for line in DAILY_EXPENSES:
        await _say(live_steward, line)

    recorded = _recorded(tmp_path)
    assert len(recorded) == len(DAILY_EXPENSES), (
        f"模型每一笔都说记好了,实际只落库 {len(recorded)}/{len(DAILY_EXPENSES)} 笔"
    )


async def test_a_monthly_arrangement_is_still_proposed(live_steward):
    """「我房租每月 3800」是**安排**不是事件,该入档还是要入档。

    这条防的是矫枉过正:把模型吓到再也不敢 propose,第一条测试一样全绿,
    但助手就此失去了记住用户的能力。
    """
    _reply, tools = await _say(live_steward, "我房租每月 3800")
    print(f"\n[房租] → {tools}")

    assert "propose_fact" in tools, f"月级安排没被 propose,矫枉过正了。工具调用:{tools}"
