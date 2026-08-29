"""M5-3:微信适配器——上面接 iLink,下面调 Lararium 的 HTTP 接口。

**和 `cli.py` 一个位置,只是换了个说话的对象**:纯客户端,不 import steward/bundles
(`.importlinter` 有契约钉着)。**独立进程**——iLink 掉线要重连,而重启一次不该把 542 MB
的 embedding 跟着重载一遍;微信那边抽风也不该让 Steward 跟着死。

这一步只做"能收能发"。审批卡是 M5-4。
"""

import json

import httpx
import pytest

from lararium.gateway.ilink import Credentials, ILinkError, InboundMessage
from lararium.gateway.wechat import State, WeChatAdapter


class FakeILink:
    """假的 iLink 端:记下发了什么,按剧本回什么。"""

    def __init__(self, batches=(), fail_with=None):
        self._batches = list(batches)
        self._fail_with = fail_with
        self.sent: list[tuple[str, str, str]] = []
        self.logins = 0

    async def get_updates(self, cursor):
        if self._fail_with is not None:
            error, self._fail_with = self._fail_with, None
            raise error
        if not self._batches:
            return [], cursor
        return self._batches.pop(0)

    async def send_text(self, *, to_user_id, text, context_token):
        self.sent.append((to_user_id, text, context_token))

    async def request_qrcode(self):
        return "qr-token", "https://liteapp/q/new"

    async def poll_qrcode_status(self, qrcode):
        self.logins += 1
        return Credentials(bot_token="tok-new", bot_id="b", user_id="u", base_url="https://ilink")


def lararium(handler):
    return httpx.AsyncClient(
        base_url="http://127.0.0.1:8420", transport=httpx.MockTransport(handler)
    )


def adapter(tmp_path, ilink, handler, **state):
    path = tmp_path / "wechat" / "state.json"
    return WeChatAdapter(
        ilink=ilink,
        lararium=lararium(handler),
        token="tok-wechat",
        state=State(path=path, **state),
    )


# ── 状态持久化 ──────────────────────────────────────────────────────────


def test_state_survives_a_restart(tmp_path):
    """游标、context_token、出件箱位置都要落盘——不存就会重收、漏收、或重发。"""
    path = tmp_path / "wechat" / "state.json"
    State(path=path, cursor="c1", context_token="ctx", peer="u1", outbox_after=7).save()

    again = State.load(path)

    assert (again.cursor, again.context_token, again.peer, again.outbox_after) == (
        "c1",
        "ctx",
        "u1",
        7,
    )


def test_a_corrupt_state_file_does_not_block_startup(tmp_path):
    """状态文件坏了就从零开始,不打崩启动。

    最坏后果是重收一批消息(至少能用),而崩在启动上的后果是**助手整个不在了**
    ——用户什么都收不到,也不知道为什么。
    """
    path = tmp_path / "wechat" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text("{这不是 json", encoding="utf-8")

    assert State.load(path).cursor == ""


def test_state_is_written_atomically(tmp_path):
    """先写临时文件再 rename:半截的状态文件比没有更坏。

    断言目录里只留下最终文件——写到一半被杀不会留下一个能被 json 解析、内容却残缺的文件。
    """
    path = tmp_path / "wechat" / "state.json"
    State(path=path, cursor="c1").save()

    assert [p.name for p in path.parent.iterdir()] == ["state.json"]


# ── 收信 ────────────────────────────────────────────────────────────────


async def test_an_inbound_message_is_posted_to_lararium(tmp_path):
    """收到微信消息 → `POST /v1/messages`,带控制端 token。"""
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"envelope_id": "e1"})

    ilink = FakeILink(
        batches=[
            ([InboundMessage(1, "u1@im.wechat", "打车 28", "ctx-1")], "cursor-2"),
        ]
    )
    a = adapter(tmp_path, ilink, handler)

    await a.pump_inbound_once()

    assert seen[0].url.path == "/v1/messages"
    assert seen[0].headers["authorization"] == "Bearer tok-wechat"
    assert json.loads(seen[0].content) == {"content": "打车 28"}


async def test_the_cursor_and_context_token_are_persisted_after_each_batch(tmp_path):
    """游标和 `context_token` 收到就存。

    `context_token` 是**主动推送的凭据**(官方实现里它没有过期逻辑,收到新消息就覆盖)
    ——丢了它,M4-7 那条早报就发不出去。
    """
    ilink = FakeILink(batches=[([InboundMessage(1, "u1@im.wechat", "你好", "ctx-1")], "cursor-2")])
    a = adapter(tmp_path, ilink, lambda _r: httpx.Response(200, json={"envelope_id": "e1"}))

    await a.pump_inbound_once()

    saved = State.load(a.state.path)
    assert saved.cursor == "cursor-2"
    assert saved.context_token == "ctx-1"
    assert saved.peer == "u1@im.wechat"


async def test_an_empty_long_poll_is_not_an_error(tmp_path):
    """长轮询空返回是常态(服务端挂到超时),不该当异常也不该刷屏。"""
    a = adapter(tmp_path, FakeILink(), lambda _r: httpx.Response(500))

    await a.pump_inbound_once()  # 不抛就算过


