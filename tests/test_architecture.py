"""架构不变量的机械化门禁。

这里的规则有个共同点:**单个功能测试查不出来,退化了也不会有人注意到**。
比如某天在 loop.py 里图省事直接写了一次账本文件——所有功能测试照样全绿,
但"单写者"原则已经死了,而且再也没人会发现。所以它们需要自己的守卫。

通用工具能管的不放这里:类型交给 mypy,import 边界交给 import-linter,
naive datetime 交给 ruff 的 DTZ 规则。这里只放它们都管不了的。
"""

import ast
from pathlib import Path

import pytest

SOURCE_ROOTS = (Path("src"), Path("bundles"))


def _source_files() -> list[Path]:
    files: list[Path] = []
    for root in SOURCE_ROOTS:
        if root.exists():
            files.extend(sorted(root.rglob("*.py")))
    return files


def _writeoffense(path: Path, node: ast.AST, detail: str) -> str:
    return f"{path}:{getattr(node, 'lineno', '?')} {detail}"


def _open_mode_arg(call: ast.Call) -> str | None:
    """从 open(...) 调用里取 mode 参数:位置第二参,或 mode= 关键字参。"""
    if call.keywords:
        for kw in call.keywords:
            if (
                kw.arg == "mode"
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)
            ):
                return kw.value.value
    if (
        len(call.args) >= 2
        and isinstance(call.args[1], ast.Constant)
        and isinstance(call.args[1].value, str)
    ):
        return call.args[1].value
    return None


def test_only_the_ledger_module_writes_files() -> None:
    """账本单写路径(DESIGN §6.3、宪法第一条)。

    账本的唯一合法写入者是 Gate.settle() → Ledger.write()。任何别处的文件写入
    都绕过了门控,等于给提示注入开了后门。这里用"整个源码树只有 ledger.py 能写文件"
    这条更粗但更结实的规则来守——本项目除账本外没有别的东西需要写文件。

    用 AST 而非子串匹配:旧的子串检查拦不住 `open(p, "w").write(...)`。这里禁
    `open(..., "w"/"a"/"x")`(含二进制/`+` 变体)、文件对象 `.write*`、Path 的
    write 方法族、`os.replace` 与 `shutil` 写族。
    """
    allowed = {
        Path("bundles/memory/ledger.py"),
        # CLI 客户端(M2-6)要持久化自己的 after 游标(~/.lararium/cli.seq),让
        # "杀掉重开 after 用上次 seq → 不丢不重"。这是**客户端自己的状态文件**,
        # 不在服务端 data_dir、也不碰账本;cli.py 是纯 HTTP 客户端,拿不到 ledger。
        Path("src/lararium/gateway/cli.py"),
        # 微信适配器(M5-3)要持久化自己的会话状态:收信游标 get_updates_buf、
        # 出件箱位置 after、以及 bot_token 与 context_token。理由和 cli.py 一样,
        # 而且更硬——游标不存会重收或漏收,after 不存会在重启后**重发**(用户收到
        # 两遍同一句回复,比没收到还糟)。同样是**客户端自己的状态文件**:
        # wechat.py 是纯 HTTP 客户端,`.importlinter` 钉着它 import 不到 steward/bundles,
        # 结构上就够不着账本。
        # M5-4 起它还写 `{data_dir}/media/<sha256>.<ext>`:附件字节。文件名**由内容哈希
        # 算出来**(`Attachment.path`,sha256 有 pattern 校验),对方给的文件名一个字节
        # 都不参与——不然一个叫 `../../prompts/character.default.md` 的附件就是人设的
        # 写入口,而人设被改是之后每一轮都听新的。
        Path("src/lararium/gateway/wechat.py"),
    }
    offenders: list[str] = []

    for path in _source_files():
        if path in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                # open(...) 带写模式
                if isinstance(func, ast.Name) and func.id == "open":
                    mode = _open_mode_arg(node)
                    if mode is None or any(c in mode for c in "wax"):
                        offenders.append(_writeoffense(path, node, f"open(mode={mode!r}) 写文件"))
                # Path 便捷写方法 X.write_text(...) .write_bytes(...)(receiver 无关,
                # write/writelines 则不要:文件对象的 .write 已被上面的 open(...,"w") 兜住,
                # 而 Ledger.write() 是合法门控路径,泛泛地禁会误伤)
                elif isinstance(func, ast.Attribute) and func.attr in (
                    "write_text",
                    "write_bytes",
                ):
                    offenders.append(_writeoffense(path, node, f".{func.attr}() 写文件"))
                # os.replace / os.rename(原子改名=写)
                elif (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                    and func.attr in ("replace", "rename")
                ):
                    offenders.append(_writeoffense(path, node, f"os.{func.attr}() 写文件"))
                # shutil 写族:copy/move/copyfile/copy2/copytree/rmtree(rmtree 其实是删,一并禁)
                elif (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "shutil"
                    and func.attr in ("copy", "copyfile", "copy2", "copytree", "move", "rmtree")
                ):
                    offenders.append(_writeoffense(path, node, f"shutil.{func.attr}() 写文件"))

    assert offenders == [], (
        f"这些文件在写文件,但账本的唯一写入路径必须是 Gate.settle():{offenders}。"
        "如果确实需要写别的文件(不是账本),把它加进本测试的 allowed 白名单并说明理由。"
    )


