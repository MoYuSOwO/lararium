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
        found: list[BundleInfo] = []
        for manifest_path in sorted(Path(bundles_dir).glob("*/manifest.yaml")):
            data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
            found.append(
                BundleInfo(
                    name=data["name"],
                    description=data["description"],
                    skills=tuple(SkillInfo(s["name"], s["desc"]) for s in data.get("skills", [])),
                    tools=tuple(data.get("tools", [])),
                    root=manifest_path.parent,
                )
            )
        return cls(sorted(found, key=lambda b: b.name))

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