# ── 发信 ────────────────────────────────────────────────────────────────


async def test_outbox_items_are_delivered_to_wechat(tmp_path):
    """出件箱里的回复 → 经 iLink 发到用户微信;`after` 游标随之推进并落盘。"""

    def handler(_request):
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "seq": 4,
                        "envelope_id": "e1",
                        "channel": "wechat",
                        "kind": "reply",
                        "content": "记好了",
                    },
                ]
            },
        )

    ilink = FakeILink()
    a = adapter(tmp_path, ilink, handler, context_token="ctx-1", peer="u1@im.wechat")

    await a.pump_outbox_once()

    assert ilink.sent == [("u1@im.wechat", "记好了", "ctx-1")]
    assert State.load(a.state.path).outbox_after == 4


async def test_the_outbox_cursor_prevents_resending_after_a_restart(tmp_path):
    """`after` 持久化:重启不重发。用户收到两遍同一句回复,比没收到还糟。"""
    asked: list[str] = []

    def handler(request):
        asked.append(str(request.url))
        return httpx.Response(200, json={"items": []})

    a = adapter(tmp_path, FakeILink(), handler, outbox_after=9)

    await a.pump_outbox_once()

    assert "after=9" in asked[0]


async def test_a_push_with_no_known_peer_is_left_in_the_outbox(tmp_path):
    """还没人跟它说过话时,推送**留在出件箱里等**,不许把游标推过去。

    M4-7 说过:失效形态应该是"消息在出件箱里等你开口",而不是"发不出去就丢了"。
    """

    def handler(_request):
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "seq": 4,
                        "envelope_id": "e1",
                        "channel": "wechat",
                        "kind": "notice",
                        "content": "这个月餐饮 1240",
                    }
                ]
            },
        )

    ilink = FakeILink()
    a = adapter(tmp_path, ilink, handler)  # 没有 peer / context_token

    await a.pump_outbox_once()

    assert ilink.sent == []
    assert State.load(a.state.path).outbox_after == 0, "游标不许推过去,不然这条就永远丢了"


# ── 重连 ────────────────────────────────────────────────────────────────


async def test_a_stale_token_triggers_a_relogin_not_an_hour_of_downtime(tmp_path):
    """★ `-14` → **重连**,不是照抄官方那样停机一小时。

    官方 `session-guard.ts` 把 -14 当 token 过期、暂停该账号所有 API 一小时;但实测
    缺 HTTP 头时返回的也是 -14。头由一处构造、每次全带(ilink.py 有报文级测试钉着),
    所以这里 -14 只剩"token 真的失效"一种解释——直接重新扫码,不停机。
    """
    ilink = FakeILink(fail_with=ILinkError("session timeout", code=-14))
    a = adapter(tmp_path, ilink, lambda _r: httpx.Response(200, json={"items": []}))

    await a.pump_inbound_once()

    assert ilink.logins == 1
    assert a.ilink_token == "tok-new"
    assert State.load(a.state.path).bot_token == "tok-new"


async def test_the_new_qrcode_is_pushed_to_the_user_when_still_reachable(tmp_path):
    """重连要重新扫码——**把新二维码当消息发给用户**,否则每天都得 SSH 上服务器扫一次。

    尽力而为:旧凭据要是已经彻底失效,这条发不出去,那就只能落日志。
    发不出去**不许**打断重连本身。
    """
    ilink = FakeILink(fail_with=ILinkError("session timeout", code=-14))
    a = adapter(
        tmp_path,
        ilink,
        lambda _r: httpx.Response(200, json={"items": []}),
        context_token="ctx-1",
        peer="u1@im.wechat",
    )

    await a.pump_inbound_once()

    assert any("liteapp" in text for _to, text, _ctx in ilink.sent), ilink.sent


async def test_a_failure_to_deliver_the_qrcode_does_not_break_the_relogin(tmp_path):
    """二维码发不出去(旧 token 已死)时,重连照常完成——只是用户得自己去看日志。"""

    class Unreachable(FakeILink):
        async def send_text(self, **_kwargs):
            raise ILinkError("session timeout", code=-14)

    ilink = Unreachable(fail_with=ILinkError("session timeout", code=-14))
    a = adapter(
        tmp_path,
        ilink,
        lambda _r: httpx.Response(200, json={"items": []}),
        context_token="ctx-1",
        peer="u1@im.wechat",
    )

    await a.pump_inbound_once()

    assert a.ilink_token == "tok-new"


async def test_other_ilink_errors_are_not_silently_swallowed(tmp_path):
    """不是 -14 的错误照样往上抛——吞掉等于把 bug 埋进日志(E1)。"""
    ilink = FakeILink(fail_with=ILinkError("boom", code=-1))
    a = adapter(tmp_path, ilink, lambda _r: httpx.Response(200, json={"items": []}))

    with pytest.raises(ILinkError, match="boom"):
        await a.pump_inbound_once()
