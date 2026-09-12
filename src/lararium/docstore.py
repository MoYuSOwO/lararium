"""用户自己手写的 markdown 文档库那一层:名字校验、落点兜底、逐字节读写、匹配纪律、
扫文件搜索、分页。

**为什么住在 `lararium` 而不是某个 bundle 里**:两个 bundle 需要同一份
——做菜(`data/recipes/<菜名>.md`,扁平一层)和学习(`data/courses/<课程>/notes.md`,
一门课一个目录)。而 `.importlinter` 的 bundles-are-independent 禁止 bundle 互相 import,
所以到了第二个用例只有两条路:**挪到 lararium 侧共用,或者抄一份**。抄一份不行,
因为这四样每一样错了的症状都不一样地坏:

```
名字校验(白名单 + resolve() 落点兜底,两道)   错了 → 路径穿越
落点计算(只有 store 知道目录在哪)             错了 → 同上,而且是无声的
replace 的匹配纪律(恰好一次才改)              错了 → 静默改坏用户手写的一份文档
扫文件搜索(不建索引)                          错了 → 搜不到 = 和"没写过"长得一模一样
```

**边界是这两个真实用例定的,不是猜的(G5)**:进这里的每一样,recipes 和 courses
**都在用**;只有一边用的一律留在那一边——所以 `RecipeStore`(扁平 `<名字>.md`)、
`CourseStore`(一门课一个目录)、笔记本的 `paginate`(做菜那边 `read_recipe` 不分页)
都不在这里。反过来,**人话**也不在这里:两边的句子不一样(「菜名」/「课程名」、
「菜谱是平的一层」/「一门课一个笔记本」),所以这一层只回**判定**
(`name_fault` 给的是哪一条犯规),句子由各自的工具层说(E2)。
把两套句子塞成一个带 `noun=` 的模板是 G7 那种假统一:整齐,但里面那句话是假的。

**这一层不知道自己在给谁服务**:没有 bundle 名、没有领域词、没有时间戳、没有全局状态
(F5)。它只认「一个 root 目录 + 一个用户给的名字 + 一段文本」。
"""

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# 删掉的搬进这里(M5-20:移走不是 unlink)。前导 `.` 不只是"约定俗成的隐藏":
# 名字白名单拒前导 `.`,所以**模型寻址不到它**——回收站不是一个它能打开的命名空间。
TRASH_DIR = ".trash"

# 名字长度封顶。真人说不出 40 字的菜名/课程名,而这个数同时把 UTF-8 文件名压在文件系统
# 那 255 字节以内(40 个汉字乘 4 字节,加后缀),不必再猜哪个文件系统更严。
MAX_NAME_CHARS = 40
# 一行里嵌一段文本(删除理由、搜索词、replace 的原文回显)的上限:一行看得完。
# **所有用到它的地方共用一个数**,别让它们各自漂。
MAX_INLINE_CHARS = 60

# 控制字符:归一化折掉的是空白,`\x00` / `\x1b` 这类不在其中。它们在文件名里没有任何
# 正当用途,而一个带 `\r` 的名字在任何日志/终端里都会把后半行盖掉。
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# 白名单拒的那几样。`..` 按**子串**拒:一个正常的名字里不可能有它,而按"路径分量"判
# 就得先把名字当路径解析一遍,那正是这一层要避免的事。
# **顺序是接口的一部分**:`../../etc/passwd` 撞上的是 `/` 而不是 `..`,而调用方的句子
# 里带着撞上的那个字符——换顺序就换了那句话。
_FORBIDDEN = ("/", "\\", "..")

# 命中片段/改动前后那一小段,各取前后多少字。片段是"够判断值不值得读全文"的量,
# 不是正文的替代品——给多了它就是一个伪装成搜索的"全部读出来"。
SNIPPET_RADIUS = 40
EXCERPT_RADIUS = 30

# 把一段文本包进一行里的界符。正文里出现它就能在同一行伪造出第二个字段,一并中和。
OPEN = "「"
CLOSE = "」"


class UnreadableDocument(Exception):
    """某一份 `.md` 读不出来(最常见的是它不是 UTF-8——文件是用户自己的,编辑器随他挑)。

    **带上是哪一份**:`UnicodeDecodeError` 自己那句话里没有文件名,而"某处解码失败"
    指不到任何地方,用户拿着它什么也做不了(E3)。名字由调用方给(`label`):
    这一层不知道这份文件在用户嘴里叫什么——做菜那边是菜名,学习那边是课程名,
    而文件名都叫 `notes.md`。
    """


