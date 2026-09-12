"""学习 bundle —— **一门课一个笔记本**,模型手里只有一个课程名。

用户定的形状(任务书里有他的原话):

> 「作业考试也是待办啊,为什么不能用待办系统来装呢?我要的是类似
> **一个课程建立一个目录 + 知识库**这种。」
> 「**一门课给一个笔记本**应该大概也够了吧?」

**一门课一个笔记本,不是一门课下面多份笔记。** 理由不是"够用",是**多份笔记会逼模型编
文件名**——它会产出「第一章笔记」「chapter1」「线代笔记1」这种互不一致的东西。
**别让模型发明一个维度**(和做菜那个被否掉的 category 是同一个毛病)。

**没有 `write_note`(整篇覆盖)。** 一学期的笔记整篇覆盖太危险;`append` + `replace`
够用,真要重构**用户直接编辑那个 `.md`**——那正是选文件而不是 SQLite 的理由。
**代价**:本子会长。所以 `read_note` **必须分页**,而 `search_notes` 从"锦上添花"变成
**主要入口**——找一段的正常姿势是搜,不是从头读。

**路径不出现在接口上**:模型手里只有课程名,只有 `CourseStore.locate` 知道目录在哪
(`test_no_tool_signature_has_a_path_parameter` 机械地钉着这一条,同 M6-5、同 M5-4)。

**课件那一半是 M6-6b**(`add_file` / `list_materials`,加上 Steward 侧的 `read_pdf`)。
这一轮**一个字都没碰**:没有实现的读取器不许出现在工具清单里——**能力边界写在清单里,
比写在错误信息里强**。而落点的单位已经是**整个课程目录**,所以那一轮往里加
`materials/` 时,这七个签名一个字都不用动,改名和删除也天然把课件一起搬走。
**但模型看得见的话里(docstring、回话)这一轮一个「课件」都不提**:清单里没有课件工具,
说"笔记和课件都跟过来了"就是在描述一个它摸不到的东西——那一轮加工具时一起改,
反正同一次前缀重建。

★ **换行不用管**(M6-5 探针量过,真实字符串见 REVIEW):工具结果在**调用它的那一轮是
逐字节原样进模型的**,组装器的折行与 200 字截断只作用在**历史轮**的 L0 回放。所以
`read_note` 原样返回;反过来,**一行一条**的输出(课程列表、命中片段、删除理由)里嵌的
文本必须自己折行(`docstore.one_line`)——当轮没有任何上游会替我们折。
"""

import functools
from collections.abc import Callable
from pathlib import Path

from fastmcp import FastMCP

from bundles.courses.store import (
    NOTE_PAGE_CHARS,
    CourseSpot,
    CourseStore,
    UnreadableNote,
    page_index,
    paginate,
)
from bundles.runtime import BundleRuntime
from lararium.docstore import (
    CLOSE,
    EXCERPT_RADIUS,
    MAX_INLINE_CHARS,
    OPEN,
    SNIPPET_RADIUS,
    clip,
    excerpt,
    find_all,
    one_line,
    page_of,
    replace_once,
    scan,
)

# 一页多少门课。口径照 `list_recipes`:一次工具调用不许顶穿 L0。
LIST_PER_PAGE = 30
# 命中一行要带片段(约 80 字),所以页比列表小(照 `search_recipes`)。
SEARCH_PER_PAGE = 10
# 一本笔记里最多取多少处命中。**上限要有,而且超了要说出口**——静默截断读起来和
# "就这些"一模一样(M4-3)。
NOTE_HIT_CAP = 50


def _inline(text: str) -> str:
    """把一段文本嵌进一行里:先折行、再封顶,截断看得见(顺序照 M6-5 的 `_inline`)。"""
    return clip(one_line(text), MAX_INLINE_CHARS)


