import functools
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# M6-6d:bundle 工具名 = `<manifest 的 name>__<原工具名>`。双下划线:单下划线分不清边界
# (`web_search` 自己就带一个),点号 OpenAI 系函数名不许用;和 MCP 客户端的
# `mcp__server__tool` 同形。**这条规则只在这个文件里写一次**——组装根加前缀、回放认旧名、
# 文字检查推"能调得到的名字",都从这里取。
TOOL_NAME_SEPARATOR = "__"


@dataclass(frozen=True)
class SkillInfo:
    name: str
    desc: str


@dataclass(frozen=True)
class BundleInfo:
    name: str
    description: str
    skills: tuple[SkillInfo, ...]
    # manifest 里写的是**裸名**(bundle 自己的函数叫什么)。前缀只在 `name` 那一处写一次,
    # 在注册那一刻加上——写成前缀名的话同一个前缀要手抄十遍,还可能和 `name` 对不上。
    tools: tuple[str, ...]
    root: Path

    def tool_name(self, tool: str) -> str:
        """模型看到、调用的那个名字。"""
        return f"{self.name}{TOOL_NAME_SEPARATOR}{tool}"


def _renamed(fn: Callable[..., Any], name: str) -> Callable[..., Any]:
    """换一个名字,别的一个字节不动:`functools.wraps` 带过签名与 docstring(工具 schema 就是它俩),
    `__wrapped__` 让守卫能顺着找回原函数(见 `Steward.all_tools`)。

    **包一层而不是改原函数的 `__name__`**:函数对象是 bundle 的,`create_server()` 那条路上
    MCP 要的是裸名(命名空间是客户端的事),改了原对象两边就互相踩。
    """

    @functools.wraps(fn)
    def renamed(*args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    renamed.__name__ = name
    return renamed


class Registry:
    def __init__(self, bundles: list[BundleInfo]) -> None:
        self.bundles = bundles
        self._by_name = {b.name: b for b in bundles}

    @classmethod
    def load(cls, bundles_dir: Path) -> "Registry":
        found = [cls._parse_manifest(p) for p in sorted(Path(bundles_dir).glob("*/manifest.yaml"))]
        names = [b.name for b in found]
        duplicated = sorted({n for n in names if names.count(n) > 1})
        if duplicated:
            raise ValueError(
                f"bundle 重名: {duplicated}。名字是路由依据,重名会让其中一个永远调不到,"
                f"但目录行里还照样列着——必须唯一。"
            )
        return cls(sorted(found, key=lambda b: b.name))

    @staticmethod
    def _parse_manifest(path: Path) -> BundleInfo:
        """解析失败必须说清是哪个文件。「扔个目录进去就能用」是 bundle 系统的卖点,
        那么「扔错了立刻知道错在哪」就是它的下半句——否则装了五六个 bundle 之后,
        一句光秃秃的 KeyError: 'name' 只能靠逐个删目录来二分定位。"""
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            return BundleInfo(
                name=data["name"],
                description=data["description"],
                skills=tuple(SkillInfo(s["name"], s["desc"]) for s in data.get("skills", [])),
                tools=tuple(data.get("tools", [])),
                root=path.parent,
            )
        except (KeyError, TypeError, yaml.YAMLError) as exc:
            raise ValueError(f"{path} 不是合法的 bundle manifest:{exc}") from exc

    def directory_lines(self) -> str:
        """前缀第1层的目录部分。排序确定,内容不含时间——字节稳定。

        M5-14 起改成嵌套列表,而且它**就是路由的全部出处**:总览(SKILL.md)删掉了,
        模型决定"要不要读某份方法篇"手里只有下面那一行 desc。实测这样够用——不带守卫时
        记账/查账直接调工具、月度复盘主动去读 monthly-review,靠的正是这句 desc。

        **所以 desc 从此是承重的**:写 manifest 的时候要当回事,它不再是一句注解。
        """
        lines = []
        for b in self.bundles:
            lines.append(f"- {b.name} —— {b.description}")
            lines.extend(f"    * {s.name}    {s.desc}" for s in b.skills)
        return "\n".join(lines)

    def get(self, bundle: str) -> BundleInfo:
        if bundle not in self._by_name:
            raise KeyError(f"没有这个 bundle: {bundle};已注册: {sorted(self._by_name)}")
        return self._by_name[bundle]

    def qualify_tools(
        self, bundle: str, tools: Iterable[Callable[..., Any]]
    ) -> list[Callable[..., Any]]:
        """给一个 bundle 交出来的工具加上前缀(M6-6d),顺序不变。

        **前缀取自 manifest 的 `name`,调用方给的 `bundle` 只用来找 manifest**——而且找到之后
        要对账:交出来的函数名必须和 manifest 的 `tools` 逐个、按顺序相同。对不上就是组装根
        拿错了名字(前缀会贴到别人头上)或者 manifest 没跟着改(目录会撒谎),两种都当场炸。
        """
        info = self.get(bundle)
        funcs = list(tools)
        handed = tuple(getattr(f, "__name__", "") for f in funcs)
        if handed != info.tools:
            raise ValueError(
                f"{bundle} 的 manifest 声明的工具是 {list(info.tools)},交来加前缀的是 {list(handed)}"
                "——名字和顺序都得逐个对上,否则前缀会贴错,或者 manifest 在撒谎。"
            )
        return [_renamed(f, info.tool_name(f.__name__)) for f in funcs]

    def legacy_tool_names(self) -> dict[str, str]:
        """加前缀之前的名字 → 现在的名字,**从 manifest 推出来**,不手写(M6-6d)。

        读者是起居注里改名之前的那些记录:L0 回放(`Steward._recent_turns`)和断点续跑
        (`Steward.process_next`)。**删除条件**是 `Steward.legacy_tool_names_retired()`
        ——没压缩的历史里一条旧名字都没有了,这张表就没有读者。

        一个裸名要是有两个 bundle 都叫它,那条旧记录是谁的说不清——**不收**,让它像任何
        认不出的名字一样被丢掉,不猜。
        """
        owners = Counter(t for b in self.bundles for t in b.tools)
        return {t: b.tool_name(t) for b in self.bundles for t in b.tools if owners[t] == 1}

    def read_skill(self, bundle: str, skill: str | None = None) -> str:
        """不带 skill 名时列出这个领域有哪些方法篇,**不报错**(M5-14)。

        原来这一支读的是 `SKILL.md` 总览,而那份总览逐条对下来只剩两句别处没有的话,
        其余全是 docstring 和 discipline 的重复——它是一层会悄悄腐烂的文档,已经删掉。
        模型自然会先这么调一次;拿"读取失败"惩罚一个合理动作,它下次就绕着走,
        而绕法不可控。
        """
        info = self.get(bundle)
        if skill is None:
            if not info.skills:
                return f"{bundle} 没有额外的方法篇,直接用它的工具就行。"
            listed = "\n".join(f"- {s.name}:{s.desc}" for s in info.skills)
            return f'{bundle} 有这些方法篇(要哪篇就 read_skill("{bundle}", "名字")):\n{listed}'
        if skill not in {s.name for s in info.skills}:
            raise KeyError(
                f"{bundle} 没有这个 skill: {skill};可用: {[s.name for s in info.skills]}"
            )
        return (info.root / "skills" / f"{skill}.md").read_text(encoding="utf-8")