def test_no_shell_or_dynamic_code_execution() -> None:
    """无代码执行面(DESIGN §9)。

    这是非目标,不是暂缓。唯一允许的代码执行面是 M2 的受限沙箱容器,
    而沙箱是靠容器隔离实现的,我们自己的进程里不该出现任何 shell 或动态执行。
    """
    banned_imports = {"subprocess", "pty"}
    banned_calls = {"eval", "exec", "compile"}
    banned_attrs = {("os", "system"), ("os", "popen"), ("os", "execv")}
    offenders: list[str] = []

    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in banned_imports:
                        offenders.append(f"{path}:{node.lineno} import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.split(".")[0] in banned_imports:
                    offenders.append(f"{path}:{node.lineno} from {node.module}")
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in banned_calls:
                    offenders.append(f"{path}:{node.lineno} {func.id}()")
                elif (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and (func.value.id, func.attr) in banned_attrs
                ):
                    offenders.append(f"{path}:{node.lineno} {func.value.id}.{func.attr}()")

    assert offenders == [], f"系统内不得存在 shell 或动态代码执行:{offenders}"


def test_only_db_opens_a_sqlite_connection() -> None:
    """CONVENTIONS D3:连接一律从 `db.connect` / `db.open_connection` 拿。

    理由是**并发**,不是整洁:同步工具函数跑在**框架给的线程池**里(FastMCP、
    Pydantic AI 各自决定怎么调度,那不是我们能选的),一条 assistant 消息里的多个
    工具调用因此是并发执行的。`check_same_thread=False` 关掉的只是那个守卫,不是让
    连接变线程安全——串行化靠 `db.GuardedConnection` 那把锁,而它**只有一份**。

    自己 `sqlite3.connect` 就是绕过它,而症状一点都不像并发问题:M5-8 之前两个 bundle
    各写了一份裸 connect,memory 那条的面孔是 `KeyError: 提案不存在`——写进去了转头
    读不回来,**账本唯一写入路径上的数据面失败**。三分之一的并发批次中招,查无线索。

    那两处当时不是谁偷懒,是这条规则还没写下来——所以它现在既在 CONVENTIONS 里,
    也在这里钉着:下一个 bundle 不用再靠自觉。

    **三扇门都要守。** 第一版只认字面的 `sqlite3.connect(...)`,而
    `import sqlite3 as _sq` 和 `from sqlite3 import connect` 从旁边大摇大摆走过去
    ——实测两种写法同时出现,测试一声不响地通过。守住一扇门的门禁比没有更坏:
    它让人以为这条规则是机械保证的。

    **不能改成"禁止 import sqlite3"**:`sqlite3.Connection` / `sqlite3.Row` 到处在做
    类型标注,那样会误伤一大片。要认的是**调用**,不是 import。
    """
    allowed = {Path("src/lararium/db.py")}  # 唯一允许建连接的地方,那把锁在它手里
    offenders: list[str] = []

    for path in _source_files():
        if path in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders.extend(f"{path}:{where}" for where in _sqlite_connect_calls(tree))

    assert offenders == [], (
        f"这些地方自己建了 sqlite 连接,绕过了 db 里那把串行化的锁:{offenders}。"
        "走 db.connect(Steward 的库,含建表)或 db.open_connection(自己有库的模块)。"
    )


def _sqlite_connect_calls(tree: ast.AST) -> list[str]:
    """一个文件里所有"自己建 sqlite 连接"的地方。**三扇门一起认。**

    别名要跟着记,否则 `import sqlite3 as _sq` 就是一扇没人看的门。
    """
    modules: set[str] = set()
    bare: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.asname or a.name for a in node.names if a.name == "sqlite3")
        elif isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
            bare.update(a.asname or a.name for a in node.names if a.name == "connect")

    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "connect"
            and isinstance(func.value, ast.Name)
            and func.value.id in modules
        ):
            found.append(f"{node.lineno} {func.value.id}.connect()")
        elif isinstance(func, ast.Name) and func.id in bare:
            found.append(f"{node.lineno} {func.id}()  # from sqlite3 import connect")
    return found


