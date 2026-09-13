"""M6-6d:给模型读的文字里提到的 bundle 工具,必须是**模型调得到的那个名字**。

工具注册时加了前缀(`finance__list_recent`),而回话、docstring、skill、manifest 里散着
两百多处「先用 list_recent 看一眼」。漏一处,就是在把模型指向一个不存在的工具——模型照着喊,
框架回"没有这个工具",工具重试上限是 1,这一轮就「处理失败,已放弃」。**而且没有任何
测试会因为一句话里的名字不对而红**,除非有人专门去扫。这个文件就是那个人(G8)。

扫什么:会被模型读到的文字——
- `src/lararium/` 与 `bundles/` 的 Python 源码里**所有字符串字面量**(AST 取 `Constant`
  与 f-string 的文字部分;docstring 也是字面量,工具的 docstring 就是 schema)。
  哪一句会进上下文静态判不了,所以一律算(宁可误报,同本目录 `test_architecture` 的口径);
  注释不算,它进不了任何字符串。
- `bundles/*/skills/*.md`(`read_skill` 原样返回)、`prompts/*.md`(人设、纪律、归拢)。
- `bundles/*/manifest.yaml` 里 `tools` 之外的值(描述会进前缀的目录行)。`tools` 本身写的是
  裸名——那是 bundle 在声明自己的函数叫什么,前缀在注册那一刻才加上。

判什么,**词表全部从注册表推,一个名字都不手写**(新 bundle 一装进来就在词表里):
- 裸名:某个 bundle 的原工具名,前面没有前缀;
- 调不到:长得像 `<bundle>__<工具>`,但注册表里没有这个名字(前缀写错、工具名拼错);
- 拼出来的:f-string 占位符后面直接跟 `__<工具>`——前缀是运行时拼的,这里核不了,
  也就说不准模型读到的是什么。写完整的字面量。
"""

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from lararium.steward.registry import TOOL_NAME_SEPARATOR, Registry

# f-string 的占位符在拼出来的文字里用这个字符代替:它不是标识符字符,不会和前后粘成一个名字。
_PLACEHOLDER = "\x00"
_IDENT = "A-Za-z0-9_"  # 只认 ASCII:中文在 `\w` 里,用 `\w` 的话「用list_recent」就认不出边界


@dataclass(frozen=True)
class Vocabulary:
    bare: frozenset[str]
    qualified: frozenset[str]
    bundles: frozenset[str]


def _vocabulary(registry: Registry) -> Vocabulary:
    return Vocabulary(
        bare=frozenset(t for b in registry.bundles for t in b.tools),
        qualified=frozenset(b.tool_name(t) for b in registry.bundles for t in b.tools),
        bundles=frozenset(b.name for b in registry.bundles),
    )


class _Texts(ast.NodeVisitor):
    """收一个模块里的全部字面文字,带行号。"""

    def __init__(self) -> None:
        self.found: list[tuple[int, str]] = []

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self.found.append((node.lineno, node.value))

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        # f-string **整条拼成一段**再判:文字部分原样,占位符换成一个记号。这样
        # `f"{x}__list_recent"` 看得出前缀是拼的;而文字部分不单独再收一遍(不重复报)。
        text = "".join(
            v.value if isinstance(v, ast.Constant) else _PLACEHOLDER for v in node.values
        )
        self.found.append((node.lineno, text))
        for v in node.values:
            if isinstance(v, ast.FormattedValue):
                self.visit(v)  # 占位符里还可能嵌着字面量:f"{'list_recent' if a else b}"


def _python_texts(source: str) -> list[tuple[int, str]]:
    collector = _Texts()
    collector.visit(ast.parse(source))
    return collector.found


def _manifest_texts(source: str) -> list[tuple[int, str]]:
    """manifest 里除 `tools` 之外的每个标量值。用 compose 拿节点,行号才报得出来。"""
    found: list[tuple[int, str]] = []

    def walk(node: yaml.Node) -> None:
        if isinstance(node, yaml.ScalarNode):
            found.append((node.start_mark.line + 1, str(node.value)))
        elif isinstance(node, yaml.SequenceNode):
            for item in node.value:
                walk(item)
        elif isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                if node is root and getattr(key, "value", None) == "tools":
                    continue  # 声明处:bundle 自己的函数叫什么,裸名是对的
                walk(value)

    root = yaml.compose(source)
    if root is not None:
        walk(root)
    return found


def _problems_in(text: str, vocab: Vocabulary) -> list[tuple[int, str]]:
    """(文字内的行偏移, 问题描述)。"""
    out: list[tuple[int, str]] = []

    def at(index: int) -> int:
        return text.count("\n", 0, index)

    if vocab.bare:
        names = "|".join(sorted(map(re.escape, vocab.bare), key=len, reverse=True))
        for m in re.finditer(rf"(?<![{_IDENT}{_PLACEHOLDER}])({names})(?![{_IDENT}])", text):
            out.append((at(m.start()), f"裸名 `{m.group(1)}`(模型调不到,前面要带 bundle 前缀)"))
        for m in re.finditer(rf"{_PLACEHOLDER}{TOOL_NAME_SEPARATOR}({names})(?![{_IDENT}])", text):
            out.append((at(m.start()), f"前缀是拼出来的 `{{…}}__{m.group(1)}`,写完整的名字"))
    qualified_like = rf"(?<![{_IDENT}])([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*){TOOL_NAME_SEPARATOR}([A-Za-z][{_IDENT}]*)"
    for m in re.finditer(qualified_like, text):
        prefix, tool = m.group(1), m.group(2)
        if (prefix in vocab.bundles or tool in vocab.bare) and m.group(0) not in vocab.qualified:
            out.append((at(m.start()), f"`{m.group(0)}` 不是注册过的工具名"))
    return out


