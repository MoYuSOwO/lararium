"""M5-3:微信适配器——上面接 iLink,下面调 Lararium 的 HTTP 接口。

**和 `cli.py` 一个位置,只是换了个说话的对象**:纯客户端,不 import steward/bundles
(`.importlinter` 有契约钉着)。**独立进程**——iLink 掉线要重连,而重启一次不该把 542 MB
的 embedding 跟着重载一遍;微信那边抽风也不该让 Steward 跟着死。

这一步只做"能收能发"。审批卡是 M5-4。
"""

import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path

import httpx
import pytest

from lararium.gateway import wechat
from lararium.gateway.ilink import Credentials, ILinkError, InboundMessage, MediaRef, QrStatus
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
        # 别立即返回:没有 await 挂起点的循环连 asyncio.wait_for 都打断不了。
        await asyncio.sleep(0)
        self.logins += 1
        return QrStatus(
            raw="confirmed",
            credentials=Credentials(
                bot_token="tok-new", bot_id="b", user_id="u", base_url="https://ilink"
            ),
        )


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
        media_dir=tmp_path / "media",
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
    assert json.loads(seen[0].content) == {"content": "打车 28", "attachments": []}


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


# ── 恢复路径:二维码过期 ─────────────────────────────────────────────────
#
# 失效剧本:凌晨三点会话到期 → 二维码发到微信 → 你在睡觉 → 几分钟后码过期 →
# 适配器对着一个死码轮询到天亮 → 你早上回消息毫无反应。助手静默死掉,只能人工重启。
# 这是**恢复路径**上的第二个洞:第一个(二维码发不出去)已经防住了,这个没有。


class ExpiringILink(FakeILink):
    """第 `expire_after` 次轮询之后,这个码就过期了;新申请的码可以正常确认。"""

    def __init__(self, *, expire_after: int, fail_with=None):
        super().__init__(fail_with=fail_with)
        self.expire_after = expire_after
        self.qr_issued = 0
        self.polls = 0

    async def request_qrcode(self):
        self.qr_issued += 1
        return f"qr-{self.qr_issued}", f"https://liteapp/q/{self.qr_issued}"

    async def poll_qrcode_status(self, qrcode):
        # **别立即返回**:没有 await 挂起点的 while True 是个热循环,
        # 连 asyncio.wait_for 都打断不了——那样测试自己会挂死,而那是一次
        # "场景没发生"的假绿。
        await asyncio.sleep(0)
        self.polls += 1
        if self.qr_issued == 1 and self.polls > self.expire_after:
            return QrStatus(raw="expired")
        if self.polls <= self.expire_after:
            return QrStatus(raw="wait")
        self.logins += 1
        return QrStatus(
            raw="confirmed",
            credentials=Credentials(
                bot_token="tok-new", bot_id="b", user_id="u", base_url="https://ilink"
            ),
        )


async def test_an_expired_qrcode_makes_the_adapter_ask_for_a_new_one(tmp_path):
    """★ 码过期就**重新申请一个**,不许对着死码轮询到天亮。

    `relogin()` 原来只请求一次二维码,然后 `while True` 死等那一个;而
    `poll_qrcode_status` 把 expired 和"还没扫"都返回 None,调用方**没法区分**。
    实测症状:轮询 31 次,始终只用第 1 个码。
    """
    ilink = ExpiringILink(expire_after=2)
    a = adapter(tmp_path, ilink, lambda _r: httpx.Response(200, json={"items": []}))

    await a.relogin()

    assert ilink.qr_issued > 1, f"始终只用了第 {ilink.qr_issued} 个码——对着死码等到天亮"
    assert a.ilink_token == "tok-new"


async def test_a_new_qrcode_is_announced_again_after_expiry(tmp_path):
    """重新申请的码也要**再发给用户一次**。只在日志里换一张,用户那边还是那张死码。"""
    ilink = ExpiringILink(expire_after=1)
    a = adapter(
        tmp_path,
        ilink,
        lambda _r: httpx.Response(200, json={"items": []}),
        context_token="ctx-1",
        peer="u1@im.wechat",
    )

    await a.relogin()

    announced = [text for _to, text, _ctx in ilink.sent if "liteapp" in text]
    assert len(announced) >= 2, f"新码没重新发给用户:{announced}"
    assert "/q/2" in announced[-1], "发出去的还是旧那张码"


