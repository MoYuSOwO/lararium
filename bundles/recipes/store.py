"""做菜 bundle 的存储层:菜名 → 落点、读写、改名、回收站、以及这个 bundle 说的那些人话。

**为什么是文件不是表**(用户定的,任务书里有他的原话):一份做法没有稳定的形状——
步骤数不定、「焯水的时候加勺盐」是注解不是步骤、「上次咸了,酱油减半」该写在它影响的
那一步旁边,塞进表里就得给每一条注解找个格子。而「存 SQLite 让路径穿越成为一类不存在的
bug」这个说法是错的:不落盘要付的代价是**用户没法在自己电脑上打开自己的菜谱、没法 cat、
没法 cp 一份备份**,那不是零成本。选文件,而**路径不是接口的一部分**。

★ **M6-6a:共用的那一份已经搬到 `lararium.docstore` 了。** M6-5 刻意把名字校验、落点兜底、
`replace` 的匹配纪律、扫文件搜索拆成模块级小函数,原话是「不是为了抽象,是为了将来能搬」
——学习 bundle 就是那第二个真实用例,而 `.importlinter` 禁止它 import 这个 bundle,
所以共用的那一份住在 `lararium` 侧(bundle → lararium 是允许的,finance 就 import
`lararium.db`)。

**留在这里的是"做菜怎么摆、做菜怎么说话"**:扁平一层 `<菜名>.md`、回收站里那一份、
删除理由那个小文件、以及十一种非法名字各自的那句人话。共用层只回**判定**
(`docstore.name_fault` 说犯了哪一条),句子在这里——因为「菜谱是平的一层,名字就是名字」
这半句在学习 bundle 里是假的(那边一门课一个目录)。
下面那几个 `import` 里的名字是**转出去给工具层和测试用的**:定义只有一份,在共用层。
"""

from dataclasses import dataclass
from pathlib import Path

from lararium.docstore import (
    CLOSE,
    EXCERPT_RADIUS,
    MAX_INLINE_CHARS,
    MAX_NAME_CHARS,
    OPEN,
    SNIPPET_RADIUS,
    TRASH_DIR,
    Hit,
    NameFault,
    Replaced,
    UnreadableDocument,
    clip,
    excerpt,
    find_all,
    name_fault,
    normalize_name,
    one_line,
    page_of,
    read_document,
    relocate,
    replace_once,
    save_document,
    scan,
    under,
)

# **名字保持原样**:`server.py` 和老测试都按这个名字捕获它,而这一轮是**提取**
# ——提取不许改任何既有行为,一个别名比改八处调用点便宜,而定义仍然只有一份。
UnreadableRecipe = UnreadableDocument

__all__ = [
    "CLOSE",
    "EXCERPT_RADIUS",
    "MAX_INLINE_CHARS",
    "MAX_NAME_CHARS",
    "OPEN",
    "REASON_SUFFIX",
    "SNIPPET_RADIUS",
    "SUFFIX",
    "TRASH_DIR",
    "Hit",
    "Located",
    "NameFault",
    "RecipeStore",
    "Replaced",
    "UnreadableRecipe",
    "clip",
    "excerpt",
    "find_all",
    "name_error",
    "name_fault",
    "normalize_name",
    "one_line",
    "page_of",
    "read_document",
    "relocate",
    "replace_once",
    "save_document",
    "scan",
    "under",
]

SUFFIX = ".md"
# 删除理由落在做法**旁边**的小文件里,不写进做法正文——那是用户的字,存储层不动它。
REASON_SUFFIX = ".reason"


def name_error(name: str) -> str | None:
    """第一道校验的**人话那一半**:合法返回 None,不合法返回一句给模型的话(E2)。

    判定在 `docstore.name_fault`(和学习 bundle 共用同一份,变异检查两边各钉一遍);
    这里只负责"用做菜的话说出来"。一个字都不许改——M6-5 的验收逐条实测过这十一句,
    而 M6-6a 是提取,不是重写。
    """
    fault = name_fault(name)
    if fault is None:
        return None
    if fault.kind == "empty":
        return '菜名是空的。给一个菜名,比如 read_recipe("番茄炒鸡蛋")。'
    if fault.kind == "too_long":
        return (
            f"这个菜名太长了({len(name)} 字,最多 {MAX_NAME_CHARS} 字),没这么存。"
            f"取个短名字当菜名,长的说明写进做法正文里。"
        )
    if fault.kind == "forbidden":
        return (
            f"菜名里不能有「{fault.found}」,这一道没存/没读。菜谱是平的一层,"
            f"名字就是名字,不分类、不带层级。"
        )
    if fault.kind == "leading_dot":
        return "菜名不能以「.」开头,这一道没存/没读。换个正常的菜名。"
    return "这个菜名里有控制字符,没这么存。重打一遍菜名。"


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
        """(菜名, 正文) 逐份读出来——搜索就是扫这个,见 `docstore.scan` 的 docstring。

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
        """原子写(`docstore.save_document`):要么旧的完整、要么新的完整,永不被截断。"""
        save_document(path, content)

    def move(self, src: Path, dst: Path) -> None:
        """改名 / 进出回收站:**搬文件,不重写内容**,所以字节天然一个不变。"""
        relocate(src, dst)

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
        # `label` 给菜名(文件名去掉 `.md`):共用层不知道这份文件在用户嘴里叫什么,
        # 而"某处解码失败"指不到任何地方。
        return read_document(path, label=path.stem)