def _speak_errors(fn: Callable[..., str]) -> Callable[..., str]:
    """把文件系统那一类失败翻成人话,**一处包住全部七个工具**(E2 + G8)。

    E2:模型可调的工具不许把异常抛给模型——抛了整轮炸掉,用户看到的是助手死掉。
    而"每个工具自己 try 一遍"是**七处守同一条不变量**,漏一处就是无声的;所以包在
    构造工具列表那一步,新加的工具(M6-6b 那两个)自动在里面。

    **为什么这一份没有跟着搬进共用层**:它翻的是给**这个** bundle 说的那句话,
    和 `name_error` 同一个判据——判定/机制共用,**人话不共用**。而它翻的东西
    (`OSError` → 一句话)漂移面是一句话,不是一条安全性质。

    `functools.wraps` 保签名与 docstring,所以工具 schema 一个字节不变(A1);
    `inspect.signature` 会跟着 `__wrapped__` 取到原签名,机械检查照样看得见真参数。
    """

    @functools.wraps(fn)
    def speaking(*args: object, **kwargs: object) -> str:
        try:
            return fn(*args, **kwargs)
        except UnreadableNote as exc:
            return str(exc)
        except OSError as exc:
            # 磁盘满、权限、名字太长被文件系统拒……都归这里。**说清没成**,
            # 别回一句"好了"——那是 M5-20 那个失效形态:用户以为办了,其实没办。
            return f"这一步没办成(文件读写失败:{exc})。稍后再试,或者看一眼磁盘。"

    return speaking


def _missing(spot: CourseSpot) -> str:
    """「没有这门课」那句话。三个工具共用一份措辞,别让它们各自漂。"""
    return (
        f"没有「{spot.name}」这门课。list_courses 看看有哪些;"
        f'第一次记笔记用 append_to_note("{spot.name}", "……"),没有的课会顺手建出来。'
    )


