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
