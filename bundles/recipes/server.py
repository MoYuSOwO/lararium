"""做菜 bundle —— 一份做法一个 markdown 文件,模型手里只有一个菜名。

**它是文档,不是表**(用户重新设计过,任务书里记着他的原话):

> 「做菜确实要标准啊,就是标准之后尝试,觉得可以就留着作为经验,然后不可以就提改进。
> **其实就是读文件**,这个没有太多格式化的东西。……**但是不要真的给文件系统,给几个
> 工具,不给读目录这种。**」

「做法表 + 尝试表」那一版是错的:一份做法没有稳定的形状,而「尝试」也不是一张独立的
日志——**做法本身在长**,觉得行就并进去当经验,不行就在旁边写改进。拆成两张表等于
强迫用户把一件事记在两个地方。分类也去掉了(「番茄炒鸡蛋属于什么类?」),扁平化之后
**名字天然唯一**。

**路径和分类都不出现在接口上**:八个工具的签名里只有菜名、翻页和搜索词,
`test_no_tool_signature_has_a_path_or_category_parameter` 机械地钉着这一条。落盘的事
只有 `store.RecipeStore.locate` 一个函数知道。

★ **换行这件事,量过再说的**(M6-5 探针,真实字符串见 REVIEW):规划里写的是
「我们的渲染器折行,所以做法整篇会变成一坨,这个 bundle 的全部内容都不可读」——
**实测不是这样**。工具结果在**调用它的那一轮是逐字节原样进模型的**(探针 1:
`read_recipe` 返回 278 字带换行的做法,HTTP body 里那条 `role=tool` 消息与原文
`==` 为 True);组装器的折行(`fold_text`)与 200 字截断只作用在**历史轮**的 L0 回放
(探针 2)。所以:

- `read_recipe` **什么都不做**,原样返回。把换行换成 `① ②` 之类反而会毁掉硬口径第一条
  (「读回来和写进去逐字节一致」)——那两条本来就是互相排斥的,而实测说明该保的是前者。
- 反过来,**一行一条**的输出(列表行、命中片段、删除理由)里嵌的文本必须自己折行:
  当轮那份没有任何上游会替我们折(`store.one_line`)。

**存进来的做法是用户让存的,不是"经过核实的"**:它可能是用户让 `web_fetch` 抓一份菜谱
存下来的,那一轮是脏的。`write_recipe` 写的是本 bundle 自己的库、不经门控——**这不是
漏洞**(门控管的是长期档案),但 docstring 要把这件事说清楚,而不是在这一步发明新守卫
(M5-11)。
"""

import functools
from collections.abc import Callable
from pathlib import Path

from fastmcp import FastMCP

from bundles.recipes.store import (
    CLOSE,
    EXCERPT_RADIUS,
    MAX_INLINE_CHARS,
    OPEN,
    RecipeStore,
    UnreadableRecipe,
    clip,
    excerpt,
    one_line,
    page_of,
    replace_once,
    scan,
)
from bundles.runtime import BundleRuntime

# 一页多少条。菜名一行最长 40 字,30 行约 1200 字,和一次检索同量级(照
# steward/tools.py 那几个上限的口径:一次工具调用不许顶穿 L0)。
LIST_PER_PAGE = 30
# 命中一行要带片段(约 80 字),所以页比列表小:10 行约 1000 字,同一个量级。
SEARCH_PER_PAGE = 10


def _inline(text: str) -> str:
    """把一段文本嵌进一行里:先折行、再封顶,截断看得见。顺序照 finance `_render_note`
    ——先折再截,别让空白吃掉预算。"""
    return clip(one_line(text), MAX_INLINE_CHARS)


def _speak_errors(fn: Callable[..., str]) -> Callable[..., str]:
    """把文件系统那一类失败翻成人话,**一处包住全部八个工具**(E2 + G8)。

    E2:模型可调的工具不许把异常抛给模型——抛了整轮炸掉,用户看到的是助手死掉。
    而"每个工具自己 try 一遍"是**八处守同一条不变量**,漏一处就是无声的;所以包在
    构造工具列表那一步,新加的工具自动在里面。

    `functools.wraps` 保签名与 docstring,所以工具 schema 一个字节不变(A1);
    `inspect.signature` 会跟着 `__wrapped__` 取到原签名,机械检查照样看得见真参数。
    """

    @functools.wraps(fn)
    def speaking(*args: object, **kwargs: object) -> str:
        try:
            return fn(*args, **kwargs)
        except UnreadableRecipe as exc:
            return str(exc)
        except OSError as exc:
            # 磁盘满、权限、名字太长被文件系统拒……都归这里。**说清没成**,
            # 别回一句"好了"——那是 M5-20 那个失效形态:用户以为办了,其实没办。
            return f"这一步没办成(文件读写失败:{exc})。稍后再试,或者看一眼磁盘。"

    return speaking


