"""报文级测试:断言模型**实际收到**什么,而不是断言组装器输出了什么。

M1 审计的 P0-1 就长在这条边界上——所有测试都停在 AssembledContext,而把
AssembledContext 翻译成真实报文的 PydanticAIClient.run 零覆盖,于是
"pydantic-ai 在 message_history 非空时丢掉 system_prompt"这件事,
在四关全绿的掩护下藏了整个 M1。这个文件的存在就是为了让它不能再藏。
"""

from typing import Any

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from lararium.config import Settings
from lararium.steward.assembler import AssembledContext
from lararium.steward.model import PydanticAIClient


@pytest.fixture
def wire(monkeypatch):
    """真实的 PydanticAIClient,但底层模型换成能捕获报文的 FunctionModel。"""
    monkeypatch.setenv("LARARIUM_API_KEY", "sk-test")
    captured: list[list[Any]] = []

    def spy(messages: list[Any], info: Any) -> ModelResponse:
        captured.append(messages)
        return ModelResponse(parts=[TextPart("ok")])

    client = PydanticAIClient(Settings.load(), model=FunctionModel(spy))
    return client, captured


def part_kinds(messages: list[Any]) -> list[str]:
    return [getattr(p, "part_kind", "") for m in messages for p in m.parts]


def system_texts(messages: list[Any]) -> list[str]:
    return [
        str(p.content)
        for m in messages
        for p in m.parts
        if getattr(p, "part_kind", "") == "system-prompt"
    ]


def ctx(
    *, prefix: str = "【前缀】", history: list[tuple[str, str]] = (), now: str = "本轮"
) -> AssembledContext:
    messages: list[dict[str, str]] = []
    for user, assistant in history:
        messages.append({"role": "user", "content": user})
        messages.append({"role": "assistant", "content": assistant})
    messages.append({"role": "user", "content": now})
    return AssembledContext(system_prompt=prefix, messages=messages)


async def test_prefix_reaches_the_model_on_the_first_turn(wire):
    client, captured = wire
    await client.run(ctx(), [], [])
    assert system_texts(captured[-1]) == ["【前缀】"]


async def test_prefix_still_reaches_the_model_on_later_turns(wire):
    """★ P0-1 的回归测试。修复前这里拿到 0 条前缀。"""
    client, captured = wire
    await client.run(ctx(history=[("问1", "答1"), ("问2", "答2")]), [], [])
    assert system_texts(captured[-1]) == ["【前缀】"], "第二轮起前缀丢了,账本和人格没发出去"


async def test_prefix_appears_exactly_once_and_first(wire):
    """前缀必须在报文最前面且只有一份——重复一份等于白烧一遍前缀的钱。"""
    client, captured = wire
    await client.run(ctx(history=[("问1", "答1")]), [], [])
    kinds = part_kinds(captured[-1])
    assert kinds.count("system-prompt") == 1
    assert kinds[0] == "system-prompt"


async def test_prefix_is_byte_identical_across_turns(wire):
    """缓存命中的硬约束,在报文层面复核一遍(Task 12 只在组装器层面验过)。"""
    client, captured = wire
    await client.run(ctx(now="第一问"), [], [])
    await client.run(ctx(history=[("第一问", "答1")], now="第二问"), [], [])
    assert len({tuple(system_texts(m)) for m in captured}) == 1


async def test_history_reaches_the_model_in_order(wire):
    client, captured = wire
    await client.run(ctx(history=[("问1", "答1")], now="问2"), [], [])
    texts = [str(getattr(p, "content", "")) for m in captured[-1] for p in m.parts]
    assert texts == ["【前缀】", "问1", "答1", "问2"]
