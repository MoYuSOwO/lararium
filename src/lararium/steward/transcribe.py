"""后台把收到的 PDF 一页页转成文字,写进 `pdftext` 缓存(M6-6c)。

用户拍板的形状:「**当然是直接转然后按照 id 缓存,因为 ai 随时有可能读啊**」。文件在主控的
池子里,`read_pdf` 是主控的内置工具,任何一轮对话都能读任何一份收到的 PDF——随手发个合同
问「第 2 页写的啥」,它从来不会被归到哪门课下。所以**触发点在收件那一侧,和 bundle 无关**。

**挂在哪**:`Steward.process_next` 认领信封之后调 `notice(attachments)`——只看 media_type,
是 PDF 就置一下 `wake`,**一个字节都不等**。真正干活的是 lifespan 里和 worker 并排跑的
`run()`。认领那一刻是每个信封都必经的一个点(HTTP 入站、数据面、重试、重启后重新排队,
全都从这里过);而入站那个 HTTP 处理器按 DESIGN §9 只写收件箱、不碰业务逻辑。

**干活的时候扫的是池子,不是信封**:`notice` 只负责叫醒,每一步都去 `media/` 里找还没登记的
`*.pdf`。于是三种情况是同一条路,不用各写一份:刚收到的、**服务重启时转到一半的**(缓存在库里,
已转的页不重转)、**6c 上线之前就躺在池子里的**。认 PDF 的判据是落盘时嗅出来的后缀
(和 `read_pdf` 同一条,`.bin` 哪怕字节里有 `%PDF-` 也不碰——M5-5)。

**不把聊天挤慢**:转换和聊天用同一个 key。一次只转一页(单路);**有信封在排队或正在处理时
不开新的一页**(`chat_busy`),最多和聊天重叠正在飞的那一页。本地那一半不是瓶颈:一页带大图
的讲义开文件 + 画 + 编 PNG 实测中位 59 ms(REVIEW M6-6c),模型那一次按 PLAN 实测约 1.9 秒
——并发几路省的只是模型那一段,换来的是同一个 key 上同时多几个请求,那正是要避免的。

**失败**:照 M5-30 的形状分清成功 / 失败 / 未完成(状态怎么推见 `pdftext` 模块 docstring)。
一页失败不影响别的页;失败之后整条流水线退避一下,别让一次服务商抖动连着把后面几页的次数也烧掉。

**落起居注**:照 sweep 的先例(`phase: input / output`),**输入先落,输出后落**;输入落的是
模型实收的那一份——指令原文 + 那张图的**引用**(PNG 的 sha256、类型、大小),和 `read_image`
那条路一样落引用不落字节。字节可以从 (PDF 的哈希, 页码, 渲染长边) 原样重画出来。
"""

import asyncio
import contextlib
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from lararium.envelope import PDF_MEDIA_TYPE, SUFFIXES, Attachment, is_media_id
from lararium.steward.journal import Journal
from lararium.steward.model import ModelCallError, ModelReply
from lararium.steward.pdf import RENDER_LONG_SIDE, UnreadablePdf, page_count, render_page
from lararium.steward.pdftext import PdfText
from lararium.steward.vision import ImagePart

logger = logging.getLogger("lararium")

# ★ 转换指令。**改它就是改所有将来的缓存**——已经转好的页不会跟着重转,于是新旧两种写法会
# 在同一个缓存里并存,而谁也看不出哪页是按哪一版转的(起居注里每次调用的原文是查得到的)。
#
# 1-5 条是 PLAN M6-6 那一节里**实测过**的原样(表格页 / 公式页 / 时序图页三个样本:视觉模型 +
# 这段指令赢在表格、公式、图——「OCR 只能转录字形,模型能描述」)。PLAN 的原话是「别改词序、
# 别"优化"措辞;要改,先重新打一遍那三个样本」。
#
# 第 6 条是 6c 加的(**追加在末尾,前五条一个字没动**):现在收到的**任何** PDF 都转,包括转发
# 来的,页面上完全可以写着「忽略以上指令」。它是**说服,不是防线**(M5-5 立的区分):
# 机制那一半是①这次调用不带任何工具,它做不了事;②读出来的时候过刀(`tools.read_pdf` 和
# `web_fetch` 共用那个出口)、拉不可信闩。**加了这一条之后那三个样本还没用真 API 重打过,
# 列在 REVIEW 的"要真机验的"里。**
PAGE_TO_TEXT_PATH = Path("prompts/pdf-page.md")