def _tool_functions(store: RecipeStore) -> list[Callable]:
    """八个工具。**顺序即冻结顺序**(工具 schema 是前缀第 0 层,DESIGN §4),
    manifest.yaml 的 tools 顺序是设计时的权威,`test_manifest_declares_...` 逐名对齐。
    """

    def list_recipes(page: int = 1, include_deleted: bool = False) -> str:
        """存了哪些菜。一页一页给(page 从 1 起),总数在第一行。

        带 include_deleted=True 会把删掉的那些也列出来、标「已删」和当初写的原因。
        """
        live = store.names()
        rows = [f"- {name}" for name in live]
        gone = store.deleted() if include_deleted else []
        for name, reason in gone:
            tail = f":{OPEN}{_inline(reason)}{CLOSE}" if reason.strip() else ""
            rows.append(f"- {name}(已删{tail})")
        if not rows:
            return (
                '还没存过做法。写一份用 write_recipe("番茄炒鸡蛋", "……")'
                "——正文就是普通的 markdown,想怎么写怎么写。"
            )
        shown, page, pages = page_of(rows, page, LIST_PER_PAGE)
        head = f"存了 {len(live)} 道菜"
        if include_deleted:
            head += f"、回收站里 {len(gone)} 道"
        return "\n".join([f"{head},第 {page}/{pages} 页:", *shown])

    def read_recipe(name: str) -> str:
        """读一份做法,**原样给你**——换行、表格、符号,文件里是什么样就是什么样。

        存进来的做法是用户让存的,**不是"经过核实的"**:有可能是从网上抓下来存的,
        照着做之前该自己过一眼。想改其中一处用 replace_in_recipe,补一句经验用
        append_to_recipe。
        """
        loc = store.locate(name)
        if loc.path is None:
            return loc.error
        text = store.read(loc.path)
        if text is None:
            return (
                f"没有「{loc.name}」这道菜。list_recipes 看看存了哪些;"
                f"要是名字打错了,rename_recipe 能改过来。"
            )
        if not text.strip():
            return f"「{loc.name}」这道菜的文件是空的,里面什么都没写。"
        return text

    def write_recipe(name: str, content: str) -> str:
        """写一份做法(整篇),content 是普通 markdown,随便写。

        **同名会整篇覆盖,旧的那版不留**——所以只改一处的时候用 replace_in_recipe、
        补一句经验用 append_to_recipe,别把整篇重打一遍。
        这道菜不存在时**会新建**,而且回话里会说"之前没有这道"。
        """
        loc = store.locate(name)
        if loc.path is None:
            return loc.error
        if not content.strip():
            # 整篇覆盖不留上一版,所以空 content 是**无声清掉一份做法**。
            # 而"想写空做法"这个意图不存在,所以拦下来不损失任何东西。
            return f"content 是空的,「{loc.name}」一个字没动——空的正文会把原来那版整篇清掉。"
        existed = loc.path.is_file()
        store.save(loc.path, content)
        if existed:
            return f"更新了「{loc.name}」({len(content)} 字)。整篇覆盖,原来那版没留。"
        # ★ 打错菜名得能救:「番茄炒鸡蛋」打成「番茄炒鸡旦」就是一道新菜。打错的那次
        # 照样会创建,**说一声才挡得住**——显式 create 参数挡不住(它会照填 True)。
        return (
            f"之前没有「{loc.name}」这道菜,给你新建了({len(content)} 字)。"
            f"要是名字打错了,rename_recipe 能改过来。"
        )

    def append_to_recipe(name: str, text: str) -> str:
        """在一份做法后面追加一段——做完一次菜写一句经验,最常用的就是这个。

        原有内容一个字不动,text 接在末尾(需要的话自动补一个换行)。
        """
        loc = store.locate(name)
        if loc.path is None:
            return loc.error
        if not text.strip():
            return f"text 是空的,「{loc.name}」一个字没动。要追加就给一句话。"
        old = store.read(loc.path)
        if old is None:
            # **隐式创建只留 write_recipe 一条路。** 两条路就是两次"打错字创建新菜",
            # 而追加那条路上连"给你新建了"这句话都不好说(用户以为是在往老的后面加)。
            return (
                f'没有「{loc.name}」这道菜,没法追加。要新建就用 write_recipe("{loc.name}", "……")。'
            )
        separator = "" if old.endswith("\n") else "\n"
        store.save(loc.path, old + separator + text)
        return f"记在「{loc.name}」后面了:{OPEN}{_inline(text)}{CLOSE}。原来那些一个字没动。"

    def replace_in_recipe(name: str, old: str, new: str) -> str:
        """把一份做法里的 old 换成 new(new 给空串就是删掉那一段)。

        **old 必须在文件里恰好出现一次**:一次都没有、或者出现好几次,都**不改**并
        告诉你为什么。所以 old 要贴原文、必要时多带前后一两行让它唯一。
        改完会把改动前后那一小段回给你,自己核对一眼。
        """
        loc = store.locate(name)
        if loc.path is None:
            return loc.error
        if not old:
            return f"old 是空的,没法定位改哪里,「{loc.name}」一个字没动。把要换掉的原文贴进 old。"
        text = store.read(loc.path)
        if text is None:
            return f"没有「{loc.name}」这道菜,没改。list_recipes 看看存了哪些。"
        got = replace_once(text, old, new)
        if got.count == 0:
            return (
                f"「{loc.name}」里没找到这段,一个字没动:{OPEN}{_inline(old)}{CLOSE}。"
                f"可能这处已经改过了,也可能记错了原文——先 read_recipe 看一眼再来。"
            )
        if got.text is None:
            # 多次命中**绝不允许"改第一处"**:那是在猜用户指的是哪一处,
            # 猜错了就是静默改坏一份做法(而回话照样说"改好了")。
            return (
                f"这段在「{loc.name}」里出现了 {got.count} 次,一个字没动——"
                f"不知道你指的是哪一处。把 old 给长一点(多带前后一两行)让它只剩一处。"
            )
        store.save(loc.path, got.text)
        return "\n".join(
            [
                f"改好了「{loc.name}」。自己核对一下:",
                f"改前:{excerpt(text, got.at, len(old), EXCERPT_RADIUS)}",
                f"改后:{excerpt(got.text, got.at, len(new), EXCERPT_RADIUS)}",
            ]
        )

    def search_recipes(query: str, page: int = 1) -> str:
        """搜菜名**和**正文:「我之前哪道菜写了要少放酱油」这种问题用它。

        回的是菜名 + 命中处附近的一小段,并标出命中的是名字还是内容(名字命中通常更强)。
        **只给片段,要全文用 read_recipe。**
        """
        needle = query.strip()
        if not needle:
            return '没说搜什么。给一个词,比如 search_recipes("酱油")。'
        hits = scan(store.entries(), needle)
        shown = _inline(needle)
        if not hits:
            return (
                f"没有哪道菜提到「{shown}」。换个说法再试(搜的是菜名和正文的原文),"
                f"或者 list_recipes 看全部(一共 {len(store.names())} 道)。"
            )
        rows, page, pages = page_of(hits, page, SEARCH_PER_PAGE)
        lines = [
            f"「{shown}」命中 {len(hits)} 道菜,第 {page}/{pages} 页(只给片段,要全文用 read_recipe):"
        ]
        for hit in rows:
            kind = (
                "名字+内容" if hit.in_name and hit.in_text else ("名字" if hit.in_name else "内容")
            )
            tail = f" {OPEN}{hit.snippet}{CLOSE}" if hit.snippet else ""
            lines.append(f"- {hit.name}({kind}命中){tail}")
        return "\n".join(lines)

    def rename_recipe(old: str, new: str) -> str:
        """给一道菜改名——菜名打错了(「番茄炒鸡旦」)用它救。正文一个字不动。

        new 已经有同名的菜时**不改**:合并两份做法是静默的破坏,得你自己决定留哪份。
        """
        src = store.locate(old)
        if src.path is None:
            return src.error
        dst = store.locate(new)
        if dst.path is None:
            return dst.error
        if src.name == dst.name:
            return f"新名字和「{src.name}」一样,没改。"
        if not src.path.is_file():
            return f"没有「{src.name}」这道菜,没改。list_recipes 看看存了哪些。"
        if dst.path.is_file():
            return (
                f"已经有「{dst.name}」这道菜了,没改——改名会把两份做法合到一起,"
                f"而那是静默的破坏。两边都 read_recipe 看一眼,自己决定留哪份。"
            )
        store.move(src.path, dst.path)
        return f"「{src.name}」改名成「{dst.name}」了,正文一个字没动。"

    def delete_recipe(name: str, reason: str, undo: bool = False) -> str:
        """删掉一道菜。**reason 必填**:说清为什么删,三个月后回头看才看得懂。

        **不是真删**:文件搬到一边存着,list_recipes(include_deleted=True) 看得到。
        删错了就同一个菜名再调一次、带 undo=True,原样回来(内容逐字节不变)——
        撤回的时候 reason 就写一句为什么要拿回来。
        """
        loc = store.locate(name)
        if loc.path is None or loc.trash is None:
            return loc.error
        live, trashed = loc.path, loc.trash

        if undo:
            if not trashed.is_file():
                return f"「{loc.name}」没删过,不用恢复。list_recipes 看看现在存了哪些。"
            if live.is_file():
                return (
                    f"已经有一道菜占着「{loc.name}」这个名字了,没恢复——"
                    f"盖上去会把现在那份弄丢。先给现在这份改个名(rename_recipe)再来。"
                )
            store.move(trashed, live)
            store.drop_reason(trashed)
            return f"「{loc.name}」拿回来了,内容一个字没变。"

        if not reason.strip():
            return (
                f"删「{loc.name}」得说清为什么(reason),所以没删。"
                f"「做出来太咸,不想再做」和「记错名字了」不是一回事,三个月后回头看得出差别。"
            )
        if not live.is_file():
            if trashed.is_file():
                return f"「{loc.name}」已经删过了,菜谱里没有它。要拿回来就再调一次、带 undo=True。"
            return f"没有「{loc.name}」这道菜,什么都没动。list_recipes 看看存了哪些。"
        if trashed.is_file():
            # 覆盖它就是**把上一次删掉的那份销毁**,而"删掉的东西还能拿回来"正是这个
            # 工具的全部意义。宁可拒绝一次合法的删除,也不静默毁掉一份做法(M5-20)。
            return (
                f"删掉的那批里已经有一份「{loc.name}」了,所以这次没删——"
                f"搬过去会把上次删掉的那份盖掉。先带 undo=True 把旧的拿回来处理掉,"
                f"或者给现在这份改个名。"
            )
        store.move(live, trashed)
        store.save_reason(trashed, reason)
        return (
            f"删了「{loc.name}」,原因{OPEN}{_inline(reason)}{CLOSE}。文件搬到一边存着、没真删,"
            f"删错的话再调一次、带 undo=True 就能原样拿回来。"
        )

    return [
        _speak_errors(fn)
        for fn in (
            list_recipes,
            read_recipe,
            write_recipe,
            append_to_recipe,
            replace_in_recipe,
            search_recipes,
            rename_recipe,
            delete_recipe,
        )
    ]


