"""做菜 bundle 的存储层:菜名 → 落点、读写、改名、回收站、扫文件搜索。

**为什么是文件不是表**(用户定的,任务书里有他的原话):一份做法没有稳定的形状——
步骤数不定、「焯水的时候加勺盐」是注解不是步骤、「上次咸了,酱油减半」该写在它影响的
那一步旁边,塞进表里就得给每一条注解找个格子。而「存 SQLite 让路径穿越成为一类不存在的
bug」这个说法是错的:不落盘要付的代价是**用户没法在自己电脑上打开自己的菜谱、没法 cat、
没法 cp 一份备份**,那不是零成本。选文件,而**路径不是接口的一部分**。

**为什么单独一个模块**:这一层是 M6-6(学习 bundle:同一套文档库加一层课程)要**整体
搬走**的那一份,而 `.importlinter` 禁止 bundle 互相依赖——到那天只有"挪到 lararium 侧
共用"或者"抄一份"两条路,而抄一份不行:名字校验、落点兜底、`replace` 的匹配纪律、
扫文件搜索,每一条都是安全或正确性攸关的,抄一份就是两处维护同一个事实。

**这一轮不为此建任何抽象**(G5:先抽象再用是猜,而这个项目为"先猜"付过账,M5-11)。
这一条只做一件事:把这些逻辑放在**能被整体搬走的地方**,而不是埋在八个工具函数体里。
形状照 `bundles/memory`——逻辑在自己的模块,FastMCP 适配留在 `server.py`。
"""

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

SUFFIX = ".md"
# 删掉的搬进这里(M5-20:移走不是 unlink)。前导 `.` 不只是"约定俗成的隐藏":
# 名字白名单拒前导 `.`,所以**模型寻址不到它**——回收站不是一个它能打开的命名空间。
TRASH_DIR = ".trash"
# 删除理由落在做法**旁边**的小文件里,不写进做法正文——那是用户的字,存储层不动它。
REASON_SUFFIX = ".reason"

# 菜名长度封顶。真人说不出 40 字的菜名,而这个数同时把 UTF-8 文件名压在文件系统那
# 255 字节以内(40 个汉字乘 4 字节,加后缀),不必再猜哪个文件系统更严。
MAX_NAME_CHARS = 40
# 一行里嵌一段文本(删除理由、搜索词、replace 的原文回显)的上限,口径和 finance 的
# `MAX_NOTE_CHARS` 一样:一行看得完。**三处共用一个数**,别让它们各自漂。
MAX_INLINE_CHARS = 60

# 控制字符:归一化折掉的是空白,`\x00` / `\x1b` 这类不在其中。它们在文件名里没有任何
# 正当用途,而一个带 `\r` 的菜名在任何日志/终端里都会把后半行盖掉。
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# 白名单拒的那几样。`..` 按**子串**拒:一道菜的名字里不可能有它,而按"路径分量"判
# 就得先把名字当路径解析一遍,那正是这一层要避免的事。
_FORBIDDEN = ("/", "\\", "..")

# 命中片段/改动前后那一小段,各取前后多少字。片段是"够判断值不值得 read_recipe 全文"
# 的量,不是正文的替代品——给多了它就是一个伪装成搜索的"全部读出来"。
SNIPPET_RADIUS = 40
EXCERPT_RADIUS = 30

# 本模块用来把一段文本包进一行里的界符。正文里出现它就能在同一行伪造出第二个字段,
# 一并中和(照 finance `_render_note` 的第二刀)。
OPEN = "「"
CLOSE = "」"


class UnreadableRecipe(Exception):
    """某份 .md 读不出来(最常见的是它不是 UTF-8——文件是用户自己的,编辑器随他挑)。

    **带上是哪一道菜**:`UnicodeDecodeError` 自己那句话里没有文件名,而"某处解码失败"
    指不到任何地方,用户拿着它什么也做不了(E3)。
    """


def normalize_name(raw: str) -> str:
    """归一化在校验**之前**做:折内部空白、去首尾。

    「 番茄  炒鸡蛋 」和「番茄 炒鸡蛋」必须落到同一道菜——不然同一个名字能在库里存出
    两份,而用户在文件管理器里看到的是两个长得一样的文件,谁也说不清哪份是新的。
    """
    return re.sub(r"\s+", " ", raw or "").strip()