async def test_polling_does_not_spin_hot(tmp_path):
    """轮询要有下界。服务端对一个死码多半**立刻返回**,没有下界就是 2 核机器上的
    一个满转核心——把 Lararium 和同机的别的东西一起拖慢。

    生产里长轮询挂 35 秒所以看不出来;这条就是照着"服务端立刻返回"的情形写的。
    """

    class NeverConfirms(FakeILink):
        def __init__(self):
            super().__init__()
            self.polls = 0

        async def request_qrcode(self):
            return "qr", "https://liteapp/q/1"

        async def poll_qrcode_status(self, qrcode):
            await asyncio.sleep(0)
            self.polls += 1
            return QrStatus(raw="wait")

    ilink = NeverConfirms()
    a = adapter(tmp_path, ilink, lambda _r: httpx.Response(200, json={"items": []}))

    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(a.relogin(), timeout=0.3)

    assert ilink.polls < 10, f"0.3 秒里轮询了 {ilink.polls} 次,这是在热转"


def test_state_is_flushed_to_disk_before_the_rename(tmp_path, monkeypatch):
    """原子写要和 `ledger.py` 那份(R3-1)一致:同目录 .tmp → **fsync** → os.replace。

    这里后果轻(最坏重扫一次码),但**同一个仓库里两套原子写标准,以后的人照哪份写?**
    """
    fsynced: list[int] = []
    monkeypatch.setattr(os, "fsync", lambda fd: fsynced.append(fd))

    State(path=tmp_path / "wechat" / "state.json", cursor="c1").save()

    assert fsynced, "没 fsync:rename 是原子的,但内容可能还在页缓存里"


async def test_a_qrcode_stuck_in_an_unknown_state_is_eventually_replaced(tmp_path, monkeypatch):
    """★ **按构造兜底**:状态既不是 confirmed 也不是 dead,等够时限也要换新码。

    官方那串状态里还有要换轮询主机的(`scaned_but_redirect`)、要验证码的
    (`need_verifycode`)——枚举不全。而"这张码没戏了"的形态**只要有一种没被枚举到**,
    就又回到了"对着死码等到天亮"。限时换码对所有形态都成立,不用把状态认全。

    这和前缀指纹那次是同一条:**别枚举,按构造**。
    """
    monkeypatch.setattr(wechat, "_QR_LIFETIME", 0.05)
    monkeypatch.setattr(wechat, "_POLL_FLOOR", 0.01)

    class Stuck(FakeILink):
        def __init__(self):
            super().__init__()
            self.qr_issued = 0

        async def request_qrcode(self):
            self.qr_issued += 1
            return f"qr-{self.qr_issued}", f"https://liteapp/q/{self.qr_issued}"

        async def poll_qrcode_status(self, qrcode):
            await asyncio.sleep(0)
            if self.qr_issued >= 3:
                return QrStatus(
                    raw="confirmed",
                    credentials=Credentials("tok-new", "b", "u", "https://ilink"),
                )
            return QrStatus(raw="scaned_but_redirect")  # 我们没处理的状态

    ilink = Stuck()
    a = adapter(tmp_path, ilink, lambda _r: httpx.Response(200, json={"items": []}))

    await a.relogin()

    assert ilink.qr_issued == 3, "卡在未知状态时没有换码"
    assert a.ilink_token == "tok-new"


# ── M5-4:审批走同一套分派 ───────────────────────────────────────────────
#
# **任务书写的是「IM 按钮回调」,但这条通道上没有按钮。** 官方 types.ts 的
# MessageItemType 只有 NONE/TEXT/IMAGE/VOICE/FILE/VIDEO/TOOL_CALL_*,全库 grep 不到
# button/card/inline_keyboard/callback_data;官方自己处理斜杠命令也是
# `trimmed.startsWith("/")`。所以正确形态是:**以 / 开头的消息走 /v1/commands**,
# 别的走 /v1/messages。分派仍然只有一套(服务端的 handle_command),没写第二份。


def routed(tmp_path, text, *, response=None, status=200):
    """把一条消息投进适配器,返回它发出去的所有 HTTP 请求。"""
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, json=response if response is not None else {"text": "ok"})

    ilink = FakeILink(batches=[([InboundMessage(1, "u1@im.wechat", text, "ctx-1")], "c2")])
    a = adapter(tmp_path, ilink, handler, context_token="ctx-1", peer="u1@im.wechat")
    return a, ilink, seen


