from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class SkillInfo:
    name: str
    desc: str


@dataclass(frozen=True)
class BundleInfo:
    name: str
    description: str
    skills: tuple[SkillInfo, ...]
    tools: tuple[str, ...]
    # 这些工具会真的改数据。M5-12 的留痕要用:「说了已记、这一轮却没有写工具跑过」。
    # **必须显式声明**(只读 bundle 写 `writes: []`)——漏写不会报错,只会让仪器
    # 从此不响,而一个永远不响的仪器比没有更坏。有架构门禁钉着。
    writes: tuple[str, ...]
    # 只读的那些。**存在的唯一理由是让 writes 可被机械校验**:只断言"writes 里的名字
    # 在 tools 里"挡得住填错字,挡不住**加了写工具忘了登记**——那种漏法一声不响,
    # 而表现是 `claimed_without_write` 从此对它永远不响。要求 writes ⊎ reads == tools
    # 才逼得出一个决定:新加的工具到底写不写。
    reads: tuple[str, ...]
    root: Path


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
                writes=tuple(data["writes"]),
                reads=tuple(data["reads"]),
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

    def write_tools(self) -> set[str]:
        """所有会改数据的工具名。判据来自各自的 manifest,不在主控里维护一张清单
        ——那张清单会在下一个 bundle 加工具时悄悄过期,而过期的表现是仪器不响。"""
        return {tool for b in self.bundles for tool in b.writes}

    def get(self, bundle: str) -> BundleInfo:
        if bundle not in self._by_name:
            raise KeyError(f"没有这个 bundle: {bundle};已注册: {sorted(self._by_name)}")
        return self._by_name[bundle]

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