def name_error(name: str) -> str | None:
    """第一道:白名单。合法返回 None,不合法返回**人话理由**(E2)。

    ★ **理由不是"防路径"**——路径已经不是任何工具的参数了。这一道守的是**命名空间的
    完整性**:一个叫 `..` 或带 `/` 的菜名会跑到别人家里;一个前导 `.` 的菜名能指到
    `.trash`,而回收站不该是模型寻址得到的地方。

    它守的是"我想到的那些"。想不到的交给 `under()` 那一道——两道**各自独立成立**,
    抽掉任何一道,另一道仍然挡得住一部分(变异检查逐条钉过)。
    """
    if not name:
        return '菜名是空的。给一个菜名,比如 read_recipe("番茄炒鸡蛋")。'
    if len(name) > MAX_NAME_CHARS:
        return (
            f"这个菜名太长了({len(name)} 字,最多 {MAX_NAME_CHARS} 字),没这么存。"
            f"取个短名字当菜名,长的说明写进做法正文里。"
        )
    for bad in _FORBIDDEN:
        if bad in name:
            return (
                f"菜名里不能有「{bad}」,这一道没存/没读。菜谱是平的一层,"
                f"名字就是名字,不分类、不带层级。"
            )
    if name.startswith("."):
        return "菜名不能以「.」开头,这一道没存/没读。换个正常的菜名。"
    if _CONTROL.search(name):
        return "这个菜名里有控制字符,没这么存。重打一遍菜名。"
    return None


def under(root: Path, target: Path) -> Path | None:
    """第二道:落点兜底。算完 `resolve()` 再问它还在不在 `root` 底下,不在就返回 None。

    白名单是"我想到的那些",这一道是"**不管怎么绕**,出了这个目录就不行"——角色等同
    M5-22 五步里的 `getpeername()` 复查:前面每一步都可能有想漏的,所以在真正要落地的
    那一刻,按**实际落点**再核一次。

    `resolve()` 顺带把符号链接跟穿:一条指到目录外面的 `番茄炒鸡蛋.md` 会在这里被拦下,
    而不是让我们**透过它**往外写。`root` 必须是已经 resolve 过的那一份(见 RecipeStore)。
    """
    resolved = target.resolve()
    return resolved if resolved.is_relative_to(root) else None


def one_line(text: str) -> str:
    """把一段文本折成一行,并中和本模块的行内界符。

    ★ **这一刀是渲染,不是存储**:`read_recipe` 回的是文件原样、逐字节不动;而列表行、
    命中片段、删除理由都是**一行一条**的形状,里面的换行不折掉,一段正文就能凭换行伪造出
    后续列表项,而伪造出来的那行和真条目形式上一模一样(P1-2 的形状,M4-4 / M5-21 各栽
    一次)。

    ★ **这一刀是实测之后才敢留的**(M6-5 探针):工具结果**在调用它的那一轮是原样进模型
    的**,组装器的折行与 200 字截断只作用在**历史轮**。也就是说这一行里的换行,没有任何
    上游会替我们折掉——当轮那份就是模型逐字节收到的那份。
    """
    folded = re.sub(r"\s+", " ", text).strip()
    return folded.replace(OPEN, "﹁").replace(CLOSE, "﹂")


def clip(text: str, limit: int) -> str:
    """超了就截断,并**说清少了多少**——静默截断读起来和"就这些"一模一样(M4-3)。"""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…(还有 {len(text) - limit} 字)"


def excerpt(text: str, at: int, length: int, radius: int) -> str:
    """取 `text[at : at+length]` 前后各 `radius` 字,折成一行;两头的 `…` 表示"还有"。

    `replace` 改完要把**改动前后那一小段**回给模型让它自己核对,不是只回一句"改好了"
    ——"改好了"三个字在改对一处和改错一处之间没有任何区别。
    """
    start = max(0, at - radius)
    end = min(len(text), at + length + radius)
    body = one_line(text[start:end])
    return f"{'…' if start > 0 else ''}{body}{'…' if end < len(text) else ''}"


@dataclass(frozen=True)
class Replaced:
    """一次定点替换的结果。`text` 只在恰好命中一次时有值,别处一律 None。"""

    count: int
    at: int
    text: str | None


def replace_once(text: str, old: str, new: str) -> Replaced:
    """`old` **恰好出现一次**才换;零次和多次都不换。

    照的是 coding agent 的 edit 纪律——而它安全不是因为模型聪明,是因为**匹配不上就拒绝
    执行**:零次说明记错了原文、或者这一处已经改过了;多次说明这段话在文件里不唯一,
    而**改第一处是在猜用户指的是哪一处**,猜错了就是静默改坏一份做法。
    """
    count = text.count(old)
    if count != 1:
        return Replaced(count=count, at=-1, text=None)
    at = text.index(old)
    return Replaced(count=1, at=at, text=text[:at] + new + text[at + len(old) :])