async def test_a_slash_command_goes_to_the_commands_endpoint(tmp_path):
    """`/pending` 走 `/v1/commands`,不是 `/v1/messages`——不进模型,是代码路径。

    审批必须走代码路径,这是门控的全部意义:**模型手上没有批准工具,这是故意的**
    (memory 的 SKILL.md 写着)。要是把 `/approve` 当普通消息喂给模型,
    等于把批准权交回给它。
    """
    a, _ilink, seen = routed(tmp_path, "/pending", response={"text": "有 1 条待审"})

    await a.pump_inbound_once()

    assert [r.url.path for r in seen] == ["/v1/commands"]
    assert json.loads(seen[0].content) == {"line": "/pending"}
    assert seen[0].headers["authorization"] == "Bearer tok-wechat"


async def test_the_command_result_is_sent_straight_back(tmp_path):
    """命令结果**直接回**给用户,不经出件箱——命令端点是同步返回的,没有信封。"""
    a, ilink, _seen = routed(tmp_path, "/pending", response={"text": "有 1 条待审:abc123"})

    await a.pump_inbound_once()

    assert ilink.sent == [("u1@im.wechat", "有 1 条待审:abc123", "ctx-1")]


async def test_ordinary_messages_still_go_to_the_model(tmp_path):
    """不以 / 开头的还是走 `/v1/messages`。"""
    a, _ilink, seen = routed(tmp_path, "打车 28", response={"envelope_id": "e1"})

    await a.pump_inbound_once()

    assert [r.url.path for r in seen] == ["/v1/messages"]


async def test_leading_whitespace_still_counts_as_a_command(tmp_path):
    """手机输入法很容易带前导空格。判定要在 **strip 之后**做。

    R2-1 那条教训的另一半:CLI 有 `input().strip()` 兜着,这里没有——
    ` /approve abc` 被当成普通消息喂给模型,用户会以为"批准了",而账本纹丝不动。
    """
    a, _ilink, seen = routed(tmp_path, "  /approve abc123  ", response={"text": "已批准"})

    await a.pump_inbound_once()

    assert [r.url.path for r in seen] == ["/v1/commands"]
    assert json.loads(seen[0].content) == {"line": "/approve abc123"}


async def test_a_failing_command_answers_in_plain_words(tmp_path):
    """命令端点报错时回一句人话,**不打崩收信泵**。

    打崩的后果是:用户打错一个命令,助手从此不再收消息——而他不会知道为什么。
    """
    a, ilink, _seen = routed(tmp_path, "/rollback 不存在", status=500)

    await a.pump_inbound_once()

    assert len(ilink.sent) == 1
    assert "没执行成功" in ilink.sent[0][1]


async def test_quit_does_not_kill_the_adapter(tmp_path):
    """`/quit` 不许让适配器退出。

    它在 CLI 里是"关掉我这个窗口",在微信里没有窗口可关——真退了就是助手下线,
    而用户只是手滑。服务端的 `/quit` 本来就是零副作用的(M2-5)。
    """
    a, ilink, _seen = routed(tmp_path, "/quit", response={"text": "已退出客户端。"})

    await a.pump_inbound_once()  # 不抛、不退出就算过

    assert len(ilink.sent) == 1


