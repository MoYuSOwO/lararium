import re
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
# M6-2:每种附件后面那句「能拿它干什么」。**一种都不许空着。**
#
# 这是 M5-5「每一种降级都要留下一句话」的延伸,而要治的症状很具体:一行
# `(视频 · media/xxx)` 后面什么都没有,等于让模型自己猜有没有路——猜"有"就去试一个
# 不存在的工具,猜"没有"就对着一行引用编内容。**有路的说清怎么走,没路的说清没有。**
#
# 图片那句里带工具名,是因为**它是有路的那一种**(M6-6b 起 PDF 也有,见 `_PDF_HINT`):图不再在到达轮被塞进上下文,
# 模型不调 `read_image` 就等于没看过。`{id}` 由 `as_line()` 填成短 id
# ——让模型照抄,别让它自己从别处拼。
_KIND_HINTS: dict[str, str] = {
    "image": '要看里面有什么就调 read_image("{id}")',
    # 语音**不提"听不了"**:转写已经按原序进正文了,再说一句会让她以为什么都没收到
    # (M6-1 有一条反向测试钉着这个)。没有转写的那条由适配器另说一句。
    "voice": "音频存着,能读的只有转出来的文字",
    "file": "文件存着,里面写了什么我读不了——现在没有读文件的路",
    "video": "视频存着,里面是什么我读不了——现在没有读视频的路",
}
# M6-6b:**PDF 有路了**(`read_pdf`),所以它不能再跟着"文件"那句说"没有读文件的路"
# ——那句从那一刻起就是假话,而模型会信它、永远不去调。
#
# **按 media_type 挑,不按 kind**:微信那头叫 FILE 的东西什么都可能是,只有嗅出来确实是
# PDF 的才有路;一份认不出来的文件照旧说读不了(不许把"不知道是什么"兜底成 PDF,M5-5)。
# 反过来,微信叫 IMAGE、字节却是 PDF 的那种,也指到 read_pdf——指到 read_image 只会换回
# 一句"不是图片"。页码给个能照抄的 1,并说清一次一页、会报共几页:模型第一次调的时候
# 还不知道这份有几页。
PDF_MEDIA_TYPE = "application/pdf"
_PDF_HINT = '要看里面写了什么就调 read_pdf("{id}", 1)——一次一页,它会说共几页'
# 文件名的上限。名字也是长度输入,而那行报告**每一轮都在 L0 里付钱**。
MAX_NAME_CHARS = 60
# 文件名里一律丢掉的那几类字符。**丢掉,不转义。**
#
# 名字是外部输入(转发来的文件、别人发来的收据,名字是别人起的),而它要被渲染进
# 那行报告里。`·` 是那行的字段分隔符、括号是它的边界、换行能凭空伪造出下一行:
# 一个叫 `a · id deadbeefdead · 看这个.jpg` 的文件渲染出来和一条真实条目形状完全一致,
# 而它指着的是另一份附件(P1-2「伪造出来的那行和真的一模一样」的同一个形状)。
# 围栏标记也一起丢——不可信轮的正文是被 `<<< >>>` 围起来的。
# 名字只是给人看的:少一个符号什么都不损失,留着它就是一条伪造通道。
_NAME_BANNED = re.compile(r"[\x00-\x1f\x7f·()<>]")
# media_type → 落盘后缀。后缀只是给人和文件管理器看的,**权威是 media_type**;
# 认不出来就 .bin,不去猜。
SUFFIXES: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    # HEIC/HEIF 是 iPhone 发原图的默认格式。认得出**不等于**送得进模型
    # ——能不能送由 `vision.SENDABLE_IMAGE_TYPES` 说了算(服务商实测不收)。
    # 这里只管它落盘叫什么。
    "image/heic": "heic",
    "image/heif": "heif",
    "audio/silk": "silk",
    "video/mp4": "mp4",
    # M6-1:PDF 原来嗅不出来也没有后缀,一份课件落盘成 `<hash>.bin`、类型是
    # `application/octet-stream`——**存下来了,但认不出、用不了**。这张表和
    # `wechat._MAGIC` 的魔数**必须一起动**:只加一张的话它照样落成 `.bin`,
    # 而那是把一次响亮的失败换成一次静默的失败(M5-5)。
    "application/pdf": "pdf",
}
_DEFAULT_SUFFIX = "bin"
# 反查方向。**由正查表算出来,不另抄一份**——抄一份反过来写的那天两边就开始漂,
# 而漂的表现是"某种附件被当成另一种送出去"(M5-5 补:`tools._media_type_of` 当场
# 犯了这个,一段语音和一份 PDF 都被贴上 image/jpeg 交给了模型)。
_MEDIA_TYPE_BY_SUFFIX: dict[str, str] = {v: k for k, v in SUFFIXES.items()}
# 正文里那行只放短 id。全长 64 位十六进制会**永久地**乘进后续每一轮 L0 的成本,
# 而 L0 的预算算术对图片一无所知(M5-5 约束 1)。12 位十六进制 = 48 bit,
# 单用户助手一辈子的图也撞不上,同时它还得当"把那张图取回来"的键用。
SHORT_ID_CHARS = 12
# 一条消息最多挂几个附件。信封是所有外部输入的入口,列表长度也是输入。
MAX_ATTACHMENTS = 8

