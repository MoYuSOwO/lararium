"""报文级测试:断言真正发出去的 HTTP body。"""

import json
from typing import Any

import httpx
import pytest

from lararium.steward.assembler import AssembledContext

PREFIX = "【前缀】"


@pytest.fixture
def wire(http_spy_factory, reply_factories):
    """真实 PydanticAIClient + 真实 OpenAIChatModel,只把 HTTP 传输换掉。"""
    text_reply, tool_call_reply = reply_factories
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        wants_tool_round_trip = bool(body.get("tools")) and not any(
            m.get("role") == "tool" for m in body["messages"]
        )
        return httpx.Response(
            200, json=tool_call_reply() if wants_tool_round_trip else text_reply()
        )

    return http_spy_factory(handler), bodies


def ctx(*, history: tuple[tuple[str, str], ...] = (), now: str = "本轮") -> AssembledContext:
    messages: list[dict[str, str]] = []
    for user, assistant in history:
        messages.append({"role": "user", "content": user})
        messages.append({"role": "assistant", "content": assistant})
    messages.append({"role": "user", "content": now})
    return AssembledContext(system_prompt=PREFIX, messages=messages)


def head(body: dict[str, Any]) -> str:
    return json.dumps(body["messages"][0], ensure_ascii=False, sort_keys=True)


async def test_prefix_is_the_first_message_on_the_first_turn(wire):
    client, bodies = wire
    await client.run(ctx(), [], [])
    assert bodies[-1]["messages"][0] == {"role": "system", "content": PREFIX}


async def test_prefix_is_still_the_first_message_on_later_turns(wire):
    """★ P0-1 的回归测试。"""
    client, bodies = wire
    await client.run(ctx(history=(("问1", "答1"), ("问2", "答2"))), [], [])
    assert bodies[-1]["messages"][0] == {"role": "system", "content": PREFIX}


async def test_prefix_appears_exactly_once(wire):
    client, bodies = wire
    await client.run(ctx(history=(("问1", "答1"),)), [], [])
    assert [m for m in bodies[-1]["messages"] if m["role"] == "system"] == [
        {"role": "system", "content": PREFIX}
    ]


async def test_prefix_is_byte_identical_across_turns(wire):
    client, bodies = wire
    await client.run(ctx(now="第一问"), [], [])
    await client.run(ctx(history=(("第一问", "答1"),), now="第二问"), [], [])
    assert len({head(b) for b in bodies}) == 1


async def test_history_reaches_the_model_in_order(wire):
    client, bodies = wire
    await client.run(ctx(history=(("问1", "答1"),), now="问2"), [], [])
    assert [(m["role"], m["content"]) for m in bodies[-1]["messages"]] == [
        ("system", PREFIX),
        ("user", "问1"),
        ("assistant", "答1"),
        ("user", "问2"),
    ]


async def test_prefix_survives_a_tool_round_trip(wire):
    """一轮里调一次工具 = 两次 HTTP 请求。工具往返是最常见的情况,
    也恰恰是前缀最容易被挤走的时候,而它之前一条测试都没有。"""

    def current_time() -> str:
        """返回时间"""
        return "2026-08-17T22:00:00+08:00"

    client, bodies = wire
    await client.run(ctx(history=(("问1", "答1"),), now="现在几点"), [current_time], [])
    assert len(bodies) == 2, f"预期两次请求,实际 {len(bodies)}"
    for i, b in enumerate(bodies, 1):
        assert b["messages"][0] == {"role": "system", "content": PREFIX}, f"第{i}次请求前缀不对"
    assert len({head(b) for b in bodies}) == 1