def test_the_adapter_knows_no_command_verbs():
    """★ **不许写第二份分派。**

    适配器只判"是不是以 / 开头",**具体动词一个都不许认**——认了就是第二份分派,
    而两份实现必然漂移,而这条路上放的是**账本的批准权**。

    动词表从 `commands.py` 动态取,不抄死:新增命令时这条自动跟上。
    """
    import re

    verbs = set(
        re.findall(
            r'verb == "(/[a-z]+)"',
            Path("src/lararium/gateway/commands.py").read_text(encoding="utf-8"),
        )
    )
    assert len(verbs) >= 8, f"动词表没取到,这条测试是空转的:{verbs}"

    source = Path("src/lararium/gateway/wechat.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    leaked = sorted(v for v in verbs if f'"{v}"' in code or f"'{v}'" in code)
    assert not leaked, f"适配器自己认起了命令动词 {leaked}——那就是第二份分派"


# ── M5-4 媒体入站与"不堵通道" ───────────────────────────────────────────


class FakeCdn:
    """按 MediaRef 回字节;可以指定某一条下载失败。"""

    def __init__(self, blobs=None, fail=False):
        self._blobs = blobs or {}
        self.fail = fail
        self.asked: list[str] = []

    async def download_media(self, ref):
        self.asked.append(ref.encrypted_query_param)
        if self.fail:
            raise ILinkError("CDN 502", code=502)
        return self._blobs.get(ref.encrypted_query_param, b"\xff\xd8\xff\xe0jpeg-bytes")


class FakeILinkWithCdn(FakeILink, FakeCdn):
    def __init__(self, batches=(), blobs=None, fail_download=False):
        FakeILink.__init__(self, batches=batches)
        FakeCdn.__init__(self, blobs=blobs, fail=fail_download)


def image(query="q1"):
    return MediaRef(kind="image", encrypted_query_param=query, full_url="", aes_key_b64="")


async def test_a_text_message_behind_an_image_still_arrives(tmp_path):
    """★ 本步真正的理由:**一条纯图片消息不许把它后面的所有文字消息一起堵死。**

    失效形态不是"图片不支持",是**助手整个哑掉**——那条消息卡在队首,用户在微信这头
    只看到"发什么都没反应",而日志里是同一条消息每三秒重来一遍。
    """
    delivered: list[str] = []

    def handler(request):
        body = json.loads(request.content)
        delivered.append(body["content"])
        return httpx.Response(202, json={"envelope_id": "e1"})

    ilink = FakeILinkWithCdn(
        batches=[
            (
                [
                    InboundMessage(1, "u1@im.wechat", "", "ctx", media=(image(),)),
                    InboundMessage(2, "u1@im.wechat", "刚发的那张", "ctx"),
                ],
                "cursor-2",
            )
        ]
    )
    a = adapter(tmp_path, ilink, handler)

    await a.pump_inbound_once()

    assert "刚发的那张" in delivered, "图片后面那条文字消息没送达——通道被堵住了"
    assert State.load(a.state.path).cursor == "cursor-2", "游标没推进,下一轮会再收同一批"


async def test_one_poisoned_message_does_not_stop_the_batch(tmp_path):
    """**任何**一条投递失败都要跳过并推进游标,不只是图片。

    今天是图片,明天是超 16KB 的长文(服务端 413)、或者服务端抖一下。一条毒消息
    能永久堵死通道的话,恢复手段只剩人工进库删行——而没人会知道要去删。
    """
    delivered: list[str] = []

    def handler(request):
        body = json.loads(request.content)
        if body["content"].startswith("毒"):
            return httpx.Response(413, json={"error": "content 超出 16KB 上限"})
        delivered.append(body["content"])
        return httpx.Response(202, json={"envelope_id": "e1"})

    ilink = FakeILinkWithCdn(
        batches=[
            (
                [
                    InboundMessage(1, "u1@im.wechat", "毒" * 3, "ctx"),
                    InboundMessage(2, "u1@im.wechat", "后面这条", "ctx"),
                ],
                "cursor-2",
            )
        ]
    )
    a = adapter(tmp_path, ilink, handler)

    await a.pump_inbound_once()

    assert delivered == ["后面这条"]
    assert State.load(a.state.path).cursor == "cursor-2", "毒消息把游标钉住了"


async def test_an_image_is_stored_under_its_content_hash_exactly_once(tmp_path):
    """按内容哈希命名:同一张图发两次只存一份,而且文件名不由对方决定。"""
    ilink = FakeILinkWithCdn(
        batches=[
            ([InboundMessage(1, "u1@im.wechat", "", "ctx", media=(image("q1"),))], "c1"),
            ([InboundMessage(2, "u1@im.wechat", "", "ctx", media=(image("q2"),))], "c2"),
        ]
    )
    a = adapter(tmp_path, ilink, lambda _r: httpx.Response(202, json={"envelope_id": "e"}))

    await a.pump_inbound_once()
    await a.pump_inbound_once()

    files = sorted(p.name for p in (tmp_path / "media").iterdir())
    digest = hashlib.sha256(b"\xff\xd8\xff\xe0jpeg-bytes").hexdigest()
    assert files == [f"{digest}.jpg"]


async def test_the_envelope_gets_a_reference_and_a_readable_line_not_bytes(tmp_path):
    """信封带的是**引用**,`content` 是一行人话——下游一切按文本走的东西都不用动。"""
    posted: list[dict] = []

    def handler(request):
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={"envelope_id": "e1"})

    ilink = FakeILinkWithCdn(
        batches=[([InboundMessage(1, "u1@im.wechat", "这是啥", "ctx", media=(image(),))], "c1")]
    )
    a = adapter(tmp_path, ilink, handler)

    await a.pump_inbound_once()

    digest = hashlib.sha256(b"\xff\xd8\xff\xe0jpeg-bytes").hexdigest()
    assert posted[0]["content"] == f"这是啥\n(图片 · media/{digest[:12]}…)"
    assert posted[0]["attachments"] == [
        {"kind": "image", "sha256": digest, "media_type": "image/jpeg"}
    ]
    assert "jpeg-bytes" not in json.dumps(posted[0]), "字节被塞进信封了"