@pytest.mark.parametrize(
    "source",
    [
        "import sqlite3\nsqlite3.connect('x')",
        "import sqlite3 as _sq\n_sq.connect('x')",
        "from sqlite3 import connect\nconnect('x')",
        "from sqlite3 import connect as _c\n_c('x')",
    ],
)
def test_the_sqlite_guard_catches_every_door(source: str) -> None:
    """★ 给门禁自己的阳性对照:**四种写法一个都不许漏。**

    第一版只认字面的 `sqlite3.connect(...)`,而别名与 `from ... import` 从旁边走过去
    ——实测两种同时出现,门禁一声不响地通过。**守住一扇门的门禁比没有更坏**:
    它让人以为这条规则是机械保证的,于是再没人去看。
    """
    assert _sqlite_connect_calls(ast.parse(source)), f"这扇门没守住:{source!r}"


def test_the_sqlite_guard_does_not_fire_on_type_annotations() -> None:
    """反向:不许误伤。`sqlite3.Connection` / `sqlite3.Row` 到处在做类型标注,
    所以认的是**调用**不是 import——改成"禁止 import sqlite3"会一片红。"""
    source = "import sqlite3\ndef f(c: sqlite3.Connection) -> sqlite3.Row: ...\nx = sqlite3.Row"

    assert _sqlite_connect_calls(ast.parse(source)) == []


def test_assembler_never_reads_the_clock() -> None:
    """时间绝不进前缀(DESIGN §4)。

    组装器一旦自己去读时钟,前缀每轮都会变,缓存从第一个字节起全部 miss。
    时间只能随信封进流水区。这条规则用"组装器里不许出现时钟调用"来守。
    """
    assembler = Path("src/lararium/steward/assembler.py")
    if not assembler.exists():
        pytest.skip("assembler.py 尚未实现(Task 9)")

    text = assembler.read_text(encoding="utf-8")
    clock_calls = [
        marker
        for marker in (
            "datetime.now",
            "datetime.today",
            "datetime.utcnow",
            "time.time",
            "time.monotonic",
            "uuid4",
        )
        if marker in text
    ]
    assert clock_calls == [], (
        f"组装器里出现了时钟/随机源 {clock_calls},前缀将每轮变化、缓存全 miss。"
        "时间戳应当由信封携带,在流水区渲染。"
    )


def test_gitignore_protects_personal_data() -> None:
    """账本和起居注装的是真实生活数据,误提交一次就永远在 git 历史里了。"""
    patterns = Path(".gitignore").read_text(encoding="utf-8").split()
    for required in ("data/", ".env", "*.sqlite"):
        assert required in patterns, f".gitignore 缺少 {required},个人数据可能被提交"


