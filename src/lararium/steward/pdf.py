"""PDF 光栅化那一层(M6-6b):打开一份 PDF、数页、把一页画成 PNG。

**pypdfium2 只出现在这个文件里**(D2,`test_pypdfium2_is_imported_in_exactly_one_module`
钉着)。库升级改 API 时只看这一个文件;库自己的异常(`PdfiumError` 之类)也关在这里,
出这个文件的只有一种 `UnreadablePdf`,消息就是给模型的那句人话(同 `WebSearchError`)。

**为什么 PDF 必须先变成图**:模型只吃 webp/png/jpeg/gif,三条"直接喂 PDF"的路 PLAN M6-6
那一节里全试死了(image_url / file_data / 上传,全 400)。这一层就是"变成图"那一步,
**不转文字、不建缓存**——那是 6c,要用户拍板(见 PLAN M6-6 节首)。

★ **一把进程级的锁,少了它整个服务进程会死**:PDFium 不是线程安全的(pypdfium2 自己的
文档写着 "inherently not thread-safe"),而同步工具函数跑在框架的线程池里、一条 assistant
消息里的多个工具调用是**并发**的(M5-8 的形状)。实测(2026-09-13,不加锁 8 线程 x 400 次
渲染,跑 3 次):**3 次进程全部当场崩掉**,退出码 133 / 139 / 133(SIGTRAP / SIGSEGV)
——不是异常,抓不住,服务直接没了。加锁后同样 3 x 400 次 0 错。
`test_concurrent_renders_do_not_bring_the_process_down` 钉着。
"""

import struct
import threading
import zlib
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

# 一页画出来**长边**多少像素。按长边定、不按 DPI:一页进模型吃多少 token 由像素数决定,
# 按 DPI 定的话一张 A0 海报一页就能顶掉几万 token。
#
# **1600 的理由**(2026-09-13,拿一份 A4 讲义——中文正文、表格、公式、红字、7pt 脚注——
# 和一份 macOS 自带的中文许可协议实渲染,切出脚注那一块逐档看):
#
#     长边 1200  A4 848x1200   7pt 脚注汉字约 11 像素高,「删」「树」这类笔画多的字糊在一起
#     长边 1600  A4 1131x1600  7pt 约 15 像素,读得清;16:9 幻灯片 1600x900   ← 选这个
#     长边 2000  A4 1414x2000  正文字比够用多出一截,token 多 56%,换不来能读的新东西
#
# 按常见的"像素 / 750 ≈ token"估:A4 一页约 2400、幻灯片约 1900;一轮最多 4 页
# (和读图共用 `MAX_IMAGES_PER_TURN`)也就一万出头,离压缩水位很远。PNG 实测
# 53 KB(幻灯片)到 505 KB(满页小字的中文协议)。代价说清:A0 海报这种大纸,
# 长边封在 1600 时小字会糊——那是"一页的 token 不随纸张大小涨"的另一面。
#
# 有的服务商会在它那边再缩一次(比如先把短边压到 768),那一层我们管不了;
# 送得清楚是我们这边能做的全部。
RENDER_LONG_SIDE = 1600

# 见模块 docstring。**模块级,而不是挂在某个实例上**:它守的是 pdfium 这个 C 库的进程级
# 全局状态,哪怕进程里有两个 `BuiltinTools`(测试里就有),两边也不许同时进 pdfium。
# F5 禁的是"谁都能改、测试之间互相污染"的模块级状态;一把锁没有任何可观测的值,
# 而把它放进实例恰恰会让它守不住它要守的东西。
_PDFIUM = threading.Lock()


class UnreadablePdf(Exception):
    """这份 PDF(或它的某一页)读不出来。**消息就是给模型的那句人话**(E2)——
    `read_pdf` 直接把它接在 id 后面当返回值用,所以按"模型读了能决定下一步"来写。"""


def page_count(path: Path) -> int:
    """这份 PDF 共几页。打不开抛 `UnreadablePdf`。"""
    with _PDFIUM:
        document = _open(path)
        try:
            return len(document)
        finally:
            document.close()


