"""iLink 协议客户端(微信 ClawBot)——协议细节只出现在这里。

规格来自两处:PLAN.md 的「iLink 协议实测记录」(真机跑通的收发闭环)+ 官方 MIT 实现
`@tencent-weixin/openclaw-weixin` v2.4.6 的 TypeScript 源码。**照着它用 Python 重新
实现**,那三个无许可证的社区仓库(= 保留所有权利)一行都没抄——本项目是 MIT。

这一层只管"把字节发对",不认识 Lararium 的任何概念(`wechat.py` 才接线)。
和 `cli.py` 同级:纯客户端,不许 import steward/bundles(`.importlinter` 有契约钉着)。

## 六个头,一个都不能少

少一个,服务端回的是 `{"errcode":-14,"errmsg":"session timeout"}` ——**这个错误信息是
骗人的**:真实原因是缺头,不是 token 过期。所以头在 `_headers()` **一处构造、每次请求
全带**,不给"某个调用点漏一个"留缝。

## `-14` 是过载的错误码,别照抄官方的处置

官方 `session-guard.ts` 把 -14 当 token 过期,处置是**把该账号所有 API 暂停一小时**。
但实测缺头时返回的也是 -14,补上头同一个 token 立刻好使。照抄的话,一个头写错就白停机
一小时,而且完全查不出原因——**日志上看是"token 过期",真相是你少发了一个头**。

我们的处置:因为头由一处构造、每次全带(有报文级测试钉着),-14 只剩"token 真的失效"
一种解释 → 报 `stale_token=True`,让上层去重连(重新扫码)。**这里没有任何停机状态。**

## 媒体走的是另一台主机(M5-4)

图片/语音/文件/视频的字节不在 iLink 上,在微信 CDN 上,而且是 AES-128-ECB 密文。
照官方 `src/cdn/`(`cdn-url.ts` / `aes-ecb.ts` / `pic-decrypt.ts`)与 `src/media/
media-download.ts` 重新实现。两件事值得单独记:

- **CDN 请求不带 iLink 的头**。官方用的是裸 `fetch`;把 `Authorization` 发到另一台
  主机就是**把 bot_token 泄给第三方**。
- **`aes_key` 有两种编码**,认漏一种就是"某类附件永远解不开":base64(16 原始字节)
  走图片,base64(32 个 ASCII 十六进制字符)走文件/语音/视频。
"""

import base64
import binascii
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# iLink-App-Id:官方 package.json 的 ilink_appid 字段。
APP_ID = "bot"
# iLink-App-ClientVersion:官方的编码是 (major<<16)|(minor<<8)|patch,
# 跟着 npm 包版本走。v2.4.6 → 132102。换版本要跟着改,别硬写十进制。
_VERSION = (2, 4, 6)
CLIENT_VERSION = (_VERSION[0] << 16) | (_VERSION[1] << 8) | _VERSION[2]
CHANNEL_VERSION = ".".join(str(p) for p in _VERSION)

# 官方 types.ts 的常量。message_type: 1=USER 2=BOT;message_state: 2=FINISH。
_MESSAGE_TYPE_BOT = 2
_MESSAGE_STATE_FINISH = 2
_ITEM_TYPE_TEXT = 1
# item type → 附件种类。**11/12(TOOL_CALL_START/RESULT)不在表里是有意的**:
# 它们是机器人自己的工具调用回显,不是用户递来的东西——认成附件就是把模型说过的话
# 当成用户递来的再喂回去(P1-1 那一族)。
_ITEM_MEDIA_KINDS: dict[int, str] = {2: "image", 3: "voice", 4: "file", 5: "video"}
# 每种附件的字段前缀,官方 types.ts:image_item / voice_item / file_item / video_item。
_ITEM_FIELDS: dict[str, str] = {
    "image": "image_item",
    "voice": "voice_item",
    "file": "file_item",
    "video": "video_item",
}

# 扫码登录用的 bot_type。官方这一档构建固定用 3。
_BOT_TYPE = "3"
# 长轮询:服务端会把 getupdates 挂住,直到有消息或超时。官方默认 35s;
# 客户端超时要比它宽一点,否则每次都是客户端先掐断。
LONG_POLL_TIMEOUT = 35.0
_API_TIMEOUT = 15.0

_STALE_TOKEN_CODE = -14

# 官方 auth/accounts.ts 的 CDN_BASE_URL。服务端多数时候会直接给 full_url,
# 给不了才拼这个(官方 ENABLE_CDN_URL_FALLBACK 默认也是开的)。
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
# 单个附件的上限。官方是 100 MB,**这里砍到 16 MB**:目标机 2C2G,一个百兆附件
# 整块读进内存就是把 Steward 一起 OOM 掉,而失效形态会是"半夜没了"。
MAX_MEDIA_BYTES = 16 * 1024 * 1024
_MEDIA_TIMEOUT = 60.0
_AES_BLOCK_BITS = 128
_HEX_KEY_RE = re.compile(r"^[0-9a-fA-F]{32}$")