@dataclass(frozen=True)
class Hit:
    """一条命中。`in_name` / `in_text` 分开记:名字命中通常更强,模型该知道差别。"""

    name: str
    in_name: bool
    in_text: bool
    snippet: str


def scan(entries: Iterable[tuple[str, str]], query: str) -> list[Hit]:
    """菜名和内容都搜,**名字命中排在前面**,同一类里按菜名定序(输出不许抖)。

    ★ **不建索引,直接扫文件。** 这不是偷懒,是"用文件"这个决定推出来的:用户随时会用
    编辑器直接改那些 `.md`(那正是选文件而不是 SQLite 的全部理由),**而任何索引都会在
    那一刻变味,且变味之后没有任何报错**——搜不到的东西和"确实没写过"长得一模一样。
    几十份几 KB 的文本,扫一遍的代价小于维护一致性的代价。真到几百份再说,
    **而到那时候正确的做法是加缓存 + 失效判据,不是先建索引。**

    大小写不敏感靠 `casefold`,而下标是拿折叠过的串算的:极少数字符折叠后长度会变
    (`ß` → `ss`),那时片段的窗口会偏一两个字。**只偏窗口,不偏内容**——片段本来就是
    一个窗口,不是逐字引用,所以这里不为它多写一层。
    """
    needle = query.casefold()
    by_name: list[Hit] = []
    by_text: list[Hit] = []
    for name, text in entries:
        in_name = needle in name.casefold()
        at = text.casefold().find(needle)
        in_text = at >= 0
        if not (in_name or in_text):
            continue
        snippet = excerpt(text, min(at, len(text)), len(query), SNIPPET_RADIUS) if in_text else ""
        hit = Hit(name=name, in_name=in_name, in_text=in_text, snippet=snippet)
        (by_name if in_name else by_text).append(hit)
    return sorted(by_name, key=lambda h: h.name) + sorted(by_text, key=lambda h: h.name)


def page_of[T](items: list[T], page: int, per_page: int) -> tuple[list[T], int, int]:
    """切一页出来,页码钳到 `[1, 总页数]`;返回 (这一页, 页码, 总页数)。

    口径照 `steward/tools.py` 的 `_paged_search`,而**那一份 bundle import 不到**
    (`.importlinter` 的 bundles-not-depend-on-steward)。所以这里是同一套口径的第二份
    实现,不是另一套口径:0/负数/超大页码都钳住不报错,超大回**最后一页**而不是空列表
    ——空列表读起来和"没有了"一模一样。
    """
    total = len(items)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    return items[start : start + per_page], page, total_pages


@dataclass(frozen=True)
class Located:
    """一个菜名算出来的两个落点:菜谱里那个、回收站里那个。

    **两个一起给,不是分两次问。** 删除/恢复同时要用到两边,而"再 locate 一次拿另一个"
    会逼出一条永远走不到的错误分支(第二次校验的输入和第一次一模一样),而写下一条
    走不到的分支,下一个读代码的人就得替它想一遍它什么时候会发生。

    `path` 为 None 时 `trash` 也是 None,`error` 是给模型的那句人话。
    """

    name: str
    path: Path | None
    trash: Path | None
    error: str


