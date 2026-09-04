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

## 重试节奏:退避,而且要能被叫醒(M5-12)

原来是「任何异常 → `sleep(3)` → 重来」,无限。而微信窗口是 24 小时:哪天用户没开口,
晨报发不出去,适配器就对着腾讯的接口打一整夜——**一夜约 1.4 万次**,真会撞限流,
而限流很可能回 `-14`,那个码正好被过载(缺 HTTP 头也回 -14),到时候极难查。

两种失效形态,别混:

- **发失败**(`send_text` 抛错)→ 指数退避,封顶几分钟(`Backoff`);
- **还没人跟它说过话**(`peer`/`context_token` 为空)→ 连出件箱都不去拉:
  拉回来也发不出去,而每 5 秒一次长轮询 + 一条 warning,一夜是上万次白转和几千行日志。

**窗口重开的信号是入站消息**(它刷新 `context_token`),所以退避和等待都必须能被
入站泵叫醒(`_inbound_woke`)。**只退避不叫醒等于把延迟写死**:用户开口之后还要再等
几分钟才收到攒着的推送,那个体验比刷接口更糟。

失败的语义没变:条目留在出件箱、游标不推进(M4-7 定的「消息在等你」)。

## 一条投递失败不许堵住它后面的所有人(M5-4)

原来一批消息是"逐条投,最后统一推游标"。任何一条投递失败(超 16KB 的长文 → 413、
服务端抖一下、以后的图片 → 空 content 400)都会让异常穿出去,**游标一格都不推**,
三秒后拿旧游标又取到同一批 —— 那条毒消息卡在队首,它后面所有消息一起进不来。
用户在微信这头看到的是"发什么都没反应",日志里是同一条消息每三秒重来一遍。

所以现在是**逐条兜住、照常推进游标**:投不进去的那条落一行日志然后跳过。丢一条消息
比哑掉整个助手轻得多,而且丢的那条至少在日志里有名有姓。
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import httpx

from lararium.envelope import Attachment, kind_word
from lararium.gateway.ilink import Credentials, ILinkClient, ILinkError, InboundMessage, MediaRef

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
# 退避的下界与封顶。下界沿用原来那个 3 秒(小抖动不该多等);封顶五分钟——
# 没有封顶的话,连着失败一整夜之后下一次重试要等到第二天。
BACKOFF_FLOOR = 3.0
BACKOFF_CEILING = 300.0

# 落盘时按字节的**魔数**认类型,不信对方给的文件名——文件名是外部输入,而这里的
# 结果会变成磁盘上的后缀,而后缀会一路决定 `Attachment.is_image`。
#
# **两个方向都要诚实,而两个方向都出过事:**
#
# - 过度声称:第一版 WebP 只认 `RIFF`。RIFF 不是 WebP,是**容器族**——WAV、AVI 全以它
#   开头。一段 WAV 会顶着 `image/webp` 落盘成 `.webp`、`is_image` 为真、被当图片送进
#   模型。"非图片不许当图片送"那个洞就从这扇门原样走回来了:守卫改对了没用,
#   它信的类型是这个函数算出来的。所以 WebP 要连偏移 8 起的 `WEBP` 一起认。
# - 声称不足:漏掉 BMP(服务商自己那句报错里就写着支持)和 HEIC(iPhone 原图默认格式)
#   的话,它们会掉进 octet-stream,然后**一声不响地**不进模型。
#
# 每一项是「若干个 (偏移, 字节) 全都对上」→ media_type。`ftyp` 在偏移 4 处对 MP4 和
# HEIC 都成立,分野在偏移 8 的 brand——所以 brand 必须一起判,一个格式一行,不玩花的。
_MAGIC: tuple[tuple[tuple[tuple[int, bytes], ...], str], ...] = (
    (((0, b"\xff\xd8\xff"),), "image/jpeg"),
    (((0, b"\x89PNG\r\n\x1a\n"),), "image/png"),
    (((0, b"GIF8"),), "image/gif"),
    (((0, b"RIFF"), (8, b"WEBP")), "image/webp"),
    (((0, b"BM"),), "image/bmp"),
    (((4, b"ftyp"), (8, b"heic")), "image/heic"),
    (((4, b"ftyp"), (8, b"heix")), "image/heic"),
    (((4, b"ftyp"), (8, b"mif1")), "image/heif"),
    (((4, b"ftyp"), (8, b"msf1")), "image/heif"),
)
# 魔数认不出来时按种类兜底。语音是 SILK(官方要转码成 wav,那不是这一步的事)。
_FALLBACK_MEDIA_TYPES: dict[str, str] = {
    "voice": "audio/silk",
    "video": "video/mp4",
}
_DEFAULT_MEDIA_TYPE = "application/octet-stream"


