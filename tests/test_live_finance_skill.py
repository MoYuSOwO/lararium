"""M4-2 的硬前置(M4-1 验收登记一):finance 的 SKILL.md 正文**确实进了模型上下文**。

为什么必须是真模型:这条要证的不是"代码里有一条路能读到 SKILL.md",而是"模型真的
走了那条路"。假模型只会按剧本调它被安排调的工具,那证明的是剧本,不是行为。
M2/M3 审计九条里有六条是同一个毛病——防护存在,但没人真的走到那里。

为什么查起居注而不看回复文本:回复里出现「不记进账本」可能是模型顺口说的。
证据要取模型**实收的那一份**(不可协商第 3 条),也就是起居注里那条 `tool_result`
事件的正文——它就是框架回灌进对话历史、模型据以作答的字节。

**这条现在是红的,而且是故意留红的。** 2026-08-21 实测(mimo-v2.5,每档 5 次):

    M4-1 原版 persona                     1/5 读了总览
    M4-2 新版 persona(先读总览再动手)   2/5
    新版 persona + 目录行加"用前先 read_skill"  2/5

三档在 n=5 下无法区分,合计 5/15 ≈ 33%。也就是说**靠 prompt 让模型先读总览不是机制**,
是概率。它红着,是因为 M4-1 验收登记一的条件("证明那段正文进了模型上下文")确实还没
兑现——把它改绿的正确方式是给一条强制路径,不是放松断言。详见 REVIEW.md M4-2 登记。

跑法(默认跳过,不进日常门禁):

    set -a && source .env && set +a && uv run pytest tests/test_live_finance_skill.py -v -s -m live
"""

import os
import sqlite3
from pathlib import Path

import pytest

from lararium.envelope import Envelope

# 模块级抓取:conftest 的 autouse fixture 会在每个测试前清掉所有 LARARIUM_*。
# 收集期(fixture 跑之前)先把真实配置留在手里,测试里再原样喂回去。
_LIVE_ENV = {k: v for k, v in os.environ.items() if k.startswith("LARARIUM_")}

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not _LIVE_ENV.get("LARARIUM_API_KEY"),
        reason="真模型验收:需要 LARARIUM_API_KEY(先 set -a && source .env && set +a)",
    ),
]

SKILL_PATH = Path("bundles/finance/skills/SKILL.md")


@pytest.fixture
def skill_text() -> str:
    """总览正文。在 fixture 里读:异步测试体里碰 pathlib 会被 ASYNC240 拦下。"""
    return SKILL_PATH.read_text(encoding="utf-8").strip()


@pytest.fixture
def steward(tmp_path, monkeypatch):
    """走**生产的组装根** build_steward,不是测试专用的平行构造——测的才真。

    只把 data_dir 改到 tmp_path:真 key、真模型、真 persona、真 registry、真 bundle 工具。
    """
    from bundles.memory.server import build_memory_components

    from lararium.config import Settings
    from lararium.gateway.server import build_steward

    for key, value in _LIVE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("LARARIUM_DATA_DIR", str(tmp_path))

    settings = Settings.load()
    ledger, gate = build_memory_components(settings.data_dir)
    return build_steward(settings, ledger, gate)


async def test_model_reads_the_finance_overview_before_it_records(steward, tmp_path, skill_text):
    """一轮"我今天吃饭花了 45":模型必须先读到 finance 总览正文,再动手记账。

    断言三件事,全部取自起居注:
    1. `read_skill` 的 tool_result 里**逐字**含 SKILL.md 全文(正文进了上下文);
    2. 它的 seq 早于 `record_expense` 的 tool_call(是"读了再干",不是"干完补读");
    3. 账真的记进了 finance 自己的库(这一轮确实干了活,不是只读不做)。
    """
    env = Envelope.new(source="user", channel="cli", content="我今天吃饭花了 45")
    steward.submit(env)
    outcome = await steward.process_next()
    assert outcome.kind == "replied", f"这一轮没走到终态:{outcome}"

    events = steward.journal.replay(env.id)
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

    record_seqs = [seq for seq, tool in calls if tool == "record_expense"]
    assert record_seqs, f"模型没记账,这一轮没走到要证的那步。工具调用:{calls}"
    assert hit[0]["seq"] < record_seqs[0], (
        "SKILL.md 是在记账**之后**才读的——正文进了上下文,但没能影响这次动作。"
    )

    conn = sqlite3.connect(tmp_path / "finance" / "finance.sqlite")
    rows = list(conn.execute("SELECT amount_cents, category, occurred_at FROM expenses"))
    conn.close()
    print("[落库]", rows)
    assert rows, "record_expense 调过了,但库里没有流水"
