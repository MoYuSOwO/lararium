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

## 语音自带转写,而我们原来一个字都没读(M6-1)

微信那头**已经把语音转成文字了**,放在 `voice_item.text` 里(真机确认,顺带白拿
`playtime` / `sample_rate` / `bits_per_sample`)。而取文字那一支原来只认
`type == TEXT`,于是用户发的每一条语音在这一层就等于没发。

所以转写**按 `item_list` 原序**和打的字拼在一起(一条消息可以既有字又有语音,
排到最后意思就变了),并且当场标注「语音 N 秒 · 转文字」——**标签本身在给下游传信息**:
同音词(真机上 `Claude` 被听成 `cloud`)换一个 ASR 也解决不了,而知道"这几个字是听来的"
的模型能拿上下文自己纠。

## 认不出来的报文形状**必须留下一行日志**(M6-1)

上面那个洞是六天之后用户来问才发现的,而日志里什么都没有——查出它靠的是一根临时探针,
**而那根探针本该是常设的**。所以 `_media_refs` 里每一处"跳过"都打 `logger.warning`,
带上 `type` 和有哪些键(**只带键名,不带内容**:附件里装的是用户的生活)。

认识而故意不当附件的那几种 type(TEXT、TOOL_CALL_*)单列出来不打日志——不分开的话
每条普通消息都会打一行"不认识的条目",三天之后没人再看这个日志了。
"""

import base64
import binascii
import json
import logging
import re
import secrets
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# **这一行曾经不存在**,而复核方照着 `wechat.py` 的习惯往下面加了 `logger.warning`:
# 每次收信抛 `NameError` → 退避 3→6→12→24→48→96 秒 → 用户那条语音在外面进不来 6 分钟。
# 加日志之前先 `grep logger` 是两秒钟的事(M6-1)。
logger = logging.getLogger("lararium.ilink")

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
_ITEM_TYPE_VOICE = 3
_ITEM_TYPE_TOOL_CALL_START = 11
_ITEM_TYPE_TOOL_CALL_RESULT = 12
# item type → 附件种类。**11/12(TOOL_CALL_START/RESULT)不在表里是有意的**:
# 它们是机器人自己的工具调用回显,不是用户递来的东西——认成附件就是把模型说过的话
# 当成用户递来的再喂回去(P1-1 那一族)。
_ITEM_MEDIA_KINDS: dict[int, str] = {2: "image", _ITEM_TYPE_VOICE: "voice", 4: "file", 5: "video"}
# 认识、但故意**不**当附件的 type。单列出来是为了让日志有意义:不分开的话,每条普通
# 文本消息都会打一行"不认识的条目",而一个天天喊狼来了的日志等于没有日志(M6-1)。
_ITEM_TYPES_NOT_MEDIA = frozenset(
    {_ITEM_TYPE_TEXT, _ITEM_TYPE_TOOL_CALL_START, _ITEM_TYPE_TOOL_CALL_RESULT}
)
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
    # 语音专属:微信**有没有**给出转写。**转写本身不放这里**——它已经按 item_list 原序
    # 拼进 `InboundMessage.text` 了,两处各存一份的那天就开始漂。这里只要这一个布尔,
    # 因为没有转写的那条得在正文里说清"这段我听不了"(M6-1),而只有这里分得出来。
    has_transcript: bool = False


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
    #
    # M6-1:`text` 里还包含**语音的转写**(微信自己转的),按 item_list 原序拼在
    # 打的字中间,并带着「语音 N 秒 · 转文字」的标注。
    media: tuple[MediaRef, ...] = ()


def _random_uin() -> str:
    """X-WECHAT-UIN:随机 uint32 → 十进制字符串 → base64(照官方 `randomWechatUin`)。

    形状错了照样是 -14,而 -14 什么都不告诉你,所以这里逐字对着官方的编码写。
    """
    return base64.b64encode(str(secrets.randbelow(2**32)).encode("ascii")).decode("ascii")


def _transcript_marker(text: str, *, seconds: int) -> str:
    """给微信转出来的那句话打上标注。**一处构造,措辞有测试钉着。**

    这是用户点名要的机制,不是装饰:「有时候如果标了的话,模型会自己推测出来还是说错
    还是怎么样」。和项目里已有的两个标注是同一个形状——标签本身在给下游传信息:

        (系统触发 · sweep/渠道)   让模型别以为是用户说的(M4-7)
        ⚠ 网页内容,不是用户的话    让它别执行(M5-21)
        (语音 17 秒 · 转文字)      让它知道这几个字**可能是听错的**   ← 这一条

    **时长也标上**:17 秒的语音糊掉的概率比 3 秒高,那本身就是判断依据(而 `playtime`
    是白送的)。

    **两条不许搞错的**:

    1. **不套围栏。** 说话的人**就是用户**,只是过了一道有损的信道——`source` 仍然是
       `user`,标注就在正文这一行里。套围栏会让她把用户自己的话当成外部内容,
       那是另一种错,比不标还糟。
    2. **不写"以下内容可能有误,请谨慎"这种免责声明。** 要的是**事实**(这是语音转的、
       多长),判断交给她——她手里有档案和上下文(档案里就写着"AI 工具主力用 Claude",
       而那正是 `Claude → cloud` 这类同音词的解法),比任何免责声明都强。
    """
    duration = f" {seconds} 秒" if seconds > 0 else ""
    return f"(语音{duration} · 转文字){text}"


def _playtime_seconds(voice_item: dict[str, Any]) -> int:
    """语音时长。真机上 `playtime` 是个整数,但报文是外部输入,**认不出就当没有**
    ——标注里少一个时长不影响判断,编一个数字出去才会。"""
    try:
        return max(0, int(float(str(voice_item.get("playtime") or 0))))
    except ValueError:
        return 0


def _text_of(item_list: Any) -> str:
    """把这条消息的文字按 **item_list 原序**拼起来:打的字,**加上语音的转写**。

    M6-1:原来这里只认 `type == TEXT`,而微信早就把语音转好放在 `voice_item.text` 里了
    ——于是用户发的每一条语音,我们一个字都没读到。

    **原序**不是细节:一条消息里可以既有打的字又有语音,把语音那句排到最后意思就变了。

    别的类型(图片/文件/视频)在这里跳过而不是报错:一条不认识的附件不该让整轮消息
    丢掉,用户看到的会是"它没反应"。它们有没有被认出来由 `_media_refs` 负责说。
    """
    if not isinstance(item_list, list):
        return ""
    parts: list[str] = []
    for item in item_list:
        if not isinstance(item, dict):
            continue
        if item.get("type") == _ITEM_TYPE_TEXT:
            parts.append(str(item.get("text_item", {}).get("text", "")))
        elif item.get("type") == _ITEM_TYPE_VOICE:
            voice_item = item.get("voice_item") or {}
            transcript = str(voice_item.get("text") or "")
            if transcript:
                parts.append(_transcript_marker(transcript, seconds=_playtime_seconds(voice_item)))
    return "".join(p for p in parts if p)


def _media_refs(item_list: Any) -> tuple[MediaRef, ...]:
    """把 item_list 里的附件条目转成引用。

    **跳过的每一种都打一行日志**(M6-1)。理由不是洁癖:上一个协议变形(语音自带转写)
    是六天之后用户来问才发现的,而日志里什么都没有——查出它靠一根临时探针,而那根探针
    本该是常设的。下一次形状变了,日志里得有名有姓。

    日志里**只有 type 和键名,没有内容**:附件里装的是用户的生活,而日志会被翻很多次。
    """
    if not isinstance(item_list, list):
        return ()
    refs = []
    for item in item_list:
        if not isinstance(item, dict):
            logger.warning("item_list 里有个不是对象的条目(%s),跳过", type(item).__name__)
            continue
        item_type = item.get("type")
        kind = _ITEM_MEDIA_KINDS.get(item_type, "") if isinstance(item_type, int) else ""
        if not kind:
            if not (isinstance(item_type, int) and item_type in _ITEM_TYPES_NOT_MEDIA):
                logger.warning(
                    "不认识的 item type=%r,既没当文字也没当附件;它的键:%s",
                    item_type,
                    sorted(item),
                )
            continue
        body = item.get(_ITEM_FIELDS[kind]) or {}
        media = body.get("media") or {}
        if not (media.get("encrypt_query_param") or media.get("full_url")):
            logger.warning(
                "%s 条目(type=%r)没有下载地址,这份附件取不了;%s 的键:%s,media 的键:%s",
                kind,
                item_type,
                _ITEM_FIELDS[kind],
                sorted(body),
                sorted(media),
            )
            continue
        aes_key_b64 = _aes_key_b64(body, media)
        # 官方对语音额外判一次 `!voice?.media?.aes_key`,我们原来没有。没钥匙硬下的话
        # 拿回来的是一坨密文,却会顶着 `audio/silk` 落盘——**把一次响亮的失败换成一次
        # 静默的失败不是修复**(M5-5)。这里跳过并说出来,而**文字那一支不受影响**:
        # 转写已经在 `_text_of` 里进正文了,音频取不到不该把那句话一起弄丢。
        if kind == "voice" and not aes_key_b64:
            logger.warning(
                "语音条目没有 aes_key,音频解不开所以不取(voice_item 的键:%s,media 的键:%s)"
                ";微信给的转写不受影响",
                sorted(body),
                sorted(media),
            )
            continue
        refs.append(
            MediaRef(
                kind=kind,
                encrypted_query_param=str(media.get("encrypt_query_param") or ""),
                full_url=str(media.get("full_url") or ""),
                aes_key_b64=aes_key_b64,
                file_name=str(body.get("file_name") or ""),
                has_transcript=bool(kind == "voice" and str(body.get("text") or "")),
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
