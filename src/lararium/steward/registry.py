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
                root=path.parent,
            )
        except (KeyError, TypeError, yaml.YAMLError) as exc:
            raise ValueError(f"{path} 不是合法的 bundle manifest:{exc}") from exc

    def directory_lines(self) -> str:
        """前缀第1层的目录部分。排序确定,内容不含时间——字节稳定。"""
        lines = []
        for b in self.bundles:
            skills = " / ".join(f"{s.name}({s.desc})" for s in b.skills)
            suffix = f" [skills: {skills}]" if skills else ""
            lines.append(f"- {b.name}:{b.description}{suffix}")
        return "\n".join(lines)

    def get(self, bundle: str) -> BundleInfo:
        if bundle not in self._by_name:
            raise KeyError(f"没有这个 bundle: {bundle};已注册: {sorted(self._by_name)}")
        return self._by_name[bundle]

    def read_skill(self, bundle: str, skill: str | None = None) -> str:
        info = self.get(bundle)
        if skill is None:
            return (info.root / "skills" / "SKILL.md").read_text(encoding="utf-8")
        if skill not in {s.name for s in info.skills}:
            raise KeyError(
                f"{bundle} 没有这个 skill: {skill};可用: {[s.name for s in info.skills]}"
            )
        return (info.root / "skills" / f"{skill}.md").read_text(encoding="utf-8")