class RecipeStore:
    """一层扁平的 markdown 文件:`<root>/<菜名>.md`,删掉的搬进 `<root>/.trash/`。

    **一层,没有分类。** 用户否掉了分类,理由是「番茄炒鸡蛋属于什么类?煮面水什么的,
    这种家常菜都很难说怎么分类」——而扁平化顺带消掉了"同一个菜名出现在两个类里"那个
    歧义分支:**名字天然唯一**。

    **路径不是任何工具的参数**:底下怎么落盘模型不知道,所以以后想改布局、改成别处存,
    八个工具的签名一个字都不用动。这条和 M5-4 同源——`Attachment` 上**根本没有可写的
    path 字段**,「能自报路径的附件就是路径穿越的入口」。
    """

    def __init__(self, root: Path) -> None:
        # 兜底比的是**解析过的** root:/tmp 在 macOS 上是指到 /private/tmp 的符号链接,
        # 不先解析,第一次比较就会把每一个合法落点都判成"出了目录"。
        self.root = Path(root).resolve()
        self.trash = self.root / TRASH_DIR

    def locate(self, raw: str) -> Located:
        """★ **唯一把菜名变成落盘位置的地方**,两道校验都在这里。

        所以八个工具里没有一个能绕开它拿到落点。这正是 G8 的那个问法——"这条不变量,
        除了我正在写的这个动作,还有哪些动作能让它不成立"——的答案:不是在八个动作上
        各装一遍闸(那样漏掉的那一处是无声的),是让"一处"成为结构上的唯一入口。
        """
        name = normalize_name(raw)
        error = name_error(name)
        if error is not None:
            return Located(name=name, path=None, trash=None, error=error)
        path = under(self.root, self.root / f"{name}{SUFFIX}")
        trash = under(self.root, self.trash / f"{name}{SUFFIX}")
        if path is None or trash is None:
            return Located(
                name=name,
                path=None,
                trash=None,
                error=f"「{name}」这个名字落不到菜谱目录里面,没这么存。换个正常的菜名。",
            )
        return Located(name=name, path=path, trash=trash, error="")

    def names(self) -> list[str]:
        """存了哪些菜。

        **`glob` 不是 `rglob`**,而这一个字母就是回收站的隔离带:`.trash/` 是子目录,
        顶层 glob 看不见它。换成 rglob 就会把删掉的菜混进「存了哪些」里,而那正是
        M5-20 要消灭的那个形态——**删了还在**。
        """
        return sorted(p.stem for p in self.root.glob(f"*{SUFFIX}"))

    def deleted(self) -> list[tuple[str, str]]:
        """回收站里有哪些,连同当初写下的理由。"""
        if not self.trash.is_dir():
            return []
        return [
            (p.stem, self._read(self._reason_path(p)))
            for p in sorted(self.trash.glob(f"*{SUFFIX}"))
        ]

    def entries(self) -> list[tuple[str, str]]:
        """(菜名, 正文) 逐份读出来——搜索就是扫这个,见 `scan` 的 docstring。

        某一份读不出来就**整个搜索报错**,不是跳过它:跳过去的那份和"确实没写过"长得
        一模一样,而那恰恰是"不建索引"要避免的那种无声。报错说得出是哪一道菜。
        """
        return [(p.stem, self._read(p)) for p in sorted(self.root.glob(f"*{SUFFIX}"))]

    def read(self, path: Path) -> str | None:
        """读一份,**不动一个字节**;文件不在返回 None。"""
        if not path.is_file():
            return None
        return self._read(path)

    def save(self, path: Path, content: str) -> None:
        """同目录 `.tmp` → fsync → 原子替换:文件要么旧的完整、要么新的完整,永不被截断。

        **这六行是从 `bundles/memory/ledger.py` 抄来的**,而抄是被迫的:
        `.importlinter` 的 bundles-are-independent 禁止 bundle 之间 import
        (finance 抄围栏分隔符是同一个理由)。抄的是标准库调用,漂移面为零。

        直接 `write_text` 的失败面孔是:写一半崩 → 做法被截断 → 读回来不报错,
        而"只写到第 3 步"和"用户只写了 3 步"长得一模一样。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)

    def move(self, src: Path, dst: Path) -> None:
        """改名 / 进出回收站:**搬文件,不重写内容**,所以字节天然一个不变。"""
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)

    def save_reason(self, trashed: Path, reason: str) -> None:
        self.save(self._reason_path(trashed), reason)

    def drop_reason(self, trashed: Path) -> None:
        self._reason_path(trashed).unlink(missing_ok=True)

    def _reason_path(self, trashed: Path) -> Path:
        # `with_name(name + 后缀)` 而不是 `with_suffix`:菜名里可以有点,
        # 而 `with_suffix` 会把「蛋炒饭.简化版.md」的理由文件算成「蛋炒饭.reason」
        # ——于是「蛋炒饭.简化版」和「蛋炒饭.家常版」共用一个理由文件,谁删谁覆盖。
        return trashed.with_name(trashed.name + REASON_SUFFIX)

    def _read(self, path: Path) -> str:
        # ★ `newline=""`(读和写两边都要)。默认的通用换行会把 `\r\n` 翻成 `\n`,
        # 于是一份从 Windows 编辑器或网页粘过来的做法,存进去再读回来就**少了字节**
        # ——而硬口径第一条是"一个字节不许变"。这不是理论:第一版就是
        # `read_text(encoding="utf-8")`,那条 CRLF 的测试当场判红。
        # (`Path.read_text` 的 `newline` 参数是 3.13 才加的,所以这里走 `open`。)
        if not path.is_file():
            return ""
        try:
            with path.open("r", encoding="utf-8", newline="") as f:
                return f.read()
        except UnicodeDecodeError as exc:
            raise UnreadableRecipe(
                f"「{path.stem}」这份文件不是 UTF-8 存的,读不出来。"
                f"用编辑器打开它、另存成 UTF-8 就好了。"
            ) from exc