def build(data_dir: Path) -> BundleRuntime:
    """统一构造入口(bundle 契约)。工具顺序由 manifest.yaml 与测试钉死(前缀第 0 层)。

    **不收 timezone**:这一层没有任何时间戳。删除理由不带时间——按 G6 问下来它没有
    读者(要查什么时候删的,文件的 mtime 就在盘上),而 bundle 不许自己兜默认时区。
    """
    store = RecipeStore(Path(data_dir) / "recipes")
    # 目录先建出来:用户要能在自己电脑上 cd 进去 cat 自己的菜谱,那正是选文件而不是
    # SQLite 的全部理由——空目录也得在,不然他找不到该往哪儿放。
    store.root.mkdir(parents=True, exist_ok=True)
    return BundleRuntime(tools=_tool_functions(store))


def create_server(data_dir: Path) -> FastMCP:
    """MCP 服务入口,和 finance / memory 同形状;生产单独容器时由它接管。"""
    mcp = FastMCP("recipes")
    for fn in build(data_dir).tools:
        mcp.tool()(fn)
    return mcp


if __name__ == "__main__":
    import os

    # 独立容器形态下 bundle 自己读 env(进程边界就是它的配置入口)。这个默认值必须和
    # `Settings.data_dir` 的默认值一致——改一边就得改另一边。
    create_server(Path(os.environ.get("LARARIUM_DATA_DIR", "./data"))).run()