def normalize_name(raw: str) -> str:
    """归一化在校验**之前**做:折内部空白、去首尾。

    「 番茄  炒鸡蛋 」和「番茄 炒鸡蛋」必须落到同一份——不然同一个名字能在库里存出
    两份,而用户在文件管理器里看到的是两个长得一样的东西,谁也说不清哪份是新的。
    """
    return re.sub(r"\s+", " ", raw or "").strip()


@dataclass(frozen=True)
class NameFault:
    """名字犯了哪一条。**不是人话**——人话由调用方说(见模块 docstring)。

    `found` 只有 `forbidden` 那一条有值(撞上的那个子串),别处是空串。
    跨模块传的是有名字的东西,不是裸 dict / 裸字符串(F1)。
    """

    kind: Literal["empty", "too_long", "forbidden", "leading_dot", "control"]
    found: str = ""


def name_fault(name: str) -> NameFault | None:
    """第一道:白名单。合法返回 None,不合法返回**犯了哪一条**。

    ★ **理由不是"防路径"**——路径已经不是任何工具的参数了。这一道守的是**命名空间的
    完整性**:一个叫 `..` 或带 `/` 的名字会跑到别人家里;一个前导 `.` 的名字能指到
    `.trash`,而回收站不该是模型寻址得到的地方。

    它守的是"我想到的那些"。想不到的交给 `under()` 那一道——两道**各自独立成立**,
    抽掉任何一道,另一道仍然挡得住一部分(变异检查逐条钉过,两个 bundle 各一遍)。
    """
    if not name:
        return NameFault(kind="empty")
    if len(name) > MAX_NAME_CHARS:
        return NameFault(kind="too_long")
    for bad in _FORBIDDEN:
        if bad in name:
            return NameFault(kind="forbidden", found=bad)
    if name.startswith("."):
        return NameFault(kind="leading_dot")
    if _CONTROL.search(name):
        return NameFault(kind="control")
    return None


def under(root: Path, target: Path) -> Path | None:
    """第二道:落点兜底。算完 `resolve()` 再问它还在不在 `root` 底下,不在就返回 None。

    白名单是"我想到的那些",这一道是"**不管怎么绕**,出了这个目录就不行"——角色等同
    M5-22 五步里的 `getpeername()` 复查:前面每一步都可能有想漏的,所以在真正要落地的
    那一刻,按**实际落点**再核一次。

    `resolve()` 顺带把符号链接跟穿:一条指到目录外面的 `番茄炒鸡蛋.md`(或者一门课的
    整个目录)会在这里被拦下,而不是让我们**透过它**往外写。
    `root` 必须是已经 resolve 过的那一份(见各 store 的 `__init__`)。
    """
    resolved = target.resolve()
    return resolved if resolved.is_relative_to(root) else None


def one_line(text: str) -> str:
    """把一段文本折成一行,并中和行内界符。

    ★ **这一刀是渲染,不是存储**:读一份文档回的是文件原样、逐字节不动;而列表行、
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
    而**改第一处是在猜用户指的是哪一处**,猜错了就是静默改坏用户手写的东西。
    """
    count = text.count(old)
    if count != 1:
        return Replaced(count=count, at=-1, text=None)
    at = text.index(old)
    return Replaced(count=1, at=at, text=text[:at] + new + text[at + len(old) :])


def find_all(text: str, query: str, limit: int) -> list[int]:
    """`query` 在 `text` 里的落点(最多 `limit` 个),大小写不敏感,不重叠。

    ★ **"怎么算命中"只有这一份实现**:`scan` 拿它的第一个落点(一份一条,做菜那边就是
    这个形状),而一本长笔记要的是"这个词在哪几处"(学习那边),两者只差一个 `limit`。
    各写一份的那天,两边对"大小写"或者"重叠"的理解就开始漂,而症状是搜得出来的东西
    在一处有、另一处没有。

    大小写不敏感靠 `casefold`,而下标是拿折叠过的串算的:极少数字符折叠后长度会变
    (`ß` → `ss`),那时片段的窗口会偏一两个字。**只偏窗口,不偏内容**——片段本来就是
    一个窗口,不是逐字引用,所以这里不为它多写一层。
    """
    needle = query.casefold()
    if not needle:
        return []
    folded = text.casefold()
    found: list[int] = []
    at = folded.find(needle)
    while at >= 0 and len(found) < limit:
        found.append(at)
        at = folded.find(needle, at + len(needle))
    return found