class ILinkError(RuntimeError):
    """iLink 返回了非零错误码。

    `stale_token` 只是"服务端说 -14"的转述,**不代表一定是 token 过期**——见模块 docstring。
    上层拿它决定要不要重连;这里不做任何停机。
    """

    def __init__(self, message: str, *, code: int) -> None:
        super().__init__(message)
        self.code = code
        self.stale_token = code == _STALE_TOKEN_CODE


@dataclass(frozen=True)
class Credentials:
    """扫码确认后拿到的一套凭据。**响应里是平铺的**,不是官方文档说的嵌在 `data` 里
    ——文档与实际不符,以实测为准。"""

    bot_token: str
    bot_id: str
    user_id: str
    base_url: str


# 官方 login-qr.ts 列的全部状态。**这里只需要分三类**:确认了 / 这张码废了 / 还在等。
# `expired` 是码本身过期;`verify_code_blocked` 是被拦下,同样得换一张新的。
# 两个 `*_redirect` 要换轮询主机,本步没做——它们落进"还在等",由调用方的**超时下界**
# 兜住(见 wechat.relogin:一张码等够时限就换新的,不靠把状态枚举全)。
_DEAD_QR_STATES = frozenset({"expired", "verify_code_blocked"})


@dataclass(frozen=True)
class QrStatus:
    """一次扫码状态轮询的结果。**三态,不是两态。**

    原来 confirmed 之外一律返回 None,调用方分不出"还没扫"和"这张码已经废了"
    ——于是过期之后会对着一张死码轮询到天亮。助手静默死掉,只能人工重启进程。
    """

    raw: str
    credentials: Credentials | None = None

    @property
    def confirmed(self) -> bool:
        return self.credentials is not None

    @property
    def dead(self) -> bool:
        """这张码不用再等了,得换一张。"""
        return self.raw in _DEAD_QR_STATES


@dataclass(frozen=True)
class MediaRef:
    """一份附件在 CDN 上的**位置和钥匙**——还没下载。

    分成两步(先拿引用、再决定下不下载)不是为了优雅:下载要走另一台主机、可能几十兆、
    可能失败,而收信这一批必须先把游标推进去。把它揉进 `get_updates` 的话,一次 CDN
    抖动就能把整条通道钉住。
    """

    kind: str
    encrypted_query_param: str
    full_url: str
    # base64 编码的密钥;空串表示这份是明文(官方 downloadPlainCdnBuffer 那一支)。
    aes_key_b64: str
    file_name: str = ""


@dataclass(frozen=True)
class InboundMessage:
    """一条收到的文本消息。

    `context_token` 决定回信路由到哪个会话,**每收到一条就要覆盖存下来**
    ——它也是主动推送的凭据(官方实现里它没有任何过期逻辑)。
    """

    message_id: int
    from_user_id: str
    text: str
    context_token: str
    # M5-4:附件引用。**纯附件消息的 text 是空串,它照样是一条消息**——
    # 原来空文本就 `continue`,于是一条纯图片消息在协议层就人间蒸发了。
    media: tuple[MediaRef, ...] = ()


def _random_uin() -> str:
    """X-WECHAT-UIN:随机 uint32 → 十进制字符串 → base64(照官方 `randomWechatUin`)。

    形状错了照样是 -14,而 -14 什么都不告诉你,所以这里逐字对着官方的编码写。
    """
    return base64.b64encode(str(secrets.randbelow(2**32)).encode("ascii")).decode("ascii")


def _text_of(item_list: Any) -> str:
    """把 item_list 里的文本条目拼起来;不认识的条目(图片/语音)跳过。

    跳过而不是报错:一条不认识的附件不该让整轮消息丢掉——用户看到的会是"它没反应"。
    """
    if not isinstance(item_list, list):
        return ""
    parts = [
        str(item.get("text_item", {}).get("text", ""))
        for item in item_list
        if isinstance(item, dict) and item.get("type") == _ITEM_TYPE_TEXT
    ]
    return "".join(p for p in parts if p)


def _media_refs(item_list: Any) -> tuple[MediaRef, ...]:
    """把 item_list 里的附件条目转成引用。不认识的类型跳过,和文本那支同一个理由。"""
    if not isinstance(item_list, list):
        return ()
    refs = []
    for item in item_list:
        if not isinstance(item, dict):
            continue
        kind = _ITEM_MEDIA_KINDS.get(item.get("type", 0), "")
        if not kind:
            continue
        body = item.get(_ITEM_FIELDS[kind]) or {}
        media = body.get("media") or {}
        if not (media.get("encrypt_query_param") or media.get("full_url")):
            continue
        refs.append(
            MediaRef(
                kind=kind,
                encrypted_query_param=str(media.get("encrypt_query_param") or ""),
                full_url=str(media.get("full_url") or ""),
                aes_key_b64=_aes_key_b64(body, media),
                file_name=str(body.get("file_name") or ""),
            )
        )
    return tuple(refs)