def _tool_functions(store: CourseStore) -> list[Callable]:
    """七个工具。**顺序即冻结顺序**(工具 schema 是前缀第 0 层,DESIGN §4),
    manifest.yaml 的 tools 顺序是设计时的权威,`test_manifest_declares_...` 逐名对齐。
    """

    def list_courses(page: int = 1, include_deleted: bool = False) -> str:
        """有哪些课。一页一页给(page 从 1 起),门数在第一行。

        带 include_deleted=True 会把删掉的那些也列出来、标「已删」和当初写的原因。
        """
        live = store.names()
        rows = [f"- {name}" for name in live]
        gone = store.deleted() if include_deleted else []
        for name, reason, _ in gone:
            tail = f":{OPEN}{_inline(reason)}{CLOSE}" if reason.strip() else ""
            rows.append(f"- {name}(已删{tail})")
        if not rows:
            return (
                '还没有哪门课的笔记。第一次记就 append_to_note("线性代数", "……")'
                "——没有的课会顺手建出来,而回话里会说一声。"
            )
        shown, page, pages = page_of(rows, page, LIST_PER_PAGE)
        head = f"有 {len(live)} 门课"
        if include_deleted:
            head += f"、回收站里 {len(gone)} 门"
        return "\n".join([f"{head},第 {page}/{pages} 页:", *shown])

    def read_note(course: str, page: int = 1) -> str:
        """读一门课的笔记本,**原样给你**——换行、表格、公式,文件里是什么样就是什么样。

        一本装不下就分页(page 从 1 起),第一行会说这是第几页、一共几页。
        **本子长的时候别从头翻**:想找某一段用 search_notes,它会告诉你在第几页。
        """
        spot = store.locate(course)
        if spot.folder is None or spot.notes is None:
            return spot.error
        if not spot.folder.is_dir():
            return _missing(spot)
        text = store.read(spot.notes, label=spot.name)
        if not text.strip():
            return (
                f"「{spot.name}」这门课的笔记本还是空的,里面什么都没写。记第一段用 append_to_note。"
            )
        pages = paginate(text, NOTE_PAGE_CHARS)
        if len(pages) == 1:
            # 一页装得下就**什么都不加**:硬口径二是"写进去再读回来逐字节一致",
            # 而这是绝大多数本子的状态。抬头那一行只在真的分了页时才出现。
            return text
        shown, page, total = page_of(pages, page, 1)
        return (
            f"「{spot.name}」的笔记,第 {page}/{total} 页(整本 {len(text)} 字)。"
            f"找某一段用 search_notes 更快,它会说在第几页:\n{shown[0]}"
        )

    def append_to_note(course: str, text: str) -> str:
        """在一门课的笔记本后面追加一段——上完课记一笔,最常用的就是这个。

        原有内容一个字不动,text 接在末尾(需要的话自动补一个换行)。
        **这门课不存在时会顺手新建**,而回话里会说一声——名字打错了看到那句话就能
        rename_course 改回来。
        """
        spot = store.locate(course)
        if spot.folder is None or spot.notes is None:
            return spot.error
        if not text.strip():
            return f"text 是空的,「{spot.name}」一个字没动(也没新建)。要记就给一句话。"
        existed = spot.folder.is_dir()
        old = store.read(spot.notes, label=spot.name) if existed else ""
        separator = "" if (not old or old.endswith("\n")) else "\n"
        store.save(spot.notes, old + separator + text)
        if existed:
            return (
                f"记在「{spot.name}」的笔记后面了:{OPEN}{_inline(text)}{CLOSE}。"
                f"原来那些一个字没动。"
            )
        # ★ 打错课程名得能救:「线性代数」打成「线性待数」就是一门新课,而且笔记会散在
        # 两个地方。加一个显式 create_course **挡不住**(打错那次照样会创建),
        # **说一声才挡得住**——所以这句话是这条缺口的唯一防线。
        return (
            f"「{spot.name}」这门课之前没有,给你新建了,第一段记进去了"
            f"({len(text)} 字)。要是课程名打错了,rename_course 能改过来。"
        )

    def replace_in_note(course: str, old: str, new: str) -> str:
        """把一门课笔记里的 old 换成 new(new 给空串就是删掉那一段)。

        **old 必须在笔记里恰好出现一次**:一次都没有、或者出现好几次,都**不改**并告诉你
        为什么。所以 old 要贴原文、必要时多带前后一两行让它唯一(search_notes 能先看一眼
        有几处)。改完会把改动前后那一小段回给你,自己核对一眼。
        """
        spot = store.locate(course)
        if spot.folder is None or spot.notes is None:
            return spot.error
        if not old:
            return f"old 是空的,没法定位改哪里,「{spot.name}」一个字没动。把要换掉的原文贴进 old。"
        if not spot.folder.is_dir():
            return _missing(spot)
        text = store.read(spot.notes, label=spot.name)
        got = replace_once(text, old, new)
        if got.count == 0:
            return (
                f"「{spot.name}」的笔记里没找到这段,一个字没动:{OPEN}{_inline(old)}{CLOSE}。"
                f"可能这处已经改过了,也可能记错了原文——先 search_notes 搜一下再来。"
            )
        if got.text is None:
            # 多次命中**绝不允许"改第一处"**:那是在猜用户指的是哪一处,
            # 猜错了就是静默改坏一学期的笔记(而回话照样说"改好了")。
            return (
                f"这段在「{spot.name}」的笔记里出现了 {got.count} 次,一个字没动——"
                f"不知道你指的是哪一处。把 old 给长一点(多带前后一两行)让它只剩一处。"
            )
        store.save(spot.notes, got.text)
        return "\n".join(
            [
                f"改好了「{spot.name}」的笔记。自己核对一下:",
                f"改前:{excerpt(text, got.at, len(old), EXCERPT_RADIUS)}",
                f"改后:{excerpt(got.text, got.at, len(new), EXCERPT_RADIUS)}",
            ]
        )

    def search_notes(query: str, course: str | None = None, page: int = 1) -> str:
        """搜笔记。**找一段的正常姿势是搜,不是从头读**——本子会长。

        不给 course 就搜所有课:回的是**哪门课**命中、在第几页、加一小段片段。
        给了 course 就在那一本里搜:回的是**在哪几处**、各在第几页。
        两个问题不一样,所以答案的形状也不一样。拿到页码就可以 read_note(course, page)。
        """
        needle = query.strip()
        if not needle:
            return '没说搜什么。给一个词,比如 search_notes("行列式")。'
        shown_query = _inline(needle)
        # course 给了空串 / 全空格也当"没给"(全搜):模型传一个 "" 多半是想说"都搜",
        # 为它回一句「课程名是空的」是在惩罚一个合理的调用。
        asked = (course or "").strip()
        if asked:
            return _search_one(store, store.locate(asked), needle, shown_query, page)
        return _search_all(store, needle, shown_query, page)

    def rename_course(old: str, new: str) -> str:
        """给一门课改名——课程名打错了(「线性待数」)用它救。笔记一个字不动。

        new 已经有同名的课时**不改**:把两门课的笔记合到一起是静默的破坏,
        得你自己决定留哪份。
        """
        src = store.locate(old)
        if src.folder is None:
            return src.error
        dst = store.locate(new)
        if dst.folder is None:
            return dst.error
        if src.name == dst.name:
            return f"新名字和「{src.name}」一样,没改。"
        if not src.folder.is_dir():
            return f"没有「{src.name}」这门课,没改。list_courses 看看有哪些。"
        if dst.folder.is_dir():
            # ★ **合并比重名更坏:重名你看得见,合并是静默的。**
            return (
                f"已经有「{dst.name}」这门课了,没改——改名会把两门课的笔记合到一起,"
                f"而那是静默的破坏。两边都 read_note 看一眼,自己决定留哪份。"
            )
        store.rename(src.folder, dst.folder)
        return f"「{src.name}」改名成「{dst.name}」了。整个课程目录一起搬的,内容一个字节没动。"

    def delete_course(course: str, reason: str = "", undo: bool = False) -> str:
        """删掉一门课。**删的时候 reason 必填**:说清为什么删,三个月后回头看才看得懂
        (不写就不删,会让你补一句)。

        **不是真删**:整个课程目录搬到一边存着,
        list_courses(include_deleted=True) 看得到。删错了就同一个课程名再调一次、
        带 undo=True,原样回来(一个字节不变)——**撤回不用给 reason**,拿回来就是拿回来。
        """
        spot = store.locate(course)
        if spot.folder is None:
            return spot.error
        trashed = store.latest_trashed(spot.name)

        if undo:
            if trashed is None:
                return f"「{spot.name}」没删过,不用恢复。list_courses 看看现在有哪些。"
            if spot.folder.is_dir():
                return (
                    f"已经有一门课占着「{spot.name}」这个名字了,没恢复——"
                    f"盖上去会把现在那份弄丢。先给现在这门改个名(rename_course)再来。"
                )
            store.out_of_trash(trashed, spot.folder)
            return f"「{spot.name}」拿回来了,一个字节没变。"

        if not reason.strip():
            return (
                f"删「{spot.name}」得说清为什么(reason),所以没删。"
                f"「这学期修完了」和「记错课程名了」不是一回事,三个月后回头看得出差别。"
            )
        if not spot.folder.is_dir():
            if trashed is not None:
                return (
                    f"「{spot.name}」已经删过了,现在没有这门课。要拿回来就再调一次、带 undo=True。"
                )
            return f"没有「{spot.name}」这门课,什么都没动。list_courses 看看有哪些。"
        if store.into_trash(spot.folder, spot.name, reason) is None:
            return (
                f"「{spot.name}」没删——回收站的落点不在课程目录里面(有人把 "
                f"{store.root.name}/.trash 换成了指到别处的链接?)。先看一眼那个目录。"
            )
        return (
            f"删了「{spot.name}」,原因{OPEN}{_inline(reason)}{CLOSE}。"
            f"整个课程目录搬到一边存着、一个字节没销毁,"
            f"删错的话再调一次、带 undo=True 就能原样拿回来。"
        )

    return [
        _speak_errors(fn)
        for fn in (
            list_courses,
            read_note,
            append_to_note,
            replace_in_note,
            search_notes,
            rename_course,
            delete_course,
        )
    ]