def load_page_prompt(path: Path = PAGE_TO_TEXT_PATH) -> str:
    """转一页用的那段指令。**住在 `prompts/`,不在代码里**(CONVENTIONS L1:给模型读的文字进文件;
    归拢的 `prompts/sweep.md`、切段的 `prompts/cut.md` 同一个形状,都在组装根读一次)。

    验收补:6c 的任务书让它写成代码里的常量,**是复核方把 L1 忘了**,执行方照做之后自己
    指出了这处冲突。去掉末尾换行:文件习惯以换行结尾,而模型收到的应该就是上面那几条本身。
    **改这个文件就是改所有将来的缓存**——已经转好的页不会重转。
    """
    return path.read_text(encoding="utf-8").rstrip("\n")


# 起居注里这类事件的 kind。**不在 `SEARCHABLE_KINDS` 里**:转出来的文字不许经 search_history
# 以"工具输出"的样子冒出来(按 id 搜内容是 6d,读的是缓存,而且要过刀)。
JOURNAL_KIND = "pdf_text"

# 池子里一份 PDF 的文件名:`<内容哈希>.pdf`。后缀由 media_type 查表,不另写一份;
# 文件名认不认得出用 `envelope.is_media_id`——id 的形状全仓库只写在那一处
# (认不出的,read_pdf 也寻址不到它,转了白转)。
_PDF_SUFFIX = SUFFIXES[PDF_MEDIA_TYPE]


class PageReader(Protocol):
    """转换要的那一种模型调用:一段指令 + 一张图,不带工具。生产里是 `PydanticAIClient`。"""

    async def run_with_image(self, prompt: str, image: ImagePart) -> ModelReply: ...


def journal_entry(sha256: str, page: int) -> str:
    """起居注里这一页的"信封 id"。**确定的**:同一页的每一次尝试都落在一起,
    `journal.replay(...)` 一眼看得出这页调过几次、每次喂了什么、回了什么。"""
    return f"pdf-{sha256}-{page}"


def _unregistered_pdfs(media_dir: Path, known: set[str]) -> list[Path]:
    """池子里还没登记的 PDF,按落盘先后排(先收到的先转)。同步函数,放线程里跑。"""
    if not media_dir.is_dir():
        return []
    found = [
        path
        for path in media_dir.glob(f"*.{_PDF_SUFFIX}")
        if is_media_id(path.stem) and path.stem not in known
    ]
    return sorted(found, key=lambda path: (path.stat().st_mtime, path.name))


# 验收补(M6-6c):按"这次失败**怪不怪这一页**"分三类,不是按"要不要重试"。
#   400 / 413 / 422  服务商拒了这一次请求的内容(这张图)       → 这一页判死
#   401 / 402 / 403 / 404 / 429
#                    服务商在说账号、余额、配置、限流——**不可能是这一页引起的**
#                                                              → 不扣这一页的次数,整条歇一会儿
#   5xx / 超时 / 没有状态码   分不清是哪边                       → 照旧扣次数,封顶 MAX_PAGE_ATTEMPTS
# 原来的写法把 401/403/404 当"明确拒了"当场判死、402/429 扣满三次判死:换一次 key、欠一次费
# (DeepSeek 是预付费,402 是真会发生的),正在排队的那几页就**永久**只剩图,修好也救不回来。
_PAGE_REJECTED = frozenset({400, 413, 422})
_NOT_THIS_PAGE = frozenset({401, 402, 403, 404, 429})


