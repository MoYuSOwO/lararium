"""M6-6b:PDF 光栅化那一层(`steward/pdf.py`)——打开、数页、把一页画成 PNG。

这里只测适配层本身;`read_pdf` 这个工具怎么说话、和读图共用额度、拉不可信闩,在
`test_tools.py` / `test_loop.py`。坏 PDF 的样本在 `tests/pdf_samples.py`,每一种都是
真打过 pdfium 的形状(见那个文件的 docstring)。
"""

import ast
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from tests import pdf_samples as samples

from lararium.steward.pdf import RENDER_LONG_SIDE, UnreadablePdf, page_count, render_page


def put(tmp_path: Path, blob: bytes, name: str = "x.pdf") -> Path:
    path = tmp_path / name
    path.write_bytes(blob)
    return path


def pixels(png: bytes) -> bytes:
    """把我们自己编的 PNG 解回原始扫描行(一个 IDAT 块、过滤字节 0)。"""
    at = png.index(b"IDAT")
    length = int.from_bytes(png[at - 4 : at], "big")
    return zlib.decompress(png[at + 4 : at + 4 + length])


def test_page_count_is_the_number_of_pages(tmp_path):
    assert page_count(put(tmp_path, samples.pdf(3))) == 3


@pytest.mark.parametrize(
    ("size", "expected"),
    [(samples.A4, (1131, 1600)), (samples.SLIDE, (1600, 900))],
    ids=["A4 讲义", "16:9 幻灯片"],
)
def test_a_page_is_drawn_with_its_long_side_fixed(tmp_path, size, expected):
    """★ 分辨率按**长边**定,不按 DPI:一页吃多少 token 不随纸张大小涨。

    A4 讲义 1131x1600(约 137 DPI,7pt 脚注的汉字约 15 像素高,读得清);
    16:9 幻灯片 1600x900。数和理由见 `RENDER_LONG_SIDE` 上方。
    """
    png = render_page(put(tmp_path, samples.pdf(1, size)), 1)

    assert samples.png_size(png) == expected
    assert max(expected) == RENDER_LONG_SIDE


def test_the_page_number_counts_from_one(tmp_path):
    """★ 页码从 1 数,和用户说的「第 3 页」一致——差一格就是永远给错一页,而且没有任何报错。

    判据:三页文档的第 1 页,和只有「page 1」那一页的文档画出来**逐字节相同**;
    最后一页(第 3 页)画得出来,不越界。
    """
    three = put(tmp_path, samples.pdf(3), "three.pdf")
    one = put(tmp_path, samples.pdf(1), "one.pdf")

    assert render_page(three, 1) == render_page(one, 1)
    assert render_page(three, 3) != render_page(three, 1)


def test_the_drawn_page_is_not_blank(tmp_path):
    """画出来的那张图上真的有字——不是一张白纸(白底是 0xff)。"""
    raw = pixels(render_page(put(tmp_path, samples.pdf(1)), 1))

    assert set(raw) - {0, 0xFF}, "整页只有白色和过滤字节:什么都没画上去"


@pytest.mark.parametrize(
    ("blob", "words"),
    [
        (samples.truncated(), "坏"),
        (samples.zero_pages(), "坏"),
        (b"", "坏"),
        (b"%PDF-1.7 this is not really a pdf", "坏"),
        (samples.encrypted(), "密码"),
    ],
    ids=["截断", "零页", "空文件", "头对内容错", "加密"],
)
def test_a_broken_pdf_raises_one_named_error_with_words(tmp_path, blob, words):
    """坏 PDF 在这一层抛**一种**有名字的异常,消息就是给模型的那句人话(同 `WebSearchError`)。

    库自己的 `PdfiumError` 不许漏出这一层(D2:第三方的语义关在隔离盒里)。
    """
    path = put(tmp_path, blob)

    with pytest.raises(UnreadablePdf) as caught:
        page_count(path)
    assert words in str(caught.value)
    with pytest.raises(UnreadablePdf):
        render_page(path, 1)


def test_a_page_that_will_not_load_says_which_page_and_the_total(tmp_path):
    """页树说 3 页、第 2 页读不出来:说清是**哪一页**坏了、一共几页——别的页照样能读。"""
    path = put(tmp_path, samples.lying_count())

    assert page_count(path) == 3
    assert samples.png_size(render_page(path, 1))
    with pytest.raises(UnreadablePdf) as caught:
        render_page(path, 2)
    assert "第 2 页" in str(caught.value) and "共 3 页" in str(caught.value)


def test_a_file_that_is_gone_is_a_sentence_too(tmp_path):
    with pytest.raises(UnreadablePdf):
        page_count(tmp_path / "gone.pdf")


def test_concurrent_renders_do_not_bring_the_process_down(tmp_path):
    """★ **PDFium 不是线程安全的,而工具调用是并发的**(M5-8:一条 assistant 消息里的多个
    工具调用在线程池里同时跑)。

    实测(2026-09-13,不加锁 8 线程 x 400 次渲染,跑 3 次):**3 次进程全部当场死掉**
    ——退出码 133 / 139 / 133(SIGTRAP / SIGSEGV)。不是异常,是整个服务进程没了。
    加一把进程级的锁之后同样 3 x 400 次,0 错。这条测试红的样子是 pytest 进程崩掉。
    """
    paths = [
        put(tmp_path, samples.pdf(3, size), f"{i}.pdf")
        for i, size in enumerate((samples.A4, samples.SLIDE))
    ]
    errors: list[BaseException] = []
    lock = threading.Lock()

    def once(i: int) -> None:
        try:
            render_page(paths[i % 2], i % 3 + 1)
            page_count(paths[(i + 1) % 2])
        except BaseException as exc:  # 收集起来在主线程断言,别让线程池吞掉
            with lock:
                errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(once, range(160)))

    assert errors == []


def test_pypdfium2_is_imported_in_exactly_one_module():
    """D2:第三方库只准出现在一个适配模块里——库升级改 API 时只看这一个文件。"""
    importers = set()
    for root in (Path("src"), Path("bundles")):
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                if any(name.split(".")[0] == "pypdfium2" for name in names):
                    importers.add(str(path))
    assert importers == {"src/lararium/steward/pdf.py"}