def render_page(path: Path, page: int) -> bytes:
    """把第 `page` 页(**从 1 数**,和用户嘴里的「第 3 页」一致)画成一张 PNG。

    页码范围由调用方先用 `page_count` 核过——越界是调用方的错,在这里照常抛。
    打不开、这一页装不上、这一页尺寸是 0,抛 `UnreadablePdf`,消息里带着是第几页、共几页。
    """
    with _PDFIUM:
        document = _open(path)
        try:
            total = len(document)
            try:
                sheet = document[page - 1]
            except pdfium.PdfiumError as exc:
                raise UnreadablePdf(
                    f"这份 PDF 共 {total} 页,第 {page} 页是坏的,读不出来。别的页可以试试。"
                ) from exc
            try:
                width, height = sheet.get_size()
                # `not (x > 0)` 而不是 `x <= 0`:NaN 也要落进这一支,不然后面除出一个 NaN 的缩放。
                if not (width > 0 and height > 0):
                    raise UnreadablePdf(
                        f"这份 PDF 共 {total} 页,第 {page} 页的尺寸是 0,画不出来。别的页可以试试。"
                    )
                scale = RENDER_LONG_SIDE / max(width, height)
                # rev_byteorder:pdfium 默认给 BGR,PNG 要 RGB。白底不透明,于是固定是 3 通道。
                bitmap = sheet.render(scale=scale, rev_byteorder=True)
                try:
                    # **在锁里把像素拷出来**:位图的内存归 pdfium 管,出了锁再读就是在和别的
                    # 线程抢同一个库。PNG 压缩是纯 Python + zlib,放到锁外面做。
                    shape = (bitmap.width, bitmap.height, bitmap.stride)
                    pixels = bytes(bitmap.buffer)
                finally:
                    bitmap.close()
            finally:
                sheet.close()
        finally:
            document.close()
    return _png(*shape, pixels)


def _open(path: Path) -> pdfium.PdfDocument:
    """打开一份 PDF,把库的每一种失败都翻成 `UnreadablePdf`。**调用方必须已经持锁。**

    交给 pdfium 的是路径而不是字节:一份几十兆的讲义不必先整份读进 Python。
    """
    try:
        return pdfium.PdfDocument(path)
    except pdfium.PdfiumError as exc:
        # 装载失败的错误码(fpdfview.h 的 FPDF_ERR_*)只分这三支:用户**能做点什么**的是
        # "去掉密码 / 去掉加密再发一次";剩下的(格式坏、没传完、零页)对用户是同一件事。
        if exc.err_code == pdfium_c.FPDF_ERR_PASSWORD:
            raise UnreadablePdf(
                "这份 PDF 加了密码,我打不开。请用户去掉密码(或者另存一份不带密码的)再发一次。"
            ) from exc
        if exc.err_code == pdfium_c.FPDF_ERR_SECURITY:
            raise UnreadablePdf(
                "这份 PDF 用了我解不开的加密方式,打不开。请用户另存一份不加密的再发一次。"
            ) from exc
        # 格式坏、没传完、一页都没有:pdfium 给的都是 FPDF_ERR_FORMAT(实测,见
        # tests/pdf_samples.py),对用户也是同一件事——重发一份原件。
        raise UnreadablePdf(
            "这份 PDF 打不开:文件是坏的或者不完整(比如没传完),也可能里面一页都没有。"
            "请用户把原件重新发一次。"
        ) from exc
    except OSError as exc:
        # 只带异常类名,不带 str(exc):后者带着服务器上的绝对路径,而这句话要进模型、落起居注。
        raise UnreadablePdf(f"这份文件从磁盘上读不出来({type(exc).__name__})。") from exc


def _png(width: int, height: int, stride: int, rgb: bytes) -> bytes:
    """把 3 通道 RGB 位图编成 PNG。**自己写,不拉 Pillow**(D1:十几行标准库)。

    每行前面一个过滤字节 0(不做行间预测):课件大半是白底,zlib 对整片 0xff 本来就压得
    很好,实测一页 A4 讲义 208 KB;做 Up/Paeth 预测要逐像素跑 Python 循环,一页慢到秒级。
    `stride` 可能比 `width * 3` 大(pdfium 按 4 字节对齐),每行只取前 `width * 3` 个字节。
    """
    row = width * 3
    raw = b"".join(b"\x00" + rgb[y * stride : y * stride + row] for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8 位、真彩色、不隔行
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )
