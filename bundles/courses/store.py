"""学习 bundle 的存储层:课程名 → 一门课的目录、笔记本的读写与分页、回收站搬整个目录。

**共用的那一层在 `lararium.docstore`**(名字校验两道、`replace` 的匹配纪律、扫文件搜索、
逐字节读写、分页钳位)——M6-5 把它们拆成模块级小函数就是为了这一天,而
`.importlinter` 禁止 bundle 之间 import,所以共用的那一份住在 `lararium` 侧。
**这个文件里只有"学习这边和做菜不一样"的那些**:

```
落点的单位是一门课的目录     <root>/<课程>/notes.md
                             ——所以改名和删除搬的是整个目录;课件的归属在表里
                             (M6-6b,`materials.py`),键就是这个目录的相对路径(`label`)
笔记本会长,所以要分页       做菜那边 read_recipe 不分页,这一条只有学习要(G5:
                             按两个真实用例定边界,只有一边用的不进共用层)
回收站里是 <课程>-<时间戳>/  M5-20 的形状。有时间戳,所以同一门课删两次不会撞;
                             时间戳里没有连字符,所以「C-语言」解得回来
```

**为什么笔记是文件不是表**:用户会亲手改它。而"用户会亲手改的东西用文件,不会亲手改的
用表"这条判据是任务书里定的——课件的归属(M6-6b)是表,笔记是文件。
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from lararium.docstore import (
    MAX_NAME_CHARS,
    TRASH_DIR,
    UnreadableDocument,
    name_fault,
    normalize_name,
    read_document,
    relocate,
    save_document,
    under,
)

# 一门课一个笔记本,文件名写死。**不给模型起名的机会**:多份笔记会逼它编文件名
# (「第一章笔记」/「chapter1」/「线代笔记1」),而那是让模型发明一个维度。
NOTES = "notes.md"
# 6a 预留的课件目录名。**M6-6b 没有用它**:课件定下来是"只记归属,不拷字节"
# (字节在媒体池里,归属在 `.materials.sqlite` 里),没有任何代码往这个目录里写。
# 常量留着,是因为 6a 的测试拿"手工放进 materials/ 的一份文件"钉着"改名 / 删除搬的是
# 整个课程目录"——那条性质仍然成立(用户自己放进课程目录的任何东西都跟着走),
# 而那几条测试不许改。
MATERIALS = "materials"
# 删除理由落在被搬走的那个目录**旁边**,不在里面:落在里面的话恢复回来就多出一个文件,
# 而 undo 的口径是**逐字节一致**(M5-20)。
REASON_SUFFIX = ".reason"

# 一页多少字。口径和这个项目别处的上限一样——一次工具调用不许顶穿 L0(约 1200 字,
# 和 `list_recipes` 一页 30 行、一次检索是同一个量级)。
NOTE_PAGE_CHARS = 1200
# 回收站目录名里的时间戳。**里面不许有连字符**:课程名可以有(「C-语言」),
# 而名字是靠最后一个连字符解回来的。
STAMP_FORMAT = "%Y%m%dT%H%M%S"

# 名字保持 bundle 自己的叫法(做菜那边是 `UnreadableRecipe`):报错那句话里带的是
# **课程名**,而文件名一律叫 `notes.md`,「某处解码失败」指不到任何地方。
UnreadableNote = UnreadableDocument


def name_error(name: str) -> str | None:
    """第一道校验的**人话那一半**:合法返回 None,不合法返回一句给模型的话(E2)。

    判定在 `docstore.name_fault`(和做菜 bundle 共用同一份);这里只负责"用学习的话
    说出来"。**为什么句子不共用**:做菜那句「菜谱是平的一层,名字就是名字,不分类、
    不带层级」在这边是**假的**(一门课就是一个目录,下面还有 materials/),
    而把两套句子塞进一个带 `noun=` 的模板是 G7 那种假统一。
    """
    fault = name_fault(name)
    if fault is None:
        return None
    if fault.kind == "empty":
        return '课程名是空的。给一门课的名字,比如 read_note("线性代数")。'
    if fault.kind == "too_long":
        return (
            f"这个课程名太长了({len(name)} 字,最多 {MAX_NAME_CHARS} 字),没这么建。"
            f"取个短名字当课程名,全称写进笔记正文里。"
        )
    if fault.kind == "forbidden":
        return (
            f"课程名里不能有「{fault.found}」,这门课没建/没读。一门课一个名字,"
            f"笔记在这门课自己的地方,不用你指路径。"
        )
    if fault.kind == "leading_dot":
        return "课程名不能以「.」开头,这门课没建/没读。换个正常的课程名。"
    return "这个课程名里有控制字符,没这么建。重打一遍课程名。"


def paginate(text: str, size: int) -> list[str]:
    """把一本笔记切成一页一页,**拼回来逐字节等于原文**。

    ★ 切在**行尾**:一段被切成两半的公式(或者一行 markdown 表格)两边都读不懂。
    一行本身就比一页长(用户粘了一大段没有换行的东西)时只能硬切,但仍然拼得回去
    ——"切开给"不许变成"改写",那是硬口径二(写进去再读回来一个字节不变)。
    """
    if not text:
        return [""]
    pages: list[str] = []
    rest = text
    while rest:
        if len(rest) <= size:
            pages.append(rest)
            break
        cut = rest.rfind("\n", 0, size + 1)
        pages.append(rest[: cut + 1] if cut > 0 else rest[:size])
        rest = rest[cut + 1 :] if cut > 0 else rest[size:]
    return pages


def page_index(pages: list[str], at: int) -> int:
    """一个下标落在第几页(从 1 起)。越界钳到最后一页,不报错。

    ★ 没有这一步,搜到了也不知道去读哪一页,而「找一段的正常姿势是搜,不是从头读」
    就断在最后一米上——搜索是主要入口,它得**接得上 `read_note`**。
    """
    seen = 0
    for i, page in enumerate(pages, start=1):
        seen += len(page)
        if at < seen:
            return i
    return len(pages)


@dataclass(frozen=True)
class CourseSpot:
    """一门课算出来的落点:课程目录 + 里面那个笔记本。

    `folder` 为 None 时 `notes` 也是 None,`error` 是给模型的那句人话。
    **算不出来就一个落点都不给**(同 M5-4:能自报路径的东西就是路径穿越的入口)。
    """

    name: str
    folder: Path | None
    notes: Path | None
    error: str


class CourseStore:
    """`<root>/<课程>/notes.md`,删掉的整个目录搬进 `<root>/.trash/<课程>-<时间戳>/`。

    **路径不是任何工具的参数**:底下怎么落盘模型不知道,所以 M6-6b 往里加 `materials/`
    的时候,这七个工具的签名一个字都不用动。
    """

    def __init__(self, root: Path, *, timezone: str) -> None:
        # 兜底比的是**解析过的** root:/tmp 在 macOS 上是指到 /private/tmp 的符号链接,
        # 不先解析,第一次比较就会把每一个合法落点都判成"出了目录"。
        self.root = Path(root).resolve()
        self.timezone = timezone

    def locate(self, raw: str) -> CourseSpot:
        """★ **唯一把课程名变成落盘位置的地方**,两道校验都在这里。

        所以七个工具里没有一个能绕开它拿到落点(G8:不是在七个动作上各装一遍闸
        ——那样漏掉的那一处是无声的——是让"一处"成为结构上的唯一入口)。
        """
        name = normalize_name(raw)
        error = name_error(name)
        if error is not None:
            return CourseSpot(name=name, folder=None, notes=None, error=error)
        folder = under(self.root, self.root / name)
        notes = under(self.root, self.root / name / NOTES)
        if folder is None or notes is None:
            return CourseSpot(
                name=name,
                folder=None,
                notes=None,
                error=f"「{name}」这个名字落不到课程目录里面,这门课没建/没读。换个正常的课程名。",
            )
        return CourseSpot(name=name, folder=folder, notes=notes, error="")

    def names(self) -> list[str]:
        """有哪些课。**一门课是一个目录**,所以列的是目录而不是笔记文件

        ——一门只有课件、还没记过笔记的课(M6-6b 的 `add_file` 先建目录)也是一门课。

        ★ **跳过前导 `.` 的**:名字白名单拒前导 `.`,所以那些目录**模型寻址不到**,
        列出来只会让用户看到一门叫 `.trash` 的课,而她点不开。
        """
        if not self.root.is_dir():
            return []
        return sorted(
            p.name for p in self.root.iterdir() if p.is_dir() and not p.name.startswith(".")
        )

    def entries(self) -> list[tuple[str, str]]:
        """(课程名, 笔记正文) 逐本读出来——搜索就是扫这个(`docstore.scan`,不建索引)。

        某一本读不出来就**整个搜索报错**,不是跳过它:跳过去的那本和"确实没写过"长得
        一模一样,而那恰恰是"不建索引"要避免的那种无声。报错说得出是哪门课。
        """
        return [(name, self.read(self.root / name / NOTES, label=name)) for name in self.names()]

    def read(self, notes: Path, *, label: str) -> str:
        """读一本,**不动一个字节**;文件不在回空串(「有课没笔记」是合法状态)。"""
        return read_document(notes, label=label)

    def save(self, notes: Path, text: str) -> None:
        """原子写:要么旧的完整、要么新的完整,永不被截断(共用层那一份)。"""
        save_document(notes, text)

    def label(self, folder: Path) -> str:
        """一门课在课件归属表里的键:**课程目录相对课程根的路径**。

        活着的课是「线性代数」,回收站里的是「.trash/线性代数-<时间戳>」。键和目录是同一个
        东西的两种写法,所以"目录搬到哪、归属跟到哪"只要在搬之前和搬之后各算一次(M6-6b)。
        `folder` 必须是 `locate` / `into_trash` / `latest_trashed` 给出来的落点(都已解析过)。
        """
        return folder.relative_to(self.root).as_posix()

    def create(self, folder: Path) -> None:
        """建出一门课的目录(还没有笔记)。`add_file` 归到一门新课时用——一门课是一个目录,
        `names()` 只认目录,不建的话这门课在 `list_courses` 里看不见。"""
        folder.mkdir(exist_ok=True)

    def rename(self, src: Path, dst: Path) -> None:
        """改名:**搬整个课程目录**,笔记和课件一起走,字节天然一个不变。

        调用方负责先确认 `dst` 不存在——合并比重名更坏(见 `rename_course`)。
        """
        relocate(src, dst)

    # ── 回收站 ────────────────────────────────────────────────────────

    def trash_root(self) -> Path | None:
        """回收站目录自己的落点**也要过兜底**。

        G8 的那个问法——"这条不变量还有哪些动作能让它不成立":`.trash` 被做成一条指到
        课程目录外面的符号链接时,"搬到回收站"就是把整门课搬出课程目录,
        而 `locate` 那两道只看课程目录自己。
        """
        return under(self.root, self.root / TRASH_DIR)

    def deleted(self) -> list[tuple[str, str, Path]]:
        """回收站里有哪些 (课程名, 当初的理由, 落点),按目录名定序(= 按时间)。"""
        trash = self.trash_root()
        if trash is None or not trash.is_dir():
            return []
        return [
            (p.name.rsplit("-", 1)[0], self.read(self._reason_path(p), label=p.name), p)
            for p in sorted(trash.iterdir())
            if p.is_dir()
        ]

    def latest_trashed(self, name: str) -> Path | None:
        """这门课最近删掉的那一份。删了两次就拿回最近那次——LIFO,和"删错了立刻撤回"一致。"""
        found = [path for deleted_name, _, path in self.deleted() if deleted_name == name]
        return found[-1] if found else None

    def into_trash(self, folder: Path, name: str, reason: str) -> Path | None:
        """整个课程目录搬进回收站,理由写在它旁边。落点算不出来时回 None(见 `trash_root`)。

        **搬,不是 unlink**(M5-20):一个字节不销毁,所以 `undo` 逐字节一致是免费的。
        """
        trash = self.trash_root()
        if trash is None:
            return None
        stamp = datetime.now(ZoneInfo(self.timezone)).strftime(STAMP_FORMAT)
        spot = trash / f"{name}-{stamp}"
        # 同一秒里删同一门课两次(删掉 → 重建 → 再删)时不许盖掉上一份:盖掉就是把
        # 上次删的那份销毁,而"删掉的还能拿回来"是这个工具的全部意义。
        serial = 2
        while spot.exists():
            spot = trash / f"{name}-{stamp}_{serial}"
            serial += 1
        relocate(folder, spot)
        save_document(self._reason_path(spot), reason)
        return spot

    def out_of_trash(self, trashed: Path, folder: Path) -> None:
        """搬回来,并把理由那个小文件收掉(它不是用户的笔记,只是回收站的登记)。"""
        relocate(trashed, folder)
        self._reason_path(trashed).unlink(missing_ok=True)

    def _reason_path(self, trashed: Path) -> Path:
        # `with_name(name + 后缀)`:课程名里可以有点,而 `with_suffix` 会让
        # 「高数.上」和「高数.下」共用一个理由文件,谁删谁覆盖(M6-5 踩过)。
        return trashed.with_name(trashed.name + REASON_SUFFIX)
