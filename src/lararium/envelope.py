import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# M4-7:主动推送(夜间归拢/压缩的提醒)是**系统自己开口**,来源必须能和用户原话分开
# ——L0 渲染靠它走「(系统触发 · source/channel)」那一支。用现成的 "cron" 是小谎:
# 它不是定时器触发的,是 worker 空闲跑完归拢/压缩之后触发的。
Source = Literal["user", "cron", "module_event", "sweep"]

# M5-4:附件种类。四种对应 iLink 的 IMAGE/VOICE/FILE/VIDEO,词是给人看的
# ——`Attachment.as_line()` 和 M5-5 的取回工具用**同一份**,不许各写各的
# (`_render_note` 那次的教训:两个出口两套渲染,总有一个先漂)。
AttachmentKind = Literal["image", "voice", "file", "video"]
_KIND_WORDS: dict[str, str] = {
    "image": "图片",
    "voice": "语音",
    "file": "文件",
    "video": "视频",
}
# media_type → 落盘后缀。后缀只是给人和文件管理器看的,**权威是 media_type**;
# 认不出来就 .bin,不去猜。
_SUFFIXES: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "audio/silk": "silk",
    "video/mp4": "mp4",
}
_DEFAULT_SUFFIX = "bin"
# 正文里那行只放短 id。全长 64 位十六进制会**永久地**乘进后续每一轮 L0 的成本,
# 而 L0 的预算算术对图片一无所知(M5-5 约束 1)。12 位十六进制 = 48 bit,
# 单用户助手一辈子的图也撞不上,同时它还得当"把那张图取回来"的键用。
SHORT_ID_CHARS = 12
# 一条消息最多挂几个附件。信封是所有外部输入的入口,列表长度也是输入。
MAX_ATTACHMENTS = 8


def kind_word(kind: str) -> str:
    """附件种类的中文词。**和 `Attachment.as_line()` 取的是同一份**——两个出口各写一套词,
    总有一个先漂(`_render_note` 那次的教训)。取不到就原样返回,不编。"""
    return _KIND_WORDS.get(kind, kind)


class Attachment(BaseModel):
    """一份落在 `{data_dir}/media/` 下的附件的**引用**——不是字节。

    字节留在文件里、按内容哈希不可变;信封、起居注、L0 里流动的一律是这个引用。
    理由有两条:一是把二进制塞进信封等于把它塞进每一次序列化和每一行日志;
    二是 M5-5 要求"历史轮只留一行文本引用",那一行的锚点就是这里的 `short`。
    """

    model_config = ConfigDict(validate_assignment=True)

    kind: AttachmentKind
    # 内容哈希。**它同时是文件名**,所以形状必须在类型上立死:能自报路径的字段
    # 就是路径穿越的入口(`../../prompts/character.default.md`),而人设被改的后果
    # 是之后每一轮都听新的(不可协商第 1 条)。
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = Field(pattern=r"^[a-z]+/[a-z0-9.+-]{1,64}$")

    @property
    def short(self) -> str:
        """正文那行里露出的短 id;也是以后按 id 取回原件的键。"""
        return self.sha256[:SHORT_ID_CHARS]

    @property
    def path(self) -> str:
        """相对 `data_dir` 的存放位置。**由哈希算出来,不是外面传进来的。**"""
        return f"media/{self.sha256}.{_SUFFIXES.get(self.media_type, _DEFAULT_SUFFIX)}"

    def as_line(self) -> str:
        """正文里代表这份附件的那一行人话。

        下游一切按文本走的东西(L0 渲染、词法检索、压缩预算)因此都不用动
        ——这正是 `content` 仍然是字符串的意义。
        """
        return f"({_KIND_WORDS[self.kind]} · media/{self.short}…)"


class Envelope(BaseModel):
    # 信封是**所有外部输入**的入口,校验不该有"从旁边绕进来"的路。
    # 曾经 env.id = client_id 这行事后赋值绕过了校验(P1-4 换字段),打开
    # validate_assignment 后任何赋值都会重新过一遍类型/pattern——下次轮到谁
    # 也不会再从旁边溜进来。
    model_config = ConfigDict(validate_assignment=True)

    # id 是协议上就由客户端提供的(幂等键),却被 search_history 渲染在围栏**外**——
    # 自由文本能伪装成系统的框定语(P1-4,这次更硬)。所以它必须是 32 位 hex,
    # 在类型上立死,不许让任何别的东西流进来。
    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    source: Source
    # channel 会被插在不可信内容的框定语里(且在围栏外),所以它必须是个标识符而不是
    # 自由文本。M2 的 ingress 是它的入口:路由名由服务端给,但校验要立在类型上,
    # 不能指望每个调用方都自觉。
    channel: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")
    content: str
    # M5-4:附件引用。`content` **仍是字符串**——它是所有外部输入的入口,
    # validate_assignment 那条纪律不许绕;图片对应的 content 是 `as_line()` 那一行人话。
    attachments: list[Attachment] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)
    meta: dict[str, Any] = Field(default_factory=dict)
    ts: datetime

    @classmethod
    def new(
        cls,
        *,
        source: Source,
        channel: str,
        content: str,
        attachments: list[Attachment] | None = None,
        meta: dict[str, Any] | None = None,
        id: str | None = None,
    ) -> "Envelope":
        # 客户端给 id 就构造时带上(让 Envelope 自己把关,非法即 ValidationError);
        # 不给才生成。绝不构造后再赋值绕过校验。
        return cls(
            id=id or uuid.uuid4().hex,
            source=source,
            channel=channel,
            content=content,
            attachments=attachments or [],
            meta=meta or {},
            ts=datetime.now(UTC),
        )