@dataclass(frozen=True)
class Hit:
    """一条命中。`in_name` / `in_text` 分开记:名字命中通常更强,模型该知道差别。

    `at` 是内容里第一处命中的下标(只命中名字时是 -1)——学习那边要拿它换算"在第几页",
    做菜那边不用(`read_recipe` 不分页)。多一个字段而不是多一个函数:算它的代价是零,
    而两套搜索口径的代价不是。
    """

    name: str
    in_name: bool
    in_text: bool
    at: int
    snippet: str


def scan(entries: Iterable[tuple[str, str]], query: str) -> list[Hit]:
    """名字和内容都搜,**名字命中排在前面**,同一类里按名字定序(输出不许抖)。

    ★ **不建索引,直接扫文件。** 这不是偷懒,是"用文件"这个决定推出来的:用户随时会用
    编辑器直接改那些 `.md`(那正是选文件而不是 SQLite 的全部理由),**而任何索引都会在
    那一刻变味,且变味之后没有任何报错**——搜不到的东西和"确实没写过"长得一模一样。
    几十份几 KB 的文本,扫一遍的代价小于维护一致性的代价。真到几百份再说,
    **而到那时候正确的做法是加缓存 + 失效判据,不是先建索引。**

    ★ **顺序写死在这里**,不跟着文件系统的遍历顺序走:`iterdir`/`glob` 的顺序在不同机器上
    不一样,而一句"第 1/3 页"在两台机器上指着不同的东西是最难查的那类 bug。
    """
    by_name: list[Hit] = []
    by_text: list[Hit] = []
    for name, text in entries:
        in_name = query.casefold() in name.casefold()
        found = find_all(text, query, limit=1)
        at = found[0] if found else -1
        if not (in_name or found):
            continue
        snippet = excerpt(text, min(at, len(text)), len(query), SNIPPET_RADIUS) if found else ""
        hit = Hit(name=name, in_name=in_name, in_text=bool(found), at=at, snippet=snippet)
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


def read_document(path: Path, *, label: str) -> str:
    """读一份,**不动一个字节**;文件不在回空串。

    ★ `newline=""`(读和写两边都要)。默认的通用换行会把 `\\r\\n` 翻成 `\\n`,于是一份
    从 Windows 编辑器或网页粘过来的文本,存进去再读回来就**少了字节**——而"一个字节
    不许变"是这一层的硬口径。这不是理论:M6-5 第一版就是 `read_text(encoding="utf-8")`,
    那条 CRLF 的测试当场判红。(`Path.read_text` 的 `newline` 参数是 3.13 才加的,
    而全局约束是 3.12,所以这里走 `open`。)
    """
    if not path.is_file():
        return ""
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            return f.read()
    except UnicodeDecodeError as exc:
        raise UnreadableDocument(
            f"{OPEN}{label}{CLOSE}这份文件不是 UTF-8 存的,读不出来。"
            f"用编辑器打开它、另存成 UTF-8 就好了。"
        ) from exc


def save_document(path: Path, content: str) -> None:
    """同目录 `.tmp` → fsync → 原子替换:文件要么旧的完整、要么新的完整,永不被截断。

    直接 `write_text` 的失败面孔是:写一半崩 → 正文被截断 → 读回来不报错,
    而"只写到第 3 步"和"用户只写了 3 步"长得一模一样。

    `newline=""` 的理由见 `read_document`——**读和写两边都要**,少一边就少一边的字节。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def relocate(src: Path, dst: Path) -> None:
    """改名 / 进出回收站:**搬,不重写内容**,所以字节天然一个不变。

    一份文件、或者一整门课的目录(笔记 + 课件),两边都是这一个调用——`rename` 对目录
    同样是原子的,而"逐字节一致"这条口径在"搬"这个动作下是免费的,在"读出来再写回去"
    那条路上则要靠每一层都不出错。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
