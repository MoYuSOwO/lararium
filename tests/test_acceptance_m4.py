"""M4 验收:端到端走一遍记账 → 查询 → 读 skill,并盯住前缀区。

这一步不测模型的判断力(那是真机的活),测的是**管线接通了、而且没有把缓存打坏**:
加 bundle 那一次前缀变过(D3 认可的重建点),此后记账、查账、读 skill 的整个过程里,
`system_prompt` 必须逐字节不动;L0 必须是严格追加。
"""

import sqlite3
from pathlib import Path

import pytest
from bundles.finance.server import build as build_finance
from bundles.memory.server import build_memory_components, memory_tool_functions

from lararium.config import Settings
from lararium.db import connect
from lararium.envelope import Envelope
from lararium.steward.inbox import Inbox
from lararium.steward.journal import Journal
from lararium.steward.loop import Steward
from lararium.steward.model import ModelReply
from lararium.steward.outbox import Outbox
from lararium.steward.registry import Registry
from lararium.steward.threads import Threads


class ScriptedToolModel:
    """按剧本真的去调工具,并把每一轮收到的上下文留下来给断言看。"""

    def __init__(self, script: list[list[tuple[str, dict]]]) -> None:
        self._script = list(script)
        self.contexts = []
        self.results: list[str] = []

    async def run(self, ctx, tools, mcp_servers):
        self.contexts.append(ctx)
        by_name = {f.__name__: f for f in tools}
        events = []
        for name, kwargs in self._script.pop(0):
            out = by_name[name](**kwargs)
            self.results.append(out)
            events.append({"type": "tool_call", "tool": name, "args": kwargs, "tool_call_id": name})
            events.append(
                {"type": "tool_result", "tool": name, "content": out, "tool_call_id": name}
            )
        return ModelReply(text="好的。", tool_events=events)


@pytest.fixture
def steward(tmp_path, monkeypatch):
    monkeypatch.setenv("LARARIUM_API_KEY", "sk-test")
    monkeypatch.setenv("LARARIUM_DATA_DIR", str(tmp_path))
    settings = Settings.load()
    conn = connect(tmp_path / "steward.sqlite")
    ledger, gate = build_memory_components(tmp_path)

    def make(script):
        model = ScriptedToolModel(script)
        return (
            Steward(
                settings=settings,
                inbox=Inbox(conn),
                journal=Journal(conn),
                registry=Registry.load(Path("bundles")),
                ledger=ledger,
                gate=gate,
                model=model,
                persona=Path("prompts/persona.md").read_text(encoding="utf-8"),
                outbox=Outbox(conn),
                threads=Threads(conn),
                bundle_tools=[
                    *memory_tool_functions(gate),
                    *build_finance(tmp_path, timezone="Asia/Shanghai").tools,
                ],
            ),
            model,
            ledger,
        )

    return make


SCRIPT = [
    [("record_expense", {"amount": 28, "category": "交通", "occurred_at": "2026-08-03"})],
    [("record_expense", {"amount": 45, "category": "餐饮", "occurred_at": "2026-08-04"})],
    [("query_spending", {"since": "2026-08-01", "until": "2026-08-31", "group_by": "category"})],
    [("read_skill", {"bundle": "finance", "skill": "monthly-review"})],
    [
        (
            "list_recent",
            {"limit": 1, "since": "2026-08-01", "until": "2026-08-31", "order": "largest"},
        )
    ],
]


async def test_m4_end_to_end_records_queries_and_reads_the_skill(steward, tmp_path):
    """记账 → 记账 → 查询 → 读 skill → 查最大一笔,五轮走通。"""
    st, model, _ledger = steward(SCRIPT)
    for text in ("打车 28", "中午吃饭 45", "这个月都花在哪了", "怎么看一个月的账", "最大的一笔"):
        st.submit(Envelope.new(source="user", channel="cli", content=text))
        assert (await st.process_next()).kind == "replied"

    conn = sqlite3.connect(tmp_path / "finance" / "finance.sqlite")
    rows = list(conn.execute("SELECT amount_cents, category FROM expenses"))
    conn.close()
    assert rows == [(2800, "交通"), (4500, "餐饮")]

    summary, skill, largest = model.results[2], model.results[3], model.results[4]
    assert "73.00" in summary and "餐饮" in summary and "交通" in summary
    assert "先看总额趋势" in skill
    assert "45.00" in largest and "餐饮" in largest


async def test_the_prefix_never_moves_while_finance_is_being_used(steward, tmp_path):
    """★ **记账/查账的整个过程里,前缀区一个字节都不许变。**

    加 bundle 那一次前缀变过(D3 认可的重建点:注册表/工具变更 = 重启),此后
    finance 的读写都不该碰它——流水进 finance 的库,账本纹丝不动,而账本正是前缀的一部分。
    """
    st, model, ledger = steward(SCRIPT)
    before = ledger.read()
    for text in ("打车 28", "中午吃饭 45", "这个月都花在哪了", "怎么看一个月的账", "最大的一笔"):
        st.submit(Envelope.new(source="user", channel="cli", content=text))
        await st.process_next()

    assert len(model.contexts) == 5, "五轮没跑满,断言是空转的(假绿 #3)"
    prefixes = {c.system_prompt for c in model.contexts}
    assert len(prefixes) == 1, f"前缀区在 {len(model.contexts)} 轮里变了 {len(prefixes)} 种形态"
    assert ledger.read() == before, "账本被流水改写了"


async def test_l0_is_strictly_append_only_across_the_run(steward, tmp_path):
    """L0 严格追加:第 N 轮的消息列表必须是第 N+1 轮的前缀,否则每轮毁一次缓存。"""
    st, model, _ledger = steward(SCRIPT)
    for text in ("打车 28", "中午吃饭 45", "这个月都花在哪了", "怎么看一个月的账", "最大的一笔"):
        st.submit(Envelope.new(source="user", channel="cli", content=text))
        await st.process_next()

    assert len(model.contexts) == 5, "五轮没跑满,断言是空转的(假绿 #3)"
    for earlier, later in zip(model.contexts, model.contexts[1:], strict=False):
        head = earlier.messages[:-1]  # 去掉本轮信封,它后面还会跟着回复与工具往返
        assert later.messages[: len(head)] == head, "历史轮被改写了,不是严格追加"