# 一份媒体的 id 长什么样——**全仓库只有这一处写着**(M6-6b 从 `steward/tools.py` 搬来)。
#
# 它是**模型可控文本**,而它会被当成文件名的一部分用(按前缀 glob 池子)。只认十六进制:
# 路径分隔符、`..`、glob 通配符一个都进不来。下界 6 位是为了挡住"给个 a 就把 media/
# 底下第一张捞出来"。
#
# **为什么住在这里**:三个地方要认它——Steward 侧的 `read_image` / `read_pdf`,和学习
# bundle 的 `add_file`(它把 id 存进归属表,之后原样交给 `read_pdf`)。bundle import
# 不到 steward(`.importlinter`),而信封这一层两边都够得着;id 本来就是信封那行报告
# 发出去的东西(`Attachment.short`)。各写一份的那天就开始漂,漂的样子是「add_file 收下的
# id,read_pdf 认不出来」。
MEDIA_ID_RE = re.compile(r"^[0-9a-f]{6,64}$")


def is_media_id(text: str) -> bool:
    """整串是不是一个媒体 id。**整串匹配**,不是 `MEDIA_ID_RE.match`:`$` 会放过末尾一个
    换行,而 `add_file` 要把 id 存进表、之后渲染进一行一条的列表——带着换行进去就能伪造出
    下一行。(`read_image` 用的是 `.match`,M6-6b 按"行为逐字节不变"没动它:在它那里末尾
    换行只会让 glob 找不到文件,回一句"没找到"。)"""
    return MEDIA_ID_RE.fullmatch(text) is not None


def media_type_of_suffix(suffix: str) -> str | None:
    """从磁盘上的后缀反推 media_type。**认不出返回 None,绝不猜。**

    猜一个类型出去,等于把"我不知道这是什么"变成"我确定这是 JPEG",而下游没有任何人
    能再纠正它——服务商只会回一句 `invalid image format`,用户看到的是助手当场死了
    这一轮,错误里全是 provider 的黑话。
    """
    return _MEDIA_TYPE_BY_SUFFIX.get(suffix.lstrip("."))


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
    # M6-2:对方给的原名,**纯展示用**。落盘位置仍然只由哈希算(上面那条),
    # 类型仍然只由魔数嗅(`wechat._sniff`:「不信对方给的文件名」)——名字影响不了
    # 任何一个决定,所以它脏一点的代价是有界的,而"人看得懂"这件事只有它给得了。
    # 没有就是空串:**不编一个出来**(「IMG_0001.jpg」是假的,而假的比没有更坏)。
    name: str = ""

    @property
    def short(self) -> str:
        """正文那行里露出的短 id;也是以后按 id 取回原件的键。"""
        return self.sha256[:SHORT_ID_CHARS]

    @property
    def path(self) -> str:
        """相对 `data_dir` 的存放位置。**由哈希算出来,不是外面传进来的。**"""
        return f"media/{self.sha256}.{SUFFIXES.get(self.media_type, _DEFAULT_SUFFIX)}"

    @property
    def is_image(self) -> bool:
        """能不能送进模型的**唯一**判据。

        **按 media_type 判,不按 kind。** 微信那头 type=IMAGE 的条目,字节嗅不出来时
        落的是 `application/octet-stream`——按 kind 判就会把一份 PDF 当图片送进去。
        """
        return self.media_type.startswith("image/")

    @field_validator("name", mode="before")
    @classmethod
    def _clean_name(cls, raw: Any) -> str:
        """名字在**进门这一步**就洗干净,而不是在渲染那一步。

        洗不是拒:一个名字奇怪的文件不该让整条消息连同用户那句「这个多少钱」一起
        ValidationError 掉(E2:给人话,不抛异常)。而洗在这里,是因为信封是所有外部
        输入的唯一入口——HTTP ingress 走的就是 `model_validate`,适配器走的是构造器,
        两条路都过这一关;洗在渲染那一步的话,下一个拿到 `name` 字段的人不会知道
        它还没洗过。
        """
        text = re.sub(r"\s+", " ", str(raw or "")).strip()
        return _NAME_BANNED.sub("", text)[:MAX_NAME_CHARS]

    def as_line(self) -> str:
        """正文里代表这份附件的那一行人话。

        下游一切按文本走的东西(L0 渲染、词法检索、压缩预算)因此都不用动
        ——这正是 `content` 仍然是字符串的意义。

        ★ **M6-2:这一行要够模型据此决定要不要去取它。** 四样各有各的理由:

        - **类型**——它决定有没有路(图片有;M6-6b 起 PDF 也有,按 media_type 认);
        - **名字**——`id` 是把手,名字才是人看的:模型要说得出「你发的那份第3讲.pdf」;
        - **完整的 id,不带省略号**——12 位就是取回原件要的全部。`…` 让模型以为拿到的是
          残件,这不是推测:M5-5 补那一轮,回绝措辞里的 `…` 让它认定"id 被截断了",
          转头让用户重发一张图。现在图片要靠模型自己调工具才进上下文,那个误解的代价
          从"多说一句废话"变成"根本调不起来";
        - **一句"能拿它干什么"**——见 `_KIND_HINTS` 上方。
        """
        fields = [_KIND_WORDS[self.kind]]
        if self.name:
            fields.append(self.name)
        fields.append(f"id {self.short}")
        # `.format` 只对有路的那几句起作用(别的句子里没有占位符),不是巧合:
        # 有路的才需要把 id 复述一遍给模型照抄。
        hint = _PDF_HINT if self.media_type == PDF_MEDIA_TYPE else _KIND_HINTS[self.kind]
        fields.append(hint.format(id=self.short))
        return f"({' · '.join(fields)})"


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