@dataclass
class Backoff:
    """指数退避的计数器。**只算时长,不负责睡**——睡在 `_sleep_or_wake` 里,
    因为那一步要能被入站消息叫醒,而"等多久"和"怎么等"是两件事。"""

    delay: float = 0.0

    def next(self) -> float:
        self.delay = BACKOFF_FLOOR if not self.delay else min(self.delay * 2, BACKOFF_CEILING)
        return self.delay

    def reset(self) -> None:
        """成功一次就归零——不归零的话,一次长故障之后的第一个小抖动要从几分钟起步。"""
        self.delay = 0.0


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
        media_dir: Path,
    ) -> None:
        self.ilink = ilink
        self.lararium = lararium
        self.token = token
        self.state = state
        self.media_dir = media_dir
        # 两个泵是独立 task,靠这个 Event 通气:入站消息一到就把等着的发信泵叫醒。
        # 窗口重开的信号只有它(入站消息会刷新 context_token)。
        self._inbound_woke = asyncio.Event()
        self._outbox_backoff = Backoff()
        self._inbound_backoff = Backoff()

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
            try:
                await self._deliver(message)
            except Exception as exc:
                # **故意兜住所有异常并继续**(E1 的例外,理由写在模块 docstring):
                # 不兜的话一条投不进去的消息会把它后面的全部堵死,而恢复手段只剩
                # 人工进库删行。这里丢的那条在日志里有名有姓;哑掉的助手没有。
                logger.warning(
                    "消息 %s 没能投进 Lararium,跳过:%s: %s",
                    message.message_id,
                    type(exc).__name__,
                    exc,
                )
        if cursor != self.state.cursor:
            self.state.cursor = cursor
            self.state.save()

    def _remember(self, message: InboundMessage) -> None:
        """每收到一条就刷新回信凭据并落盘——它同时是主动推送要用的那一份。

        **顺带把发信泵叫醒**:24 小时窗口重开的唯一信号就是这条消息,而发信泵可能正
        退避着、或者正等着"第一个说话的人"。不叫醒的话,攒着的推送要等到退避自己走完。
        """
        self.state.context_token = message.context_token or self.state.context_token
        self.state.peer = message.from_user_id or self.state.peer
        self.state.save()
        self._inbound_woke.set()

    async def _deliver(self, message: InboundMessage) -> None:
        """把一条微信消息变成一个信封投进去;有附件就先取下来存好。

        **附件先落盘,信封里放引用**——把字节塞进信封等于把它塞进每一次序列化和
        每一行日志,而 `content` 仍是字符串这条纪律不许绕(它是所有外部输入的入口)。
        """
        if not message.media:
            await self._route(message.text)
            return
        attachments, lines = [], []
        for ref in message.media:
            saved = await self._save_media(ref)
            if saved is None:
                lines.append(f"({kind_word(ref.kind)} · 没能取到)")
                continue
            attachments.append(saved)
            lines.append(saved.as_line())
        content = "\n".join(part for part in [message.text, *lines] if part)
        await self._post_message(content, attachments)
        await self._say(self._media_receipt(attachments, len(message.media)))

    def _media_receipt(self, saved: list[Attachment], total: int) -> str:
        """收到附件当场回一句人话。

        读图是 M5-5,这一步模型对着一行文本答不出图里有什么——不吭声的话用户只会
        以为"又没反应"(这正是本步要治的那个症状)。而**成没成功只有这里知道**:
        模型看到的那行文本里没有"下载失败"这个信息。
        """
        if len(saved) == total:
            return f"收到 {total} 个附件,已存下来。"
        return f"收到 {total} 个附件,其中 {total - len(saved)} 个没取到内容。"

    async def _save_media(self, ref: MediaRef) -> Attachment | None:
        """下载、解密、按内容哈希落盘。取不到就返回 None(E2:人话,不是异常)。

        图取不到是常事(CDN 抖动、密钥编码没见过)。为此丢掉整条消息的话,用户同时
        发的那句"这个多少钱"也跟着没了。
        """
        try:
            data = await self.ilink.download_media(ref)
        except (ILinkError, httpx.HTTPError, ValueError) as exc:
            logger.warning("附件没取到(%s):%s: %s", ref.kind, type(exc).__name__, exc)
            return None
        attachment = Attachment(
            kind=ref.kind,
            sha256=hashlib.sha256(data).hexdigest(),
            media_type=_sniff(data, ref.kind),
        )
        self._write_once(attachment, data)
        return attachment

    def _write_once(self, attachment: Attachment, data: bytes) -> None:
        """按内容哈希命名:同一张图发两次只存一份,已经在了就不重写。

        原子写和 `State.save` / `ledger.py` 是同一份 R3-1 标准——半张图留在 `media/` 下、
        名字却是完整哈希的话,以后**每一次**都会拿它当那张图,而它永远不会自己修好。
        """
        path = self.media_dir / Path(attachment.path).name
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        with temp.open("wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        temp.replace(path)

    async def _post_message(self, content: str, attachments: list[Attachment]) -> None:
        response = await self.lararium.post(
            "/v1/messages",
            json={
                "content": content,
                "attachments": [a.model_dump() for a in attachments],
            },
            headers={"Authorization": f"Bearer {self.token}"},
        )
        response.raise_for_status()

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
        await self._post_message(text, [])

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

        M5-12:那种情况下**连出件箱都不去拉**(`_await_peer` 挂在那儿等入站消息),
        而不是每 5 秒空转一次长轮询再放弃。
        """
        await self._await_peer()
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

    async def _await_peer(self) -> None:
        """还没人跟它说过话就挂在这儿等,别去拉出件箱。

        日志只在**真的开始等**的时候打一次:原来那条 warning 是每 5 秒一条,一夜几千行,
        而它说的是同一件事。
        """
        if self.state.peer and self.state.context_token:
            return
        logger.info("还没有人跟它说过话,推送先攒着,等第一条入站消息把窗口打开")
        while not (self.state.peer and self.state.context_token):
            self._inbound_woke.clear()
            await self._inbound_woke.wait()

    async def _sleep_or_wake(self, seconds: float) -> None:
        """等这么久,**但入站消息一到就立刻醒**。

        窗口重开的信号只有入站消息;只退避不叫醒等于把延迟写死,用户开口之后还要
        再等几分钟才收到攒着的推送。
        """
        self._inbound_woke.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._inbound_woke.wait(), timeout=seconds)

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

    async def _pump_step(
        self, once: Callable[[], Awaitable[None]], label: str, backoff: Backoff
    ) -> None:
        """跑一次泵;失败就按退避等,成功就把退避归零。

        提成一个方法是为了能测"等了多久"——`run()` 是无限循环,从外面没法看。
        """
        try:
            await once()
        except Exception as exc:
            logger.warning(
                "%s 出错(%.0f 秒后重试):%s: %s",
                label,
                backoff.next(),
                type(exc).__name__,
                exc,
            )
            await self._sleep_or_wake(backoff.delay)
        else:
            backoff.reset()

    async def run(self) -> None:
        """两个泵各跑各的。一个挂了不该拖死另一个——收不到消息至少还能把推送发出去,
        反过来也一样。

        **两个泵各有各的退避计数器**:一边的故障不该让另一边跟着变慢,而它们的失效
        原因通常也不是同一个。
        """

        async def pump(once: Callable[[], Awaitable[None]], label: str, backoff: Backoff) -> None:
            while True:
                await self._pump_step(once, label, backoff)

        await asyncio.gather(
            pump(self.pump_inbound_once, "收信", self._inbound_backoff),
            pump(self.pump_outbox_once, "发信", self._outbox_backoff),
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
        media_dir=data_dir / "media",
    )
    if not state.bot_token:
        await adapter.relogin()
    await adapter.run()


if __name__ == "__main__":
    asyncio.run(main())


def _sniff(data: bytes, kind: str) -> str:
    """按魔数认类型。**不信对方给的文件名**——它是外部输入,而结果会变成磁盘上的后缀。"""
    for signature, media_type in _MAGIC:
        if all(data[at : at + len(magic)] == magic for at, magic in signature):
            return media_type
    return _FALLBACK_MEDIA_TYPES.get(kind, _DEFAULT_MEDIA_TYPE)
