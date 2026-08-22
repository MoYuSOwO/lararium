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


# ── M4-5c v2:历史里的工具往返必须以原生形状发出去 ─────────────────────────


async def test_history_tool_exchange_is_sent_as_native_tool_calls(wire):
    """★ M4-5c v2 的要害:断言**真正发出去的报文**里,历史工具调用走的是协议字段。

    v1 把它渲染成助手正文里的一行字,模型学会了写那一行来代替调那个工具
    (5/5 漏出的痕迹行零真实调用)。原生形状下调用在 `tool_calls` 字段里、
    结果是 `role: "tool"` 的独立消息——**正文通道里写什么都伪造不出一次调用**。
    这条只信 HTTP body,不信库内部表示(补1b 的教训:FunctionModel 看不见适配器)。
    """
    client, bodies = wire
    ctx = AssembledContext(
        system_prompt=PREFIX,
        messages=[
            {"role": "user", "content": "打车 28"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "env-abcd-0", "name": "record_expense", "args": '{"amount": 28}'}
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "env-abcd-0",
                "name": "record_expense",
                "content": "记好了。",
            },
            {"role": "assistant", "content": "记好了:打车 28 元。"},
            {"role": "user", "content": "再来一笔"},
        ],
    )
    await client.run(ctx, [], [])

    msgs = bodies[-1]["messages"]
    call_msg = next(m for m in msgs if m.get("tool_calls"))
    assert call_msg["role"] == "assistant"
    assert call_msg["tool_calls"][0]["id"] == "env-abcd-0"
    assert call_msg["tool_calls"][0]["function"]["name"] == "record_expense"
    assert "28" in call_msg["tool_calls"][0]["function"]["arguments"]

    tool_msg = next(m for m in msgs if m.get("role") == "tool")
    assert tool_msg["tool_call_id"] == "env-abcd-0"
    assert tool_msg["content"] == "记好了。"

    # 正文通道里不许出现工具痕迹——v1 就是把它写在这儿才被伪造的
    assert all("record_expense" not in (m.get("content") or "") for m in msgs)