async def test_a_failed_download_is_plain_words_and_the_message_still_lands(tmp_path):
    """CDN 挂了 / 解密失败 → 走 E2 人话,消息照样投,泵不许崩。

    图取不到是常事(CDN 抖动、密钥编码没见过);为此丢掉整条消息的话,用户发的那句
    "这个多少钱"也跟着没了。
    """
    posted: list[dict] = []

    def handler(request):
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={"envelope_id": "e1"})

    ilink = FakeILinkWithCdn(
        batches=[
            ([InboundMessage(1, "u1@im.wechat", "这个多少钱", "ctx", media=(image(),))], "c1")
        ],
        fail_download=True,
    )
    a = adapter(tmp_path, ilink, handler)

    await a.pump_inbound_once()

    assert posted[0]["content"].startswith("这个多少钱")
    assert "没能取到" in posted[0]["content"]
    assert posted[0]["attachments"] == []
    assert State.load(a.state.path).cursor == "c1"
    # 成没成功**只有适配器知道**:模型看到的那行文本里没有"下载失败"这个信息,
    # 所以回执必须分两种说法,不能一律"已存下来"。
    assert "没取到" in ilink.sent[0][1], f"下载失败却回了句报喜的:{ilink.sent}"


async def test_the_user_is_told_the_image_arrived(tmp_path):
    """收到图要回一句人话。读图是 M5-5,这一步模型对着一行文本答不出内容
    ——不吭声的话用户只会以为又没反应。"""
    ilink = FakeILinkWithCdn(
        batches=[([InboundMessage(1, "u1@im.wechat", "", "ctx", media=(image(),))], "c1")]
    )
    a = adapter(tmp_path, ilink, lambda _r: httpx.Response(202, json={"envelope_id": "e"}))

    await a.pump_inbound_once()

    assert ilink.sent, "收到图之后什么都没回"
    assert "收到" in ilink.sent[0][1]


async def test_media_is_written_atomically(tmp_path, monkeypatch):
    """和 `State.save` / `ledger.py` 同一份 R3-1 标准:同目录 .tmp → fsync → replace。

    半张图留在 `media/` 下、名字却是完整哈希的话,以后**每一次**都会拿它当那张图,
    而它永远不会自己修好。

    **锚点是那对系统调用,不是"目录里没有 .tmp"**:直接 `open(path,"wb")` 写下去也
    不会留 .tmp,那样断言等于什么都没测(T6 第五种假绿)。原子性从外面看不出差别,
    所以只能钉实现——这是 T1 的例外,写在这里免得下一个人以为是疏忽。
    """
    fsynced: list[int] = []
    replaced: list[str] = []
    real_fsync, real_replace = os.fsync, Path.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (fsynced.append(fd), real_fsync(fd))[1])
    monkeypatch.setattr(
        Path,
        "replace",
        lambda self, target: (replaced.append(self.name), real_replace(self, target))[1],
    )

    ilink = FakeILinkWithCdn(
        batches=[([InboundMessage(1, "u1@im.wechat", "", "ctx", media=(image(),))], "c1")]
    )
    a = adapter(tmp_path, ilink, lambda _r: httpx.Response(202, json={"envelope_id": "e"}))

    await a.pump_inbound_once()

    assert not [p for p in (tmp_path / "media").iterdir() if p.suffix == ".tmp"]
    assert fsynced, "附件没 fsync:rename 是原子的,内容却可能还在页缓存里"
    assert [n for n in replaced if n.endswith(".jpg.tmp")], "附件不是经临时文件改名落位的"
