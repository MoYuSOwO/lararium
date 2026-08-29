"""微信适配器:上面接 iLink,下面调 Lararium 的 HTTP 接口。

**和 `cli.py` 一个位置,只是换了个说话的对象**——纯客户端,只 import httpx 与协议那一层,
不许 import steward/bundles(`.importlinter` 有契约钉着)。

**独立进程,别塞进服务进程里。** 两条理由:iLink 会掉线要重连,而重启一次不该把 542 MB
的 embedding 跟着重载一遍;微信那边抽风也不该让 Steward 跟着死。

## 两个泵,不是一问一答

`cli.py` 是"发一条 → 长轮询等这条的回复"。这里不行:M4-7 的主动推送(早报、待审提醒)
不对应任何一条用户消息,一问一答的形状接不住它。所以拆成两个独立的泵:

    收:iLink getupdates ──→ POST /v1/messages(以 / 开头的走 /v1/commands)
    发:GET /v1/outbox   ──→ iLink sendmessage

发的那个泵不关心消息从哪来,回复也好推送也好一律照发——M4-7 把推送做成了一轮完整的
对话,出件箱里它和普通回复长得一样,这里就不用分两条路。

## 三样状态必须落盘

`get_updates_buf`(收信游标)不存会重收或漏收;`outbox_after` 不存会在重启后**重发**
——用户收到两遍同一句回复,比没收到还糟;`context_token` 是**主动推送的凭据**
(官方实现里它没有过期逻辑,收到新消息就覆盖),丢了它早报就发不出去。

单用户助手,所以只记**最后一个**说话的人(`peer`)。多用户是另一个设计,不在这里假装支持。
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import httpx

from lararium.gateway.ilink import Credentials, ILinkClient, ILinkError, InboundMessage

logger = logging.getLogger("lararium.wechat")

DEFAULT_SERVER_URL = "http://127.0.0.1:8420"
DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
# 出件箱长轮询的等待秒数。和 cli.py 一个量级:短到掉线能快点发现,长到不至于空转刷屏。
_OUTBOX_WAIT = 5
# 一张二维码最多等这么久,到点换新的。比微信那边的码有效期宽松些即可——它的作用是
# **兜住所有"这张码没戏了"却没被状态字段说明白的情形**,不是精确复刻服务端的过期时间。
_QR_LIFETIME = 180.0
# 轮询下界。服务端正常会把请求挂住(长轮询),但异常时会立刻返回——没有这一行,
# 那就是热循环。
_POLL_FLOOR = 1.0


@dataclass
class State:
    """落盘的适配器状态。字段少而关键,见模块 docstring。"""

    path: Path
    bot_token: str = ""
    base_url: str = DEFAULT_BASE_URL
    cursor: str = ""
    context_token: str = ""
    peer: str = ""
    outbox_after: int = 0

    # 会落盘的字段。ClassVar 而不是 dataclass 字段——它是这个类的规格,不是某个实例的数据。
    PERSISTED: ClassVar[tuple[str, ...]] = (
        "bot_token",
        "base_url",
        "cursor",
        "context_token",
        "peer",
        "outbox_after",
    )

    @classmethod
    def load(cls, path: Path) -> "State":
        """读不出来就从零开始,**不打崩启动**。

        最坏后果是重收一批消息(至少还能用);而崩在启动上的后果是助手整个不在了
        ——用户什么都收不到,也不知道为什么。
        """
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls(path=path)
        if not isinstance(data, dict):
            return cls(path=path)
        known = {k: v for k, v in data.items() if k in cls.PERSISTED}
        return cls(path=path, **known)

    def save(self) -> None:
        """先写临时文件再 rename:**半截的状态文件比没有更坏**——它能被 json 解析,
        内容却是残缺的,而下一次启动会拿它当真。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {name: getattr(self, name) for name in self.PERSISTED}
        # 同目录 .tmp → fsync → 原子替换。**和 ledger.py 的 R3-1 是同一份标准**:
        # 少了 fsync,rename 虽是原子的,内容却可能还在页缓存里——掉电后拿到的是
        # "改名成功但内容是空的"。这里后果轻(最坏重扫一次码),但同一个仓库里
        # 两套原子写标准,以后的人照哪份写?
        temp = self.path.with_name(self.path.name + ".tmp")
        with temp.open("w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        temp.replace(self.path)


class WeChatAdapter:
    """一个泵收、一个泵发。两边都做成"跑一次"的方法,好测也好在外面编排。"""

    def __init__(
        self,
        *,
        ilink: ILinkClient,
        lararium: httpx.AsyncClient,
        token: str,
        state: State,
    ) -> None:
        self.ilink = ilink
        self.lararium = lararium
        self.token = token
        self.state = state

    @property
    def ilink_token(self) -> str | None:
        return self.ilink.token

    # ── 收 ──────────────────────────────────────────────────────────────

    async def pump_inbound_once(self) -> None:
        """长轮询收一批,逐条投进 Lararium。空返回是常态,不当异常也不刷屏。"""
        try:
            messages, cursor = await self.ilink.get_updates(self.state.cursor)
        except ILinkError as exc:
            if not exc.stale_token:
                raise  # 不是 -14 的错照样往上抛——吞掉等于把 bug 埋进日志(E1)
            # -14:头由 ilink._headers() 一处构造、每次全带(报文级测试钉着),
            # 所以它只剩"token 真的失效"一种解释 → 重连。**不停机**:官方那个
            # 一小时暂停是照着"-14 = token 过期"写的,而这个错误码是过载的,
            # 一个头写错就白停一小时,还完全查不出原因。
            await self.relogin()
            return

        for message in messages:
            self._remember(message)
            await self._route(message.text)
        if cursor != self.state.cursor:
            self.state.cursor = cursor
            self.state.save()

    def _remember(self, message: InboundMessage) -> None:
        """每收到一条就刷新回信凭据并落盘——它同时是主动推送要用的那一份。"""
        self.state.context_token = message.context_token or self.state.context_token
        self.state.peer = message.from_user_id or self.state.peer
        self.state.save()

    async def _route(self, text: str) -> None:
        """以 `/` 开头的走命令端点,别的走消息端点。

        **任务书原话是「IM 按钮回调」,但这条通道上没有按钮**:官方 `types.ts` 的
        `MessageItemType` 只有 NONE/TEXT/IMAGE/VOICE/FILE/VIDEO/TOOL_CALL_*,
        全库 grep 不到 button/card/inline_keyboard;官方自己处理斜杠命令也是
        `trimmed.startsWith("/")`。所以审批在微信里就是**打一行 `/approve <id>`**。

        **先 strip 再判**(R2-1 的另一半):手机输入法很容易带前导空格,
        ` /approve abc` 要是被当成普通消息喂给模型,用户会以为"批准了",
        而账本纹丝不动——CLI 有 `input().strip()` 兜着,这里没有。
        """
        line = text.strip()
        if line.startswith("/"):
            await self._run_command(line)
            return
        response = await self.lararium.post(
            "/v1/messages",
            json={"content": text},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        response.raise_for_status()

    async def _run_command(self, line: str) -> None:
        """把命令原样转给 `/v1/commands`,把返回的文本回给用户。

        **只判"是不是以 / 开头",具体动词一个都不认**——认了就是第二份分派,
        而两份实现必然漂移,而这条路上放的是**账本的批准权**。分派只有一套:
        服务端的 `handle_command`(它当初就是为这个抽出来的)。有测试钉着
        这个文件里不出现任何命令动词。

        审批必须走这条代码路径,这是门控的全部意义:**模型手上没有批准工具,
        那是故意的**——把 `/approve` 当普通消息喂给模型,等于把批准权交回给它。
        """
        try:
            response = await self.lararium.post(
                "/v1/commands",
                json={"line": line},
                headers={"Authorization": f"Bearer {self.token}"},
            )
            response.raise_for_status()
            reply = str(response.json().get("text", ""))
        except (httpx.HTTPError, ValueError) as exc:
            # 打崩收信泵的后果是:用户打错一个命令,助手从此不再收消息,而他不知道为什么。
            logger.warning("命令 %s 失败:%s: %s", line, type(exc).__name__, exc)
            reply = f"这条命令没执行成功({type(exc).__name__})。再试一次,或者去日志里看看。"
        await self._say(reply)

    async def _say(self, text: str) -> None:
        """直接回一句给用户。命令端点是同步返回的,没有信封,不走出件箱。"""
        if not (self.state.peer and self.state.context_token and text):
            return
        await self.ilink.send_text(
            to_user_id=self.state.peer, text=text, context_token=self.state.context_token
        )

    # ── 发 ──────────────────────────────────────────────────────────────

    async def pump_outbox_once(self) -> None:
        """把出件箱里属于本渠道的条目发到微信,发一条推一条游标。

        **发不出去就不推游标**:没有 `peer`/`context_token` 时(还没人跟它说过话),
        条目留在出件箱里等用户开口——M4-7 说过失效形态该是"消息在等你",不是"发不出去就丢"。
        """
        response = await self.lararium.get(
            f"/v1/outbox?after={self.state.outbox_after}&wait={_OUTBOX_WAIT}",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        response.raise_for_status()
        for item in response.json().get("items", []):
            if not (self.state.peer and self.state.context_token):
                logger.warning("还没有可回信的会话,出件箱 seq=%s 留着等用户开口", item.get("seq"))
                return
            await self.ilink.send_text(
                to_user_id=self.state.peer,
                text=str(item.get("content", "")),
                context_token=self.state.context_token,
            )
            self.state.outbox_after = int(item.get("seq", self.state.outbox_after))
            self.state.save()

    # ── 重连 ────────────────────────────────────────────────────────────

    async def relogin(self) -> None:
        """重新扫码,**直到真的连上**。二维码会过期,所以这是个两层循环:
        外层不断换新码,内层等这一张。

        原来只请求一次码然后死等那一张。失效剧本:凌晨三点会话到期 → 码发到微信 →
        你在睡觉 → 几分钟后码过期 → 适配器对着死码轮询到天亮 → 你早上回消息毫无反应。
        **助手静默死掉,只能人工重启进程**——而这里正是恢复路径。

        换码有两个触发口,一快一慢,缺一不可:
        - 服务端明说这张废了(`QrStatus.dead`)→ 立刻换;
        - 等够 `_QR_LIFETIME` 还没结果 → 也换。后者是**按构造**的兜底:
          官方那串状态里还有要换轮询主机的(`*_redirect`)、要验证码的,枚举不全;
          限时换码对**所有**"这张码没戏了"的形态都成立,不用把状态认全。
        """
        while True:
            qrcode, image_url = await self.ilink.request_qrcode()
            await self._announce_qrcode(image_url)
            if await self._await_scan(qrcode):
                return

    async def _await_scan(self, qrcode: str) -> bool:
        """等这一张码。连上了返回 True;这张没戏了返回 False(让外层换一张)。"""
        deadline = time.monotonic() + _QR_LIFETIME
        while time.monotonic() < deadline:
            status = await self.ilink.poll_qrcode_status(qrcode)
            if status.confirmed and status.credentials is not None:
                self._adopt(status.credentials)
                return True
            if status.dead:
                logger.warning("二维码已失效(%s),换一张", status.raw)
                return False
            # **下界**:生产里长轮询挂 35 秒所以看不出来,但服务端对一张死码多半立刻
            # 返回——没有下界这就是 2 核机器上的一个满转核心,把 Lararium 和同机
            # 别的东西一起拖慢。
            await asyncio.sleep(_POLL_FLOOR)
        logger.warning("二维码等了 %.0f 秒没结果,换一张", _QR_LIFETIME)
        return False

    async def _announce_qrcode(self, image_url: str) -> None:
        """尽力而为:旧凭据要是已经彻底失效,这条发不出去——那就只能落日志。
        **发不出去不许打断重连本身**,否则一次投递失败就把唯一的恢复路径也堵死了。"""
        # 措辞要中立:这条在**首次登录**和**会话到期重连**两种情形下都会打。
        # 冒烟时它把首次登录记成了"会话失效",第一次用的人会以为出了故障。
        logger.warning("需要扫码连上微信:%s", image_url)
        if not (self.state.peer and self.state.context_token):
            return
        try:
            await self.ilink.send_text(
                to_user_id=self.state.peer,
                text=f"微信会话到期了,扫这个重新连上:{image_url}",
                context_token=self.state.context_token,
            )
        except (ILinkError, httpx.HTTPError) as exc:
            logger.warning("新二维码没发出去(%s),请到日志里取链接", exc)

    def _adopt(self, credentials: Credentials) -> None:
        self.ilink.token = credentials.bot_token
        self.ilink.base_url = credentials.base_url.rstrip("/")
        self.state.bot_token = credentials.bot_token
        self.state.base_url = self.ilink.base_url
        self.state.save()
        logger.info("iLink 重连完成,bot_id=%s", credentials.bot_id)

    # ── 编排 ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """两个泵各跑各的。一个挂了不该拖死另一个——收不到消息至少还能把推送发出去,
        反过来也一样。"""

        async def pump(once, label: str) -> None:
            while True:
                try:
                    await once()
                except Exception as exc:
                    logger.warning("%s 出错(继续跑):%s: %s", label, type(exc).__name__, exc)
                    await asyncio.sleep(3)

        await asyncio.gather(
            pump(self.pump_inbound_once, "收信"), pump(self.pump_outbox_once, "发信")
        )


async def main() -> None:
    """入口。凭据没存过就先扫码;之后一直跑两个泵。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    data_dir = Path(os.environ.get("LARARIUM_DATA_DIR", "./data"))
    token = os.environ.get("LARARIUM_CLIENT_TOKEN", "")
    if not token:
        raise SystemExit("LARARIUM_CLIENT_TOKEN 未设置(要一个渠道为 wechat 的控制端 token)")

    state = State.load(data_dir / "wechat" / "state.json")
    adapter = WeChatAdapter(
        ilink=ILinkClient(base_url=state.base_url, token=state.bot_token or None),
        lararium=httpx.AsyncClient(
            base_url=os.environ.get("LARARIUM_SERVER_URL", DEFAULT_SERVER_URL).rstrip("/"),
            timeout=httpx.Timeout(70.0, connect=5.0),
        ),
        token=token,
        state=state,
    )
    if not state.bot_token:
        await adapter.relogin()
    await adapter.run()


if __name__ == "__main__":
    asyncio.run(main())
