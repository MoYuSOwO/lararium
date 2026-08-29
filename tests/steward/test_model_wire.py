"""报文级测试:断言真正发出去的 HTTP body。"""

import base64
import json
from typing import Any

import httpx
import pytest

from lararium.steward.assembler import AssembledContext
from lararium.steward.vision import ImagePart, ImageReturn

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


# ── M5-5 读图 ───────────────────────────────────────────────────────────


def ctx_with_image(*, history: tuple[tuple[str, str], ...] = ()) -> AssembledContext:
    messages: list[dict[str, Any]] = []
    for user, assistant in history:
        messages.append({"role": "user", "content": user})
        messages.append({"role": "assistant", "content": assistant})
    messages.append(
        {
            "role": "user",
            "content": "这是啥",
            "images": [ImagePart(sha256="ab" * 32, media_type="image/jpeg", data=b"JPEGBYTES")],
        }
    )
    return AssembledContext(system_prompt=PREFIX, messages=messages)


async def test_the_image_actually_goes_out_on_the_wire(wire):
    """报文级:图真的发出去了,而且和那句正文在**同一条 user 消息**里。

    断言发出去的字节而不是内部状态——"框定语和图在一起"这条,只有在这里才算证到。
    """
    client, bodies = wire
    await client.run(ctx_with_image(), [], [])

    last = bodies[-1]["messages"][-1]
    assert last["role"] == "user"
    kinds = [p["type"] for p in last["content"]]
    assert kinds == ["text", "image_url"], f"报文形状不对:{kinds}"
    assert last["content"][0]["text"] == "这是啥"
    assert last["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert base64.b64decode(last["content"][1]["image_url"]["url"].split(",", 1)[1]) == b"JPEGBYTES"


async def test_history_turns_carry_no_image_bytes(wire):
    """★ 约束 1 的报文级证明:历史轮里一个字节的图都不许有。

    只看组装器的结构断言不够——真正要证的是"发出去的那份"里没有。图片留在历史里的话,
    成本和注入面都会**永久地**乘进后续每一轮。
    """
    client, bodies = wire
    await client.run(
        ctx_with_image(history=(("昨天那张呢", "(图片 · media/abababababab…)"),)), [], []
    )

    parts = [
        p
        for m in bodies[-1]["messages"]
        if isinstance(m.get("content"), list)
        for p in m["content"]
    ]
    assert [p["type"] for p in parts] == ["text", "image_url"], "整份报文里图不止一张"
    earlier = bodies[-1]["messages"][:-1]
    assert all(isinstance(m.get("content"), str) for m in earlier), f"历史轮不是纯文本:{earlier}"


async def test_a_tool_can_hand_an_image_back_without_putting_bytes_in_the_journal(
    http_spy_factory, reply_factories
):
    """★ 「重新看一眼」这条路要同时满足两件事,而它们互相拉扯:

    图必须真的到达模型(否则这个工具是摆设),但**进起居注的那一份不能是字节**
    ——`tool_result` 会进全文索引、进 L0、被 replay 反复 json.loads。所以工具返回的
    是中立的 `ImageReturn`,隔离盒把它拆成:一行人话当 return_value(那份进起居注),
    字节走 content(那份只上报文)。

    这里断言的是**发出去的字节**和**上报的 tool_events**两头,不是内部状态。
    """
    text_reply, tool_call_reply = reply_factories
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if not any(m.get("role") == "tool" for m in body["messages"]):
            reply = tool_call_reply()
            reply["choices"][0]["message"]["tool_calls"][0]["function"]["name"] = "look_at_image"
            return httpx.Response(200, json=reply)
        return httpx.Response(200, json=text_reply("看到了"))

    def look_at_image() -> Any:
        """重新看一眼"""
        return ImageReturn(
            text="(重新附上 media/abababababab…)",
            images=(ImagePart(sha256="ab" * 32, media_type="image/png", data=b"PNGBYTES"),),
        )

    client = http_spy_factory(handler)
    reply = await client.run(ctx(), [look_at_image], [])

    # ① 图真的发出去了(第二次请求里)
    parts = [
        p
        for m in bodies[-1]["messages"]
        if isinstance(m.get("content"), list)
        for p in m["content"]
    ]
    assert any(p["type"] == "image_url" for p in parts), f"图没上报文:{bodies[-1]['messages'][-1]}"

    # ② 要落起居注的那一份是一行人话,没有任何字节
    results = [e for e in reply.tool_events if e["type"] == "tool_result"]
    assert results and results[0]["content"] == "(重新附上 media/abababababab…)"