def _search_one(
    store: CourseStore, spot: CourseSpot, needle: str, shown_query: str, page: int
) -> str:
    """在**一本**笔记里搜:问的是"在哪几处",所以一处一行,各自带页码。

    ★ 一门课一个笔记本,所以"这本里有几处"是个真问题——只给第一处的话,
    一本二十页的笔记等于搜不到。而课程名**不用**每行再印一遍:它在抬头里,
    每行重复是噪声(和跨课搜索恰好相反——那边不印课程名就等于没答)。
    """
    if spot.folder is None or spot.notes is None:
        return spot.error
    if not spot.folder.is_dir():
        return _missing(spot)
    text = store.read(spot.notes, label=spot.name)
    total = text.casefold().count(needle.casefold())
    spots = find_all(text, needle, limit=NOTE_HIT_CAP)
    if not spots:
        return (
            f"「{spot.name}」的笔记里没提到{OPEN}{shown_query}{CLOSE}。"
            f"换个说法再试(搜的是笔记原文),或者 read_note 从头看一眼。"
        )
    pages = paginate(text, NOTE_PAGE_CHARS)
    rows, page, page_total = page_of(spots, page, SEARCH_PER_PAGE)
    capped = f",只列前 {NOTE_HIT_CAP} 处(把搜索词说具体一点)" if total > len(spots) else ""
    lines = [
        f"{OPEN}{shown_query}{CLOSE}在「{spot.name}」的笔记里命中 {total} 处{capped},"
        f"第 {page}/{page_total} 页(只给片段,要读整段用 read_note):"
    ]
    for at in rows:
        fragment = excerpt(text, at, len(needle), SNIPPET_RADIUS)
        lines.append(f"- 第 {page_index(pages, at)} 页 {OPEN}{fragment}{CLOSE}")
    return "\n".join(lines)