def _aes_key_b64(body: dict[str, Any], media: dict[str, Any]) -> str:
    """图片优先用 `image_item.aeskey`(十六进制字符串),其余用 `media.aes_key`。

    照官方 media-download.ts:它把 hex 转成 base64 再交给 parseAesKey。两边编码不同
    是真机事实,不是冗余——挑错一个的后果是那类附件**永远解不开**。
    """
    hex_key = str(body.get("aeskey") or "")
    if hex_key and _HEX_KEY_RE.match(hex_key):
        return base64.b64encode(bytes.fromhex(hex_key)).decode("ascii")
    return str(media.get("aes_key") or "")


def _parse_aes_key(aes_key_b64: str) -> bytes:
    """照官方 `parseAesKey`:两种编码都认,认不出就报错而不是拿半把钥匙去解。"""
    try:
        decoded = base64.b64decode(aes_key_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ILinkError(f"aes_key 不是合法 base64:{exc}", code=0) from exc
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32 and _HEX_KEY_RE.match(decoded.decode("ascii", "ignore")):
        return bytes.fromhex(decoded.decode("ascii"))
    raise ILinkError(
        f"aes_key 解出来是 {len(decoded)} 字节,既不是 16 原始字节也不是 32 位十六进制", code=0
    )


def _decrypt_aes_ecb(ciphertext: bytes, key: bytes) -> bytes:
    """AES-128-ECB + PKCS7,照官方 `aes-ecb.ts`。

    ECB 是微信那头定的,不是我们选的——所以 `noqa: S305` 是"照协议办事"而不是
    "图省事"(G4:抑制要最小范围 + 写明理由)。
    """
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()  # noqa: S305
    unpadder = padding.PKCS7(_AES_BLOCK_BITS).unpadder()
    plain = decryptor.update(ciphertext) + decryptor.finalize()
    try:
        return unpadder.update(plain) + unpadder.finalize()
    except ValueError as exc:
        raise ILinkError(f"附件解密失败(填充不对,多半是密钥不匹配):{exc}", code=0) from exc


class ILinkClient:
    """iLink 的 HTTP 门面。`http` 是给报文级测试留的传输注入口,生产不传。"""

    def __init__(
        self,
        *,
        base_url: str,
        token: str | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._http = http or httpx.AsyncClient(timeout=LONG_POLL_TIMEOUT + 5)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ── 报文 ────────────────────────────────────────────────────────────

    # 超时参数叫 timeout_s 不叫 timeout:一是带上单位,二是 ruff 的 ASYNC109 会把
    # "async 函数收 timeout 参数"当成自己搓超时。这里的超时是**转交给 httpx** 的,
    # 它按连接/读/写分档,比在外面套一层 asyncio.timeout 更准——改名比抑制规则诚实。

    def _headers(self) -> dict[str, str]:
        """**唯一**构造请求头的地方。六个全带,一个都不少——理由见模块 docstring。"""
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": _random_uin(),
            "iLink-App-Id": APP_ID,
            "iLink-App-ClientVersion": str(CLIENT_VERSION),
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token.strip()}"
        return headers

    def _base_info(self) -> dict[str, str]:
        """官方每个 POST body 都带这个。照带,免得哪天服务端开始校验。"""
        return {"channel_version": CHANNEL_VERSION, "bot_agent": "Lararium"}

    @staticmethod
    def _raise_for_error(payload: dict[str, Any]) -> dict[str, Any]:
        """两个端点的错误信封**字段名不一样**:登录那套是 `errcode`/`errmsg`,
        收发那套是 `ret`/`err_msg`。两种都认——认漏一种就会把失败当成功。"""
        for code_key, msg_key in (("errcode", "errmsg"), ("ret", "err_msg")):
            code = payload.get(code_key)
            if isinstance(code, int) and code != 0:
                message = payload.get(msg_key) or payload.get("errmsg") or "(无错误信息)"
                raise ILinkError(f"iLink {code_key}={code}: {message}", code=code)
        return payload

    async def _post(
        self, endpoint: str, body: dict[str, Any], *, timeout_s: float
    ) -> dict[str, Any]:
        response = await self._http.post(
            f"{self.base_url}/{endpoint}",
            content=json.dumps({**body, "base_info": self._base_info()}, ensure_ascii=False),
            headers=self._headers(),
            timeout=timeout_s,
        )
        response.raise_for_status()
        return self._raise_for_error(response.json())

    async def _get(self, endpoint: str, *, timeout_s: float) -> dict[str, Any]:
        response = await self._http.get(
            f"{self.base_url}/{endpoint}", headers=self._headers(), timeout=timeout_s
        )
        response.raise_for_status()
        return self._raise_for_error(response.json())

    # ── 登录(不需要任何认证)──────────────────────────────────────────

    async def request_qrcode(self) -> tuple[str, str]:
        """要一张登录二维码,返回 (轮询用的 qrcode, 给用户看的图片链接)。"""
        payload = await self._get(
            f"ilink/bot/get_bot_qrcode?bot_type={_BOT_TYPE}", timeout_s=_API_TIMEOUT
        )
        return str(payload.get("qrcode", "")), str(payload.get("qrcode_img_content", ""))

    async def poll_qrcode_status(self, qrcode: str) -> QrStatus:
        """长轮询扫码状态。**还没扫**是正常控制流不是错误;**码废了**要能和它分开
        ——分不开的话调用方只能对着死码一直等(见 QrStatus 的 docstring)。"""
        payload = await self._get(
            f"ilink/bot/get_qrcode_status?qrcode={qrcode}", timeout_s=LONG_POLL_TIMEOUT
        )
        raw = str(payload.get("status", ""))
        if raw != "confirmed":
            return QrStatus(raw=raw)
        return QrStatus(
            raw=raw,
            credentials=Credentials(
                bot_token=str(payload.get("bot_token", "")),
                bot_id=str(payload.get("ilink_bot_id", "")),
                user_id=str(payload.get("ilink_user_id", "")),
                base_url=str(payload.get("baseurl") or self.base_url),
            ),
        )

    # ── 收发 ────────────────────────────────────────────────────────────

    async def get_updates(self, cursor: str) -> tuple[list[InboundMessage], str]:
        """长轮询收信。返回 (消息列表, 新游标)。**首次传空串**。

        游标必须由调用方持久化——不存就会重收或漏收。
        """
        payload = await self._post(
            "ilink/bot/getupdates", {"get_updates_buf": cursor}, timeout_s=LONG_POLL_TIMEOUT
        )
        messages = []
        for raw in payload.get("msgs") or []:
            text = _text_of(raw.get("item_list"))
            media = _media_refs(raw.get("item_list"))
            # 文本和附件**都**空才是一条什么也没有的消息(比如只有工具回显)。
            # 只看 text 的话,纯图片消息会在这里被静默丢掉。
            if not text and not media:
                continue
            messages.append(
                InboundMessage(
                    message_id=int(raw.get("message_id") or 0),
                    from_user_id=str(raw.get("from_user_id", "")),
                    text=text,
                    context_token=str(raw.get("context_token", "")),
                    media=media,
                )
            )
        return messages, str(payload.get("get_updates_buf") or cursor)

    async def download_media(self, ref: MediaRef) -> bytes:
        """把一份附件取下来并解密,返回明文字节。

        **不带 iLink 的头**:CDN 是另一台主机,把 Authorization 发过去等于把 bot_token
        泄给第三方(官方那边用的也是裸 fetch)。超限当场拒——见 MAX_MEDIA_BYTES。
        """
        url = (
            ref.full_url
            or f"{CDN_BASE_URL}/download?encrypted_query_param={quote(ref.encrypted_query_param, safe='')}"
        )
        chunks: list[bytes] = []
        size = 0
        async with self._http.stream("GET", url, timeout=_MEDIA_TIMEOUT) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_MEDIA_BYTES:
                    raise ILinkError(
                        f"附件超过 {MAX_MEDIA_BYTES // 1024 // 1024} MB 上限,不下载", code=0
                    )
                chunks.append(chunk)
        data = b"".join(chunks)
        if not ref.aes_key_b64:
            return data  # 官方 downloadPlainCdnBuffer 那一支:没有密钥就是明文
        return _decrypt_aes_ecb(data, _parse_aes_key(ref.aes_key_b64))

    async def send_text(self, *, to_user_id: str, text: str, context_token: str) -> None:
        """回一条文本。body 照官方形状:`from_user_id` 空串由服务端推断,
        `message_type=2`(BOT)、`message_state=2`(FINISH),`context_token` 决定会话路由。"""
        await self._post(
            "ilink/bot/sendmessage",
            {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_user_id,
                    # 这条消息的幂等键,自造。官方也是每条一个新的。
                    "client_id": f"lararium-{uuid.uuid4().hex}",
                    "message_type": _MESSAGE_TYPE_BOT,
                    "message_state": _MESSAGE_STATE_FINISH,
                    "context_token": context_token,
                    "item_list": [{"type": _ITEM_TYPE_TEXT, "text_item": {"text": text}}],
                }
            },
            timeout_s=_API_TIMEOUT,
        )
