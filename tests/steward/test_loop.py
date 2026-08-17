from pathlib import Path

import pytest
from bundles.memory.server import build_memory_components, memory_tool_functions

from lararium.config import Settings
from lararium.db import connect
from lararium.envelope import Envelope
from lararium.steward.assembler import AssembledContext
from lararium.steward.inbox import Inbox
from lararium.steward.journal import Journal
from lararium.steward.loop import Steward
from lararium.steward.model import ModelReply
from lararium.steward.registry import Registry


class FakeModel:
    """记录收到的上下文与工具集,返回预设回复。"""

    def __init__(self, replies: list[ModelReply]) -> None:
        self._replies = list(replies)
        self.seen: list[AssembledContext] = []
        self.tools_seen: list[list] = []

    async def run(self, ctx, tools, mcp_servers):
        self.seen.append(ctx)
        self.tools_seen.append(tools)
        return self._replies.pop(0) if self._replies else ModelReply(text="嗯")


@pytest.fixture
def steward_factory(tmp_path, monkeypatch):
    def make(replies=None):
        monkeypatch.setenv("LARARIUM_API_KEY", "sk-test")
        monkeypatch.setenv("LARARIUM_DATA_DIR", str(tmp_path))
        settings = Settings.load()
        conn = connect(tmp_path / "steward.sqlite")
        ledger, gate = build_memory_components(tmp_path)
        model = FakeModel(replies or [])
        steward = Steward(
            settings=settings,
            inbox=Inbox(conn),
            journal=Journal(conn),
            registry=Registry.load(Path("bundles")),
            ledger=ledger,
            gate=gate,
            model=model,
            persona="你是 Lararium。",
            bundle_tools=memory_tool_functions(gate),
        )
        return steward, model

    return make


async def test_process_next_returns_reply_text(steward_factory):
    steward, _ = steward_factory([ModelReply(text="你好呀")])
    steward.submit(Envelope.new(source="user", channel="cli", content="你好"))
    assert await steward.process_next() == "你好呀"


async def test_process_next_returns_none_when_inbox_empty(steward_factory):
    steward, _ = steward_factory()
    assert await steward.process_next() is None


async def test_model_receives_builtin_and_bundle_tools_in_fixed_order(steward_factory):
    """模型必须真能调到 propose_fact,否则门控在真实对话里根本走不通。"""
    steward, model = steward_factory([ModelReply(text="好")])
    steward.submit(Envelope.new(source="user", channel="cli", content="你好"))
    await steward.process_next()

    names = [f.__name__ for f in model.tools_seen[0]]
    assert names == [
        "current_time",
        "read_skill",
        "search_history",
        "propose_fact",
        "list_pending",
    ]


async def test_turn_is_fully_recorded_in_journal(steward_factory):
    """可见即入账:一轮的每个环节都要能从起居注重建。"""
    reply = ModelReply(
        text="记下了",
        tool_events=[
            {"type": "tool_call", "tool": "propose_fact", "args": {"content": "对芒果过敏"}},
            {"type": "tool_result", "tool": "propose_fact", "content": "已记下"},
        ],
        cache_hit_tokens=512,
        prompt_tokens=1024,
        completion_tokens=20,
    )
    steward, _ = steward_factory([reply])
    env = Envelope.new(source="user", channel="cli", content="我对芒果过敏")
    steward.submit(env)
    await steward.process_next()

    kinds = [e["kind"] for e in steward.journal.replay(env.id)]
    assert kinds == ["envelope", "prompt", "tool_call", "tool_result", "reply"]


async def test_recorded_prompt_matches_what_model_received(steward_factory):
    """重放的前提:落账的 prompt 必须就是模型真收到的那份。"""
    steward, model = steward_factory([ModelReply(text="好")])
    env = Envelope.new(source="user", channel="cli", content="测试")
    steward.submit(env)
    await steward.process_next()

    recorded = next(e for e in steward.journal.replay(env.id) if e["kind"] == "prompt")
    assert recorded["payload"]["system_prompt"] == model.seen[0].system_prompt
    assert recorded["payload"]["messages"] == model.seen[0].messages


async def test_second_turn_sees_first_turn_in_l0(steward_factory):
    steward, model = steward_factory([ModelReply(text="第一答"), ModelReply(text="第二答")])
    steward.submit(Envelope.new(source="user", channel="cli", content="第一问"))
    await steward.process_next()
    steward.submit(Envelope.new(source="user", channel="cli", content="第二问"))
    await steward.process_next()

    second_ctx = model.seen[1]
    assert any("第一问" in m["content"] for m in second_ctx.messages)
    assert any("第一答" in m["content"] for m in second_ctx.messages)


async def test_prefix_identical_between_turns_when_ledger_unchanged(steward_factory):
    """跨轮缓存命中的前提。"""
    steward, model = steward_factory([ModelReply(text="一"), ModelReply(text="二")])
    for content in ("第一问", "第二问"):
        steward.submit(Envelope.new(source="user", channel="cli", content=content))
        await steward.process_next()
    assert model.seen[0].system_prompt == model.seen[1].system_prompt


async def test_settled_fact_appears_in_next_prefix(steward_factory):
    steward, model = steward_factory([ModelReply(text="一"), ModelReply(text="二")])
    steward.submit(Envelope.new(source="user", channel="cli", content="第一问"))
    await steward.process_next()

    steward.gate.propose(
        kind="add",
        content="对芒果过敏",
        provenance="user_stated",
        origin="test",
        section="长期偏好",
    )
    assert steward.settle_if_needed() == 1

    steward.submit(Envelope.new(source="user", channel="cli", content="第二问"))
    await steward.process_next()
    assert "对芒果过敏" in model.seen[1].system_prompt


async def test_model_failure_logs_error_and_does_not_wedge_the_queue(steward_factory):
    """崩了要留痕,而且不能把串行队列永久卡在 processing 上。"""

    class Boom:
        async def run(self, ctx, tools, mcp_servers):
            raise RuntimeError("模型炸了")

    steward, _ = steward_factory()
    steward.model = Boom()
    env = Envelope.new(source="user", channel="cli", content="会炸")
    steward.submit(env)

    with pytest.raises(RuntimeError):
        await steward.process_next()

    errors = [e for e in steward.journal.replay(env.id) if e["kind"] == "error"]
    assert len(errors) == 1
    assert "模型炸了" in errors[0]["payload"]["content"]

    # 失败的信封已出队,下一条能被认领
    steward.submit(Envelope.new(source="user", channel="cli", content="下一条"))
    claimed = steward.inbox.claim_next()
    assert claimed is not None and claimed.content == "下一条"