def _search_all(store: CourseStore, needle: str, shown_query: str, page: int) -> str:
    """跨课搜:问的是"哪门课",所以**课程名必须出现在每一行**。

    ★ 不然模型拿着一段片段不知道它在哪门课里,接不上 read_note。顺序由
    `docstore.scan` **写死**(名字命中在前,同一类里按课程名排),不跟着 `iterdir`
    的顺序走——那个在不同机器上不一样,而"第 1/3 页"会因此指着不同的东西。
    """
    entries = store.entries()
    hits = scan(entries, needle)
    if not hits:
        return (
            f"没有哪门课的笔记提到{OPEN}{shown_query}{CLOSE}。换个说法再试(搜的是课程名和"
            f"笔记原文),或者 list_courses 看全部(一共 {len(entries)} 门)。"
        )
    by_name = dict(entries)
    rows, page, total = page_of(hits, page, SEARCH_PER_PAGE)
    lines = [
        f"{OPEN}{shown_query}{CLOSE}命中 {len(hits)} 门课,第 {page}/{total} 页"
        f"(只给片段,要读整段用 read_note):"
    ]
    for hit in rows:
        kind = "名字+内容" if hit.in_name and hit.in_text else ("名字" if hit.in_name else "内容")
        where = (
            f",第 {page_index(paginate(by_name[hit.name], NOTE_PAGE_CHARS), hit.at)} 页"
            if hit.in_text
            else ""
        )
        tail = f" {OPEN}{hit.snippet}{CLOSE}" if hit.snippet else ""
        lines.append(f"- {hit.name}({kind}命中{where}){tail}")
    return "\n".join(lines)


def build(data_dir: Path, *, timezone: str) -> BundleRuntime:
    """统一构造入口(bundle 契约)。工具顺序由 manifest.yaml 与测试钉死(前缀第 0 层)。

    **收 timezone**:回收站目录名里有时间戳(`<课程>-<时间戳>/`,M5-20 的形状),
    而 bundle 不许自己兜默认时区——那是组装根的事(照 finance 那条)。
    """
    store = CourseStore(Path(data_dir) / "courses", timezone=timezone)
    # 目录先建出来:用户要能在自己电脑上 cd 进去看自己的笔记、用编辑器直接改
    # (那正是选文件而不是 SQLite 的全部理由),空目录也得在。
    store.root.mkdir(parents=True, exist_ok=True)
    return BundleRuntime(tools=_tool_functions(store))


def create_server(data_dir: Path, *, timezone: str) -> FastMCP:
    """MCP 服务入口,和 finance / recipes / memory 同形状;生产单独容器时由它接管。"""
    mcp = FastMCP("courses")
    for fn in build(data_dir, timezone=timezone).tools:
        mcp.tool()(fn)
    return mcp


if __name__ == "__main__":
    import os

    # 独立容器形态下 bundle 自己读 env(进程边界就是它的配置入口)。这两个默认值必须和
    # `Settings` 里的一致——改一边就得改另一边。
    create_server(
        Path(os.environ.get("LARARIUM_DATA_DIR", "./data")),
        timezone=os.environ.get("LARARIUM_TIMEZONE", "Asia/Shanghai"),
    ).run()