def find_unreachable_tool_names(root: Path) -> list[str]:
    """扫 `root` 下会被模型读到的文字,返回 `文件:行 问题` 列表。词表由 `root/bundles` 的注册表推。"""
    vocab = _vocabulary(Registry.load(root / "bundles"))
    report: list[str] = []

    def check(path: Path, texts: list[tuple[int, str]]) -> None:
        for lineno, text in texts:
            for offset, problem in _problems_in(text, vocab):
                report.append(f"{path.relative_to(root)}:{lineno + offset} {problem}")

    for base in (root / "src" / "lararium", root / "bundles"):
        for path in sorted(base.rglob("*.py")):
            check(path, _python_texts(path.read_text(encoding="utf-8")))
    for pattern in ("bundles/*/skills/*.md", "prompts/*.md"):
        for path in sorted(root.glob(pattern)):
            lines = path.read_text(encoding="utf-8").splitlines()
            check(path, [(i, line) for i, line in enumerate(lines, 1)])
    for path in sorted(root.glob("bundles/*/manifest.yaml")):
        check(path, _manifest_texts(path.read_text(encoding="utf-8")))
    return report


def test_every_tool_name_the_model_reads_is_one_it_can_call() -> None:
    """★ 整个仓库:给模型读的文字里,bundle 工具名一律是注册过的完整名字。"""
    report = find_unreachable_tool_names(Path())
    assert not report, "这些地方把模型指向了调不到的工具名:\n" + "\n".join(report)


# ── 检查本身得咬得住(每一种漏法一条)──────────────────────────────────────


def _repo(tmp_path: Path, *, server: str = "", skill: str = "", manifest_extra: str = "") -> Path:
    """一个最小的仓库形状:一个**以后才装进来的** bundle,词表里只有它的两个工具。"""
    (tmp_path / "src" / "lararium").mkdir(parents=True)
    (tmp_path / "prompts").mkdir()
    bundle = tmp_path / "bundles" / "fitness"
    (bundle / "skills").mkdir(parents=True)
    (bundle / "manifest.yaml").write_text(
        f"name: fitness\ndescription: 运动\n{manifest_extra}tools: [log_workout, list_workouts]\n",
        encoding="utf-8",
    )
    (bundle / "server.py").write_text(server, encoding="utf-8")
    (bundle / "skills" / "plan.md").write_text(skill, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("server", "line", "says"),
    [
        pytest.param('X = "先用 log_workout 记一次"\n', 1, "裸名 `log_workout`", id="普通字面量"),
        pytest.param(
            'def f(n):\n    return f"第 {n} 次,list_workouts 看全部"\n',
            2,
            "裸名 `list_workouts`",
            id="f-string 的文字部分",
        ),
        pytest.param(
            'def log_workout():\n    """记一次。看历史用 list_workouts。"""\n',
            2,
            "裸名 `list_workouts`",
            id="docstring",
        ),
        pytest.param(
            'X = (\n    "第一行\\n"\n    "再用 log_workout"\n)\n',
            3,
            "裸名 `log_workout`",
            id="隐式拼接的第二段",
        ),
        pytest.param('X = "用 fitnes__log_workout 记"\n', 1, "不是注册过的工具名", id="前缀拼错"),
        pytest.param('X = "用 fitness__log_workut 记"\n', 1, "不是注册过的工具名", id="工具名拼错"),
        pytest.param(
            'X = "用 recipes__log_workout 记"\n', 1, "不是注册过的工具名", id="前缀是别人的"
        ),
        pytest.param(
            'def f(b):\n    return f"用 {b}__log_workout 记"\n',
            2,
            "前缀是拼出来的",
            id="前缀运行时拼",
        ),
    ],
)
def test_the_check_catches_a_new_bundle_naming_its_tool_wrong(tmp_path, server, line, says):
    """词表不是手写的:一个**今天还不存在**的 bundle 装进来,它的工具名立刻被认得。"""
    report = find_unreachable_tool_names(_repo(tmp_path, server=server))

    assert len(report) == 1, report
    assert report[0].startswith(f"bundles/fitness/server.py:{line} ")
    assert says in report[0]


def test_the_check_reads_skills_and_manifest_descriptions(tmp_path):
    root = _repo(
        tmp_path,
        skill="# 计划\n\n先 `list_workouts` 看一眼。\n",
        manifest_extra="skills:\n  - {name: plan, desc: 先用 log_workout 记下来}\n",
    )

    report = find_unreachable_tool_names(root)

    assert sorted(report) == [
        "bundles/fitness/manifest.yaml:4 裸名 `log_workout`(模型调不到,前面要带 bundle 前缀)",
        "bundles/fitness/skills/plan.md:3 裸名 `list_workouts`(模型调不到,前面要带 bundle 前缀)",
    ]


def test_the_check_leaves_correct_names_code_and_comments_alone(tmp_path):
    """阴性对照:完整的名字、函数定义本身、注释、manifest 的 tools 声明,都不报。"""
    server = (
        "def log_workout():  # log_workout 的注释不进任何字符串\n"
        '    """记一次。看历史用 fitness__list_workouts。"""\n'
        '    return f"记好了,{1} 次;fitness__list_workouts 看全部"\n'
        "\n"
        "TOOLS = [log_workout]\n"
        'DUNDER = "__name__"\n'
        'MCP = "mcp__fitness__log_workout"\n'
    )
    root = _repo(tmp_path, server=server, skill="用 `fitness__log_workout` 记。\n")

    assert find_unreachable_tool_names(root) == []