def _definitions_after_main(tree: ast.Module) -> list[str]:
    """一个模块里 `if __name__ == "__main__":` **之后**的所有顶层语句。

    判据不是"哪几种语句不许放",是**那一块之后什么都不许放**。理由见调用处:
    生产走到那一块就进了主循环,后面的字节永远不执行。枚举 def/class/赋值会漏掉
    import 和裸调用,而它们烂的方式一模一样——所以这里认"位置",不认"种类"。
    """
    guard = None
    for index, node in enumerate(tree.body):
        if isinstance(node, ast.If) and any(
            isinstance(sub, ast.Name) and sub.id == "__name__" for sub in ast.walk(node.test)
        ):
            guard = index
    if guard is None:
        return []
    return [f"{node.lineno} {type(node).__name__}" for node in tree.body[guard + 1 :]]


def test_nothing_is_defined_after_the_main_guard() -> None:
    """★ `if __name__ == "__main__":` 必须是模块最后一个顶层语句。

    **这条是真机第一天挖出来的,而 542 个测试全绿。** wechat.py 里 `_sniff` 定义在
    `asyncio.run(main())` 之后:生产是 `python -m lararium.gateway.wechat`,模块从上往下
    执行,走到那一行就进了事件循环,**后面的 def 永远不会被定义**。日志上的面孔是
    `NameError: name '_sniff' is not defined`,而它发生在图片落盘那一步——CDN 200 OK,
    然后消息被跳过,`data/media/` 一直是空的。

    **测试查不出来,因为测试是 `import` 这个模块**:不走 `__main__` 分支,整个文件
    从头跑到尾,`_sniff` 就有了。测试看到的模块和生产跑的模块,名字空间不一样。
    这是"测试通过、生产必炸"的教科书形状,单个功能测试**永远**抓不到——所以它需要
    自己的守卫,和这个文件里其它几条一个理由。

    这不是风格洁癖:`__main__` 块之后的每一个字节,在生产里都是死的。
    """
    offenders = [
        f"{path}:{where}"
        for path in _source_files()
        for where in _definitions_after_main(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )
    ]

    assert offenders == [], (
        f'这些顶层语句写在 `if __name__ == "__main__":` 之后:{offenders}。'
        "生产里 `python -m` 执行到那一块就进主循环了,后面的定义**永远不会发生**,"
        "用到它的地方会在真机上抛 NameError。测试 import 模块时反而是好的,所以只有"
        "真机能发现。把它们挪到 `__main__` 块之前。"
    )


@pytest.mark.parametrize(
    "tail",
    [
        "def f(): ...",
        "async def f(): ...",
        "class C: ...",
        "X = 1",
        "X: int = 1",
        "import os",
        "from os import path",
        "print('hi')",
    ],
)
def test_the_main_guard_rule_catches_every_shape(tail: str) -> None:
    """给门禁自己的阳性对照:**八种写法一个都不许漏。**

    枚举种类的门禁必然漏(sqlite 那条第一版就漏了三扇门中的两扇),所以这条认位置。
    这几个用例钉的正是"认位置"这个机制:任何一种都必须被抓到。
    """
    source = f"def used(): ...\nif __name__ == '__main__':\n    used()\n{tail}"

    assert _definitions_after_main(ast.parse(source)), f"这一种没抓到:{tail!r}"


def test_the_main_guard_rule_does_not_fire_on_correct_modules() -> None:
    """反向:不许误伤。定义在前、`__main__` 收尾是正确写法;没有 `__main__` 的模块
    (库模块,仓库里绝大多数)整份都不受这条约束。"""
    correct = "def f(): ...\nX = 1\nif __name__ == '__main__':\n    f()"
    library = "def f(): ...\nX = 1\nclass C: ...\n"

    assert _definitions_after_main(ast.parse(correct)) == []
    assert _definitions_after_main(ast.parse(library)) == []
