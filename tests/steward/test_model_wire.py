"""报文级测试:断言真正发出去的 HTTP body。"""

import base64
import json
from typing import Any

import httpx
import pytest

from lararium.steward.assembler import AssembledContext
from lararium.steward.model import ModelCallError, unwrap_tool_args
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


# ── M5-13:工具重试耗尽时,把重试提示原文捞出来 ──────────────────────────


async def test_an_exhausted_tool_retry_carries_the_feedback_out(http_spy_factory):
    """★ `Tool 'x' exceeded max retries count of 1` 这一行本身什么都没说。

    pydantic-ai 把校验详情吞在异常正文之外,起居注里也只剩那一行——真机上这一轮变
    `retry_later`、重试耗尽后用户收到「处理失败,已放弃」,而**没有任何地方能告诉你
    模型到底填错了什么**。三种抓报文的办法都没拦到那条 client(它不走那几层)。

    重试提示会作为一条 `tool` 消息回给模型,所以它一定在库自己的消息流里。
    这条断言的是:它被带出了隔离盒——**模型填的参数**和**服务端给的反馈**都要有,
    少任何一半都不够定位(只有反馈不知道它填了什么,只有参数不知道哪里不合法)。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        reply = tool_call_reply_factory()
        return httpx.Response(200, json=reply)

    def tool_call_reply_factory() -> dict[str, Any]:
        return {
            "id": "1",
            "object": "chat.completion",
            "created": 0,
            "model": "m",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {
                                    "name": "record_expense",
                                    # 金额给成一句话:参数校验必然失败
                                    "arguments": '{"amount": "一百块", "category": "餐饮"}',
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        }

    def record_expense(amount: float, category: str) -> str:
        """记一笔消费"""
        return "记好了"

    client = http_spy_factory(handler)

    with pytest.raises(ModelCallError) as caught:
        await client.run(ctx(), [record_expense], [])

    details = caught.value.details
    assert details, "重试耗尽了,却什么细节都没带出来——和修之前一样查不动"
    assert details[0]["tool"] == "record_expense"
    assert "一百块" in details[0]["args"], f"没带上模型填的参数:{details[0]}"
    assert "amount" in details[0]["feedback"], f"没带上服务端的反馈:{details[0]}"


async def test_a_normal_run_carries_no_retry_details(http_spy_factory, reply_factories):
    """反向:没出事的时候不许挂着一坨东西——它只在出错那条路上生效。"""
    text_reply, _ = reply_factories
    client = http_spy_factory(lambda _r: httpx.Response(200, json=text_reply("好")))

    reply = await client.run(ctx(), [], [])

    assert reply.text == "好"


# ── M5-13 Step 2:服务商多包的那层 `arguments` 信封 ──────────────────────


def wrapped_call_reply(name: str, inner: str) -> dict[str, Any]:
    """服务商实测回过的形状:`function.arguments` 里又套了一层 `arguments`。"""
    return {
        "id": "1",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": name, "arguments": inner},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }


async def test_a_doubly_wrapped_arguments_envelope_is_unwrapped(http_spy_factory, reply_factories):
    """★ M5-13:AMD 那个自部署端点会**偶发地把参数多包一层**:

        {"arguments": {"amount": 5, "category": "交通", "note": "地铁"}}

    校验于是报 `missing: amount` / `missing: category` / `extra_forbidden: arguments`,
    一轮里两次都包错就把工具重试耗尽,用户收到「处理失败,已放弃」——**一笔账就没了**。
    在隔离盒里把这层剥掉,而不是把 retries 调大(调大只是让它多错两次)。
    """
    text_reply, _ = reply_factories
    seen: list[tuple[float, str]] = []

    def record_expense(amount: float, category: str, note: str = "") -> str:
        """记一笔消费"""
        seen.append((amount, category))
        return "记好了"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if any(m.get("role") == "tool" for m in body["messages"]):
            return httpx.Response(200, json=text_reply("记好了"))
        return httpx.Response(
            200,
            json=wrapped_call_reply(
                "record_expense",
                '{"arguments": {"amount": 5, "category": "交通", "note": "地铁"}}',
            ),
        )

    await http_spy_factory(handler).run(ctx(), [record_expense], [])

    assert seen == [(5, "交通")], "多包的那层没剥掉,这一笔又丢了"


async def test_normal_arguments_are_left_alone(http_spy_factory, reply_factories):
    """反向:形状正常的调用一个字节都不许动。"""
    text_reply, _ = reply_factories
    seen: list[tuple[float, str]] = []

    def record_expense(amount: float, category: str, note: str = "") -> str:
        """记一笔消费"""
        seen.append((amount, category))
        return "记好了"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if any(m.get("role") == "tool" for m in body["messages"]):
            return httpx.Response(200, json=text_reply("记好了"))
        return httpx.Response(
            200,
            json=wrapped_call_reply(
                "record_expense", '{"amount": 28, "category": "交通", "note": "打车"}'
            ),
        )

    await http_spy_factory(handler).run(ctx(), [record_expense], [])

    assert seen == [(28, "交通")]


async def test_a_tool_that_really_takes_arguments_is_not_unwrapped(
    http_spy_factory, reply_factories
):
    """★ 反向的要害:**真有一个叫 `arguments` 的参数时不许剥**。

    判据不是"长得像信封",是**那个工具的 schema 里到底有没有这个参数**——
    照形状猜的话,总有一天会把一次合法调用拆散,而症状是参数凭空少了一半。
    """
    text_reply, _ = reply_factories
    seen: list[dict] = []

    def relay(arguments: dict) -> str:
        """转发一段参数"""
        seen.append(arguments)
        return "转发了"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if any(m.get("role") == "tool" for m in body["messages"]):
            return httpx.Response(200, json=text_reply("好"))
        return httpx.Response(200, json=wrapped_call_reply("relay", '{"arguments": {"a": 1}}'))

    await http_spy_factory(handler).run(ctx(), [relay], [])

    assert seen == [{"a": 1}], "把一次合法调用拆散了"


@pytest.mark.parametrize(
    ("args", "schema", "expected"),
    [
        # 正例:恰好一个 arguments 信封,而工具本身没有这个参数
        ('{"arguments": {"amount": 5}}', {"properties": {"amount": {}}}, '{"amount": 5}'),
        ({"arguments": {"amount": 5}}, {"properties": {"amount": {}}}, {"amount": 5}),
        # 形状正常:不动
        ('{"amount": 5}', {"properties": {"amount": {}}}, None),
        # ★ 带兄弟键:**不许剥**。`note` 是模型真想传的参数,剥了它就凭空消失,
        # 而症状是"它记的东西少了一块",没有任何报错。宁可走原来的校验失败(至少响)。
        ('{"arguments": {"amount": 5}, "note": "别丢了我"}', {"properties": {"amount": {}}}, None),
        # 工具真有 arguments 这个参数:不许剥
        ('{"arguments": {"a": 1}}', {"properties": {"arguments": {}}}, None),
        # 里面不是对象:剥了也没用
        ('{"arguments": "一句话"}', {"properties": {"amount": {}}}, None),
        # 根本不是 JSON:别猜
        ("这不是 json", {"properties": {"amount": {}}}, None),
    ],
)
def test_unwrap_only_strips_an_exact_envelope(args, schema, expected):
    """剥壳的判据逐条钉死。

    它是一条**为某一家服务商的毛病开的口子**,而这种口子最容易越开越大:
    今天"含有 arguments 就剥",明天就有一次合法调用被拆散。所以正例反例一起钉。
    """
    assert unwrap_tool_args(args, schema) == expected


# ── M5-16:多轮历史的报文形状,把人工审计钉成常驻门禁 ────────────────────


def audit_history(messages: list[dict[str, Any]]) -> list[str]:
    """报文里工具往返的四项检查,和 M5-16 手工 dump 时用的是同一套判据。"""
    problems: list[str] = []
    calls: dict[str, int] = {}
    for i, m in enumerate(messages):
        if m.get("role") == "assistant":
            for c in m.get("tool_calls") or []:
                calls[c["id"]] = i
        elif m.get("role") == "tool":
            cid = m.get("tool_call_id")
            if cid not in calls:
                problems.append(f"#{i} tool 消息 {cid!r} 没有前置的 assistant 调用")
            elif calls[cid] > i:
                problems.append(f"#{i} tool 消息排在它的调用之前")
    answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    problems += [f"调用 {cid} 没有配对结果(孤儿)" for cid in calls if cid not in answered]
    return problems


async def test_the_rebuilt_history_is_well_formed_on_the_wire(wire):
    """★ M5-16:**我们自己重建的那份多轮历史,发出去必须是合法的。**

    M4-5c v2 起,历史里的工具往返是我们从起居注重建的(assistant 带 tool_calls +
    配对的 tool 消息)。重建里只要有一处不对——id 配不上、有结果没有调用、顺序错位
    ——任何模型都会被带歪,而症状是"它记错了账",没有人会往报文上想。

    M5-16 手工 dump 过一次、逐条查干净了;这条把那次审计钉成常驻的,免得下次改
    assembler 时悄悄破掉,又要重新查一遍才发现。
    """
    client, bodies = wire
    history = (("打车 28", "记好了。"), ("买菜 62", "记好了。"))
    messages: list[dict[str, Any]] = []
    for i, (user, assistant) in enumerate(history):
        messages.append({"role": "user", "content": user})
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": f"c{i}", "name": "record_expense", "args": '{"amount": 28}'}],
            }
        )
        messages.append(
            {"role": "tool", "tool_call_id": f"c{i}", "name": "record_expense", "content": "记好了"}
        )
        messages.append({"role": "assistant", "content": assistant})
    messages.append({"role": "user", "content": "咖啡 22"})

    await client.run(AssembledContext(system_prompt=PREFIX, messages=messages), [], [])

    sent = bodies[-1]["messages"]
    assert audit_history(sent) == [], f"发出去的历史形状不合法:{audit_history(sent)}"
    # 阳性对照:这份报文里**确实有**工具往返,不然上面那条是在审计一份空历史
    assert sum(1 for m in sent if m.get("role") == "tool") == len(history)


def test_the_audit_itself_catches_a_broken_history():
    """给审计自己的阳性对照:它得真能否掉东西,不然它只是一句好听的话。"""
    orphan_result = [{"role": "tool", "tool_call_id": "x", "content": "结果"}]
    orphan_call = [
        {"role": "assistant", "tool_calls": [{"id": "y", "function": {"name": "f"}}]},
    ]

    assert audit_history(orphan_result), "有结果没有调用,居然没查出来"
    assert audit_history(orphan_call), "有调用没有结果,居然没查出来"
