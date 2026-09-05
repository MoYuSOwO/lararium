"""真机:记账走不走得通,以及**该读方法篇的时候读不读**(M5-14 重写)。

## 这个文件为什么被整份改写

M4-2 起它钉的是「记账前必须先 read_skill(finance) 读总览」。**那条断言本身是错的**,
而 M5-11 整条链子就是从"它变红了"开始的——没有人先问过这条断言该不该存在。

后来量出来的是:那道强制读总览的守卫**就是丢账的全部原因**。同一串 8 笔、每条排干队列:

    守卫开着   账上 3/8   丢 5 笔   谎报 4 次   284 秒
    守卫关掉   账上 8/8   丢 0 笔   谎报 0 次    55 秒

模型第一次就把参数填对了,是我们把它正确的动作打回去;而回话里没说"这笔没记",
它读完 skill 以为生效了,于是回「记好了」。而它被逼去读的那份总览,逐条对下来只剩两句
别处没有的话(长区间截断、相对时间要自己换算)——那两句已经挪进 docstring,总览删了。

## 所以现在钉什么

钉**真正该成立的两条**:

1. 连续记账 8 笔,账上就该有 8 笔——这是用户唯一在乎的事;
2. 复杂任务(系统看一个月的账)仍然会自己去 `read_skill(finance, monthly-review)`。
   摘掉守卫不能把"该读的时候也不读了"一起带走;它现在靠的是目录行里那句 desc,
   **而 desc 从此是承重的**。

跑法(默认跳过,不进日常门禁):

    set -a && source .env && set +a && uv run pytest tests/test_live_finance_skill.py -v -s -m live
"""

import sqlite3

import pytest

from lararium.envelope import Envelope

pytestmark = pytest.mark.live

EXPENSES = [
    ("打车 28", 2800),
    ("中午吃饭 45", 4500),
    ("买了杯咖啡 32", 3200),
    ("地铁 5 块", 500),
    ("晚饭 68", 6800),
    ("水果 23", 2300),
    ("打车 19", 1900),
    ("奶茶 16", 1600),
]


async def drain(steward, content):
    """投一条并**把队列排干**——可重试失败会把信封放回 pending,不排干就统计不准。"""
    steward.submit(Envelope.new(source="user", channel="cli", content=content))
    outcome = await steward.process_next()
    while outcome.kind == "retry_later":
        outcome = await steward.process_next()
    return outcome


async def test_eight_expenses_land_as_eight_rows(live_steward, tmp_path):
    """★ 用户唯一在乎的事:说了八笔,账上就该有八笔——**不丢、不重**。

    这条钉的是**我们的机制**:M5-11 的守卫会把模型填对了的调用打回去,同一串八笔只剩
    三笔(守卫开 3/8、关 8/8)。条数是确定性的,守卫回来了它立刻红。

    **金额对不对是另一件事,只打印不断言**,理由要说清楚免得被当成放宽:那测的是模型
    的抄写准确度(实测见过「水果 23」被填成 24),n=1 的真机测试钉不住它,钉了就是一条
    随机红的测试——比没有更糟。它属于"错记"那一类,单独立项对账,而**这里少一条断言
    不代表那件事不重要**:所以对不上时照样把两串数字打出来。
    """
    for content, _cents in EXPENSES:
        outcome = await drain(live_steward, content)
        assert outcome.kind == "replied", f"「{content}」这一轮没走到终态:{outcome}"

    conn = sqlite3.connect(tmp_path / "finance" / "finance.sqlite")
    rows = list(conn.execute("SELECT amount_cents, category, note FROM expenses"))
    conn.close()
    got, want = sorted(r[0] for r in rows), sorted(c for _t, c in EXPENSES)
    print("\n[落库]", rows)
    if got != want:
        print(f"[金额对不上] 期望 {want} 实际 {got} —— 模型抄错,不是丢账,见 docstring")

    assert len(rows) == len(EXPENSES), (
        f"说了 {len(EXPENSES)} 笔,账上 {len(rows)} 条:"
        f"{'少了(丢账)' if len(rows) < len(EXPENSES) else '多了(重记)'}。{rows}"
    )


async def test_a_systematic_review_still_goes_and_reads_the_method(live_steward):
    """★ 摘掉守卫不能把"该读的时候也不读了"一起带走。

    总览没了之后,模型决定要不要读 `monthly-review` 手里只有目录行里那句
    「monthly-review    怎么看一个月的账」。这条钉的就是**那句 desc 还在承重**。

    判据取起居注里的调用,不看回复措辞——它可以说"我看了一下方法"而根本没调。
    """
    env = Envelope.new(
        source="user", channel="cli", content="帮我系统地看一下这个月的账,有什么该注意的"
    )
    live_steward.submit(env)
    outcome = await live_steward.process_next()
    assert outcome.kind == "replied", f"这一轮没走到终态:{outcome}"

    calls = [
        (e["payload"].get("tool"), e["payload"].get("args"))
        for e in live_steward.journal.replay(env.id)
        if e["kind"] == "tool_call"
    ]
    print("\n[工具调用]", calls)
    print("[回复]", outcome.text)

    assert any(tool == "read_skill" and "monthly-review" in str(args) for tool, args in calls), (
        f"复杂任务没去读方法篇——目录行那句 desc 不再承重了:{calls}"
    )