class Transcriber:
    # 失败之后歇 2**连续失败次数 秒,封顶 5 分钟。**连续**:转好一页就清零。
    # 服务商挂一小时,这样只会把少数几页的次数用完,而不是把排队的几十页全判死。
    MAX_BACKOFF = 300.0
    # 聊天那一轮还没完时,隔多久再看一眼。
    CHAT_POLL = 1.0
    # 没活的时候最长睡多久再自己扫一遍池子——兜住丢了的唤醒(同 Worker 那 5 秒的理由),
    # 这里不急,扫一次池子是一次目录遍历。
    IDLE_POLL = 300.0

    def __init__(
        self,
        *,
        pages: PdfText,
        journal: Journal,
        media_dir: Path,
        reader: PageReader,
        chat_busy: Callable[[], bool],
        instructions: str,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._instructions = instructions
        self._pages = pages
        self._journal = journal
        self._media_dir = media_dir
        self._reader = reader
        self._chat_busy = chat_busy
        # 可注入的 sleep:退避与让路的时长由测试核对,不注入就是真 asyncio.sleep。
        self._sleep = sleep or asyncio.sleep
        self._failures = 0
        # 公开:收件那一侧(`notice`)要能叫醒它。
        self.wake = asyncio.Event()

    def notice(self, attachments: list[Attachment]) -> None:
        """收件那一刻调。**只置一个 Event,不许在这里做任何转换**——这一轮的回复一个字节
        都不等它(硬口径:转换绝不在请求路径上)。判据是 media_type,和报告行、read_pdf 同一条。
        """
        if any(a.media_type == PDF_MEDIA_TYPE for a in attachments):
            self.wake.set()

    async def run(self) -> None:
        """常驻:有活一页页干,没活睡到被叫醒(或到点自己再扫一遍)。"""
        while True:
            # 先清再干:干活期间来的唤醒留着,下一圈立刻再扫一遍,不会丢。
            self.wake.clear()
            try:
                worked = await self.step()
            except Exception:
                # 这一步出了代码层面的错(库、磁盘、数据库):歇一下接着来,和 Worker 对毒消息
                # 同一个处置——别让后台杂活停摆。**不会变成无限烧钱**:次数在调模型之前就记了。
                logger.exception("PDF 转换:这一步出错了,歇一下接着来")
                await self._back_off()
                continue
            if not worked:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.wake.wait(), timeout=self.IDLE_POLL)

    async def step(self) -> bool:
        """干一件事:登记池子里新出现的 PDF,再转下一页。没有可转的页回 False。"""
        await self._discover()
        job = self._pages.next_page()
        if job is None:
            return False
        while self._chat_busy():
            await self._sleep(self.CHAT_POLL)
        await self._convert(*job)
        return True

    async def _discover(self) -> None:
        for path in await asyncio.to_thread(
            _unregistered_pdfs, self._media_dir, self._pages.known()
        ):
            try:
                total = await asyncio.to_thread(page_count, path)
            except UnreadablePdf as exc:
                # 加密、坏文件、零页:整份记下原因,一页都不调(那句人话 read_pdf 自己会说)。
                self._pages.register(path.stem, total_pages=0, unreadable=str(exc))
                logger.warning("PDF %s 打不开,不转:%s", path.stem[:12], exc)
                continue
            self._pages.register(path.stem, total_pages=total)

    async def _convert(self, sha256: str, page: int) -> None:
        path = self._media_dir / f"{sha256}.{_PDF_SUFFIX}"
        try:
            png = await asyncio.to_thread(render_page, path, page)
        except UnreadablePdf as exc:
            # 画不出来是这份文件自己的事(pdfium 是确定的),再试也一样:判死,不调模型。
            self._pages.record_failure(sha256, page, str(exc), give_up=True)
            logger.warning("PDF %s 第 %d 页画不出来,不转:%s", sha256[:12], page, exc)
            return
        image = ImagePart(sha256=hashlib.sha256(png).hexdigest(), media_type="image/png", data=png)
        entry = journal_entry(sha256, page)
        where = {"media": sha256, "page": page}
        self._pages.begin_attempt(sha256, page)
        self._journal.append(
            entry,
            JOURNAL_KIND,
            {
                **where,
                "phase": "input",
                "content": self._instructions,
                "image": {
                    "sha256": image.sha256,
                    "media_type": image.media_type,
                    "bytes": len(png),
                    "long_side": RENDER_LONG_SIDE,
                },
            },
        )
        started = time.monotonic()
        try:
            reply = await self._reader.run_with_image(self._instructions, image)
        except ModelCallError as exc:
            self._journal.append(
                entry,
                JOURNAL_KIND,
                {**where, "phase": "output", "ok": False, "content": f"模型调用失败:{exc}"},
            )
            if exc.status in _NOT_THIS_PAGE:
                self._pages.refund_attempt(sha256, page, str(exc))
            else:
                self._pages.record_failure(
                    sha256, page, str(exc), give_up=exc.status in _PAGE_REJECTED
                )
            logger.warning("PDF %s 第 %d 页转文字失败:%s", sha256[:12], page, exc)
            await self._back_off()
            return
        seconds = time.monotonic() - started
        text = reply.text.strip()
        self._journal.append(
            entry,
            JOURNAL_KIND,
            {
                **where,
                "phase": "output",
                "ok": bool(text),
                "content": reply.text,
                "prompt_tokens": reply.prompt_tokens,
                "completion_tokens": reply.completion_tokens,
                "seconds": round(seconds, 2),
            },
        )
        if not text:
            # 回了个空:当失败算,不当"这页没字"缓存——缓存下来就再也不会重转了,
            # 而读的时候图总是在的,空白页判死的代价只是一句"转失败了,只能看图"。
            self._pages.record_failure(sha256, page, "模型回了空的", give_up=False)
            logger.warning("PDF %s 第 %d 页模型回了空的", sha256[:12], page)
            await self._back_off()
            return
        self._pages.save_text(sha256, page, text)
        self._failures = 0
        # L4:每次模型调用都要看得见用量和耗时。
        logger.info(
            "PDF %s 第 %d 页转好了 · prompt=%s completion=%s · %.1f 秒",
            sha256[:12],
            page,
            reply.prompt_tokens,
            reply.completion_tokens,
            seconds,
        )

    async def _back_off(self) -> None:
        self._failures += 1
        await self._sleep(min(2.0**self._failures, self.MAX_BACKOFF))
