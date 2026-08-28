"""人设(用户的)与纪律(系统的)分开存、拼起来当前缀第 1 层。

**为什么要拆**:原来两者混在 `prompts/persona.md` 一个文件里——上半截是人设(语气、
相处方式),下半截「硬性纪律」装着几个里程碑打出来的东西:没读过正文不许照着干活
(M4-2)、说"记好了"之前先真的把工具调了(M4-5c)、流水不进账本(M4-5)、propose 门控
与变化频率判据(M3-7)。**用户为改语气去编辑那个文件,极易连下半截一起重写,
而那不会有任何报错**,只会在某天发现账本里全是午饭。

拆完:
- 人设 → `{data_dir}/character.md`,**用户的**,不进仓库(否则每次 git pull 都冲突);
  不存在时用内置默认 `prompts/character.default.md`。
- 纪律 → `prompts/discipline.md`,**系统的**,是代码的一部分,跟着仓库走。

**人设只能改文件,不能靠对话改**——这是硬约束,不是偏好。两条理由:
(a) 前缀是缓存命中的命根子,模型可控写入 = 每轮都可能重建;
(b) **模型可控写入前缀 = 提示注入直通车**。P0-1 那个洞最多污染一轮,人设被改是
    **之后每一轮都听新的**,是同一个洞的升级版。
用户在对话里说「以后活泼点」怎么办?走**已有机制**:那是关于他的长期偏好,
`propose_fact` 进账本、过门控、他点头才生效——账本本来就每轮注入前缀,效果一样而且有闸门。
**不要为此新增任何工具。** `tests/test_persona.py` 有一条遍历 `all_tools()` 的断言钉着。
"""

import hashlib
import os
import sqlite3
from pathlib import Path

# 人设的软上限。它每轮都在前缀里付钱,但**超了只警告不拒绝**——用户自己的机器,用户做主。
MAX_CHARACTER_CHARS = 2000

_DEFAULT_CHARACTER = Path("prompts/character.default.md")
_DISCIPLINE = Path("prompts/discipline.md")


def character_path(data_dir: Path) -> Path:
    """用户人设文件的位置。`LARARIUM_CHARACTER_PATH` 可覆盖(换机器/多人格时用)。"""
    override = os.environ.get("LARARIUM_CHARACTER_PATH")
    return Path(override) if override else Path(data_dir) / "character.md"


def load_discipline() -> str:
    """系统纪律正文。**没有回退**:它是代码的一部分,缺了就是安装坏了,应当直接炸
    ——静默少一段纪律,表现是账本某天开始进午饭,没有任何报错指向这里。"""
    return _DISCIPLINE.read_text(encoding="utf-8")


def assemble_persona(data_dir: Path) -> tuple[str, list[str]]:
    """拼出前缀第 1 层的人格部分,返回 (正文, 警告列表)。

    顺序**写死**:人设在前、纪律在后。先说你是谁,再说规矩;规矩靠后也更贴近后文。
    人设缺失或全空 → 用内置默认;**无论如何纪律都在**。
    """
    warnings: list[str] = []
    path = character_path(data_dir)
    try:
        character = path.read_text(encoding="utf-8")
    except OSError:
        character = ""
    if not character.strip():
        character = _DEFAULT_CHARACTER.read_text(encoding="utf-8")
    if len(character) > MAX_CHARACTER_CHARS:
        warnings.append(
            f"人设 {path} 有 {len(character)} 字,超过软上限 {MAX_CHARACTER_CHARS}"
            f"——它每轮都在前缀里付钱。不拒绝,但你自己知道就好。"
        )
    return f"{character.rstrip()}\n\n{load_discipline().strip()}\n", warnings


def prefix_digest(*parts: str) -> str:
    """前缀区的指纹。用来回答"它什么时候变过"——见 `record_prefix_change`。"""
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def record_prefix_change(conn: sqlite3.Connection, digest: str) -> str | None:
    """启动时把前缀指纹和上次比,变了就记一条并返回上一个指纹;没变返回 None。

    **独立有价值,不只服务人设**:改了人设、缓存命中从 90% 掉到 0,现在**没有任何地方
    说得清为什么**。「缓存命中是设计约束不是优化项」(不可协商第 1 条),那前缀什么时候
    变过就必须查得出来——注册表变更、账本结算、人设改动,每一次都该留下时间戳。
    """
    cur = conn.execute("SELECT digest FROM prefix_log ORDER BY seq DESC LIMIT 1").fetchone()
    previous = cur["digest"] if cur else None
    if previous == digest:
        return None
    conn.execute(
        "INSERT INTO prefix_log (digest, changed_at) VALUES (?, datetime('now'))", (digest,)
    )
    return previous
