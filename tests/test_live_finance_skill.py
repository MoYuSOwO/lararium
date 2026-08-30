"""M4-2 的硬前置(M4-1 验收登记一):finance 的 SKILL.md 正文**确实进了模型上下文**。

为什么必须是真模型:这条要证的不是"代码里有一条路能读到 SKILL.md",而是"模型真的
走了那条路"。假模型只会按剧本调它被安排调的工具,那证明的是剧本,不是行为。
M2/M3 审计九条里有六条是同一个毛病——防护存在,但没人真的走到那里。

为什么查起居注而不看回复文本:回复里出现「不记进账本」可能是模型顺口说的。
证据要取模型**实收的那一份**(不可协商第 3 条),也就是起居注里那条 `tool_result`
事件的正文——它就是框架回灌进对话历史、模型据以作答的字节。

**它曾经是红的,而且红了一整个里程碑。** 2026-08-21(mimo-v2.5,每档 5 次)合计
5/15 ≈ 33%;M5-11 重新量准(每个模型 25 次)更难看:**mimo 0/25、deepseek 9/25**。
也就是说**靠 prompt 让模型先读总览不是机制,是概率**——而且概率会随模型漂,
还会随纪律列表变长被挤掉。

M4 当时就写下了正确的改法:**给一条强制路径,不是放松断言**。M5-11 把它做了
——`Steward._require_overview`:领域工具第一次被调用前,该领域总览必须已经进过本轮
上下文,否则第一次调用只拿到一句可照做的提示、**不产生任何副作用**。

**判据随之改了一处,理由要说清楚**:原来比的是"读总览"和"第一次调 record_expense"
的先后。守卫上线后,被拦下的那一次**什么都没记**——拿它当"记账"来比,会把
「拦下 → 去读 → 再记」这条守卫正常工作的路径判成失败。所以现在比的是**真正生效的
那一次**(结果不是那句提示的那次)。这不是放松:要证的一直是"正文早于账真的落库",
被拦下的那次不是落库。

跑法(默认跳过,不进日常门禁):

    set -a && source .env && set +a && uv run pytest tests/test_live_finance_skill.py -v -s -m live
"""

import sqlite3
from pathlib import Path

import pytest

from lararium.envelope import Envelope

pytestmark = pytest.mark.live

SKILL_PATH = Path("bundles/finance/skills/SKILL.md")


@pytest.fixture
def skill_text() -> str:
    """总览正文。在 fixture 里读:异步测试体里碰 pathlib 会被 ASYNC240 拦下。"""
    return SKILL_PATH.read_text(encoding="utf-8").strip()


async def test_model_reads_the_finance_overview_before_it_records(
    live_steward, tmp_path, skill_text
):
    """一轮"我今天吃饭花了 45":模型必须先读到 finance 总览正文,再动手记账。

    断言三件事,全部取自起居注:
    1. `read_skill` 的 tool_result 里**逐字**含 SKILL.md 全文(正文进了上下文);
    2. 它的 seq 早于 `record_expense` 的 tool_call(是"读了再干",不是"干完补读");
    3. 账真的记进了 finance 自己的库(这一轮确实干了活,不是只读不做)。
    """
    env = Envelope.new(source="user", channel="cli", content="我今天吃饭花了 45")
    live_steward.submit(env)
    outcome = await live_steward.process_next()
    assert outcome.kind == "replied", f"这一轮没走到终态:{outcome}"

    events = live_steward.journal.replay(env.id)
    calls = [(e["seq"], e["payload"].get("tool")) for e in events if e["kind"] == "tool_call"]
    reads = [
        e for e in events if e["kind"] == "tool_result" and e["payload"].get("tool") == "read_skill"
    ]

    # 把真机行为原样打出来,验收记录直接抄这段(-s 可见)
    print("\n[工具调用顺序]", calls)
    print("[回复]", outcome.text)

    hit = [r for r in reads if skill_text in r["payload"].get("content", "")]
    assert hit, (
        "模型没把 finance 总览读进上下文——read_skill 的 tool_result 里没有 SKILL.md 全文。"
        f"本轮工具调用:{calls}"
    )

    # **生效的那次**才算记账:被路由守卫拦下的那次结果是一句提示,库里什么都没多。
    results = {e["payload"].get("tool_call_id"): e for e in events if e["kind"] == "tool_result"}
    effective = [
        e["seq"]
        for e in events
        if e["kind"] == "tool_call"
        and e["payload"].get("tool") == "record_expense"
        and "read_skill("
        not in str(
            results.get(e["payload"].get("tool_call_id"), {}).get("payload", {}).get("content", "")
        )
    ]
    assert effective, f"模型没真记上账,这一轮没走到要证的那步。工具调用:{calls}"
    assert hit[0]["seq"] < effective[0], (
        "SKILL.md 是在记账**之后**才读的——正文进了上下文,但没能影响这次动作。"
    )

    conn = sqlite3.connect(tmp_path / "finance" / "finance.sqlite")
    rows = list(conn.execute("SELECT amount_cents, category, occurred_at FROM expenses"))
    conn.close()
    print("[落库]", rows)
    assert rows, "record_expense 调过了,但库里没有流水"
