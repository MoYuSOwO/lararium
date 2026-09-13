"""按 id 搜内容(M6-6e)的判定那一层:一页转写出来的文字里有没有这个短语、在哪;
一份文件哪些页搜得到、哪些页搜不到。**人话里最要紧的那一句(哪几页搜不到)也在这里**,
工具那一层(`tools.search_in_files`)管 id、过刀、拉闩、拼版。

★ **空白不算**:两边都把空白整个去掉再比。转写出来的文字里一句话经常被换行拆开
(「矩阵\\n乘法」),而用户和模型嘴里的是「矩阵乘法」;反过来模型抄查询时也会多带一个空格。
**折成一个空格不够**——「矩阵 乘法」和「矩阵乘法」照样对不上。去掉之后的代价是英文词的边界
没了(「the rapist」搜得到「therapist」),对"找到是哪一页"这件事是多给、不是漏给,
而片段就在旁边,模型一眼看得出是不是。表格竖线、LaTeX 拆开的不管:那些不是"同一句话
被排版拆开",规整它们等于猜——搜不到就换短一点的词。

★ **片段取自原文**:匹配在去掉空白的那份上做,落点按下标映射回原文,再从原文取窗口
(`docstore.excerpt`)。给模型看的是页面上真实的那几个字,不是我们规整过的样子。

**"怎么算命中"复用 `docstore.find_all`**(大小写不敏感、不重叠),不另写一份:
搜笔记和搜文件对大小写的理解漂开的那天,症状是同一个词在一处搜得到、另一处搜不到。

**不建索引**:几份 PDF、每份最多 200 页(`pdftext.MAX_CONVERTED_PAGES`)的文字,
Python 扫一遍的量级见 REVIEW M6-6e;缓存一直在被后台写,索引还得跟着它失效。
"""

import re
from dataclasses import dataclass

from lararium.docstore import find_all
from lararium.steward.pdftext import PageText

_SPACE = re.compile(r"\s+")
# 和上面同一个 `\s` 的补集:去掉空白之后的第 i 个字,就是原文里第 i 个匹配它的位置。
# 两个判据写成两套(比如一边 `str.isspace`、一边正则)的那天,映射就会错位一两个字。
_INK = re.compile(r"\S")
# 一份里搜不到的页,列出来最多列几段;再多就说"等",**总页数照样说**。
_RANGES_SHOWN = 6


def squeeze(text: str) -> str:
    """去掉全部空白。查询和原文都过这一刀。"""
    return _SPACE.sub("", text)


@dataclass(frozen=True)
class PageMatch:
    """一页里命中了几处,第一处在**原文**里从哪儿开始、跨多长(跨过的空白算在里面)。"""

    count: int
    at: int
    length: int


def match_page(text: str, needle: str) -> PageMatch | None:
    """`needle` 必须已经 `squeeze` 过。没命中回 None。

    下标映射只在命中的页上算:大多数页一处都不中,为它们建映射是白花。
    `find_all` 在 casefold 过的串上算下标,极少数字符折叠后长度会变(`ß` → `ss`),
    那时落点会偏一两个字——**只偏窗口,不偏"命中与否"**,和 `find_all` 自己说的同一件事;
    这里夹一下,别让它越界。
    """
    squeezed = squeeze(text)
    spots = find_all(squeezed, needle, limit=len(squeezed))
    if not spots:
        return None
    ink = [m.start() for m in _INK.finditer(text)]
    first = min(spots[0], len(ink) - 1)
    last = min(spots[0] + len(needle) - 1, len(ink) - 1)
    return PageMatch(count=len(spots), at=ink[first], length=ink[last] + 1 - ink[first])


def page_ranges(pages: list[int]) -> str:
    """`[12, 13, …, 30, 33]` → 「12-30、33」。段数多了截断,**截了要说**。"""
    spans: list[tuple[int, int]] = []
    for page in pages:
        if spans and page == spans[-1][1] + 1:
            spans[-1] = (spans[-1][0], page)
        else:
            spans.append((page, page))
    words = [f"{a}-{b}" if a != b else f"{a}" for a, b in spans[:_RANGES_SHOWN]]
    more = "等" if len(spans) > _RANGES_SHOWN else ""
    return "、".join(words) + more


def coverage_note(short: str, states: list[PageText], matched: int, *, converting: bool) -> str:
    """一份文件"搜到了什么、哪几页根本没搜"的那一行。

    ★ **这是这个工具唯一会静默错的地方**:搜不到和没写过长得一模一样。所以还没转完 /
    转失败 / 超出只转前几页的上限,**命中与否都要说**,而且各说各的——三种的出路不一样:
    没转完的等一会儿再搜;转失败的只能 read_pdf 看图;超上限的这份后面根本不会有文字。

    `converting=False` 是视觉关着(转换器不接):没转的页**永远**不会转,那就不许说
    "还没转完",那句话在让人等一件不会发生的事。
    """
    total = len(states)
    pending = [i for i, s in enumerate(states, 1) if s.state == "pending"]
    failed = [i for i, s in enumerate(states, 1) if s.state == "failed"]
    beyond = [i for i, s in enumerate(states, 1) if s.state == "beyond"]
    found = f"命中 {matched} 页" if matched else "没命中"
    if not (pending or failed or beyond):
        return f"id {short}:共 {total} 页,全都转好了,{found}。"
    done = total - len(pending) - len(failed) - len(beyond)
    parts = [f"id {short}:共 {total} 页,转好了 {done} 页,{found}"]
    if pending and converting:
        parts.append(
            f"第 {page_ranges(pending)} 页还没转完({len(pending)} 页),那几页没搜到不代表没有,"
            "过一会儿再搜"
        )
    elif pending:
        parts.append(
            f"第 {page_ranges(pending)} 页还没转成文字({len(pending)} 页)——现在的模型看不了图,"
            "不会去转,那几页搜不到"
        )
    if failed:
        parts.append(
            f"第 {page_ranges(failed)} 页转文字失败了({len(failed)} 页),那几页搜不到,"
            "要看就用 read_pdf 看图"
        )
    if beyond:
        parts.append(
            f"这份只转了前 {states[0].limit} 页,第 {page_ranges(beyond)} 页没有转出来的文字,搜不到"
        )
    return ";".join(parts) + "。"
