from pathlib import Path

import pytest

from lararium.steward.registry import Registry


@pytest.fixture
def registry():
    return Registry.load(Path("bundles"))


def test_load_discovers_memory_bundle(registry):
    assert "memory" in [b.name for b in registry.bundles]


def test_load_discovers_finance_bundle(registry):
    """M4-1:扔个目录进来就被发现——目录行(前缀第1层)零改动自动列出来。"""
    assert "finance" in [b.name for b in registry.bundles]
    assert "finance" in registry.directory_lines()
    assert "记账与消费分析" in registry.directory_lines()  # 财务 bundle 的目录行(§5 示例)


def test_directory_lines_include_name_description_and_skills(registry):
    lines = registry.directory_lines()
    assert "memory" in lines
    assert "核心账本与门控写入" in lines
    assert "writing-facts" in lines


def test_directory_lines_are_deterministic(registry):
    """前缀稳定性:同样的 bundle 集合必须生成字节一致的目录。"""
    other = Registry.load(Path("bundles"))
    assert registry.directory_lines() == other.directory_lines()


def test_read_skill_without_name_returns_overview(registry):
    text = registry.read_skill("memory", None)
    assert "# memory" in text
    assert "writing-facts" in text


def test_read_skill_with_name_returns_body(registry):
    text = registry.read_skill("memory", "writing-facts")
    assert "怎么写账本条目" in text  # 钉正文标题,不钉判据条数(那是会被打磨的内容)


def test_read_skill_rejects_unknown_bundle(registry):
    # finance 在 M4-1 已注册,换一个确实不存在的名字来测"未知 bundle"分支
    with pytest.raises(KeyError, match="health"):
        registry.read_skill("health", None)


def test_read_skill_rejects_path_traversal(registry):
    """skill 名来自模型输出,必须挡住路径穿越。"""
    with pytest.raises(KeyError):
        registry.read_skill("memory", "../../../etc/passwd")


def _write_bundle(root: Path, dirname: str, manifest: str) -> None:
    (root / dirname / "skills").mkdir(parents=True)
    (root / dirname / "manifest.yaml").write_text(manifest, encoding="utf-8")
    (root / dirname / "skills" / "SKILL.md").write_text("# x", encoding="utf-8")


def test_broken_manifest_names_the_offending_file(tmp_path):
    """扔错一个 bundle 要立刻知道错在哪,不能只给一句 KeyError: 'name'。"""
    _write_bundle(tmp_path, "finance", "description: 缺了 name 字段\ntools: []\n")

    with pytest.raises(ValueError, match=r"finance/manifest\.yaml"):
        Registry.load(tmp_path)


def test_invalid_yaml_names_the_offending_file(tmp_path):
    _write_bundle(tmp_path, "health", "name: health\n  这行缩进是坏的:\n- x\n")

    with pytest.raises(ValueError, match=r"health/manifest\.yaml"):
        Registry.load(tmp_path)


def test_duplicate_bundle_names_are_rejected(tmp_path):
    """名字是路由依据。重名时目录行会列出两个,但只有一个调得到——必须拒绝。"""
    _write_bundle(
        tmp_path, "a", "name: finance\ndescription: 甲\ntools: []\nwrites: []\nreads: []\n"
    )
    _write_bundle(
        tmp_path, "b", "name: finance\ndescription: 乙\ntools: []\nwrites: []\nreads: []\n"
    )

    with pytest.raises(ValueError, match="重名"):
        Registry.load(tmp_path)


def test_every_registered_bundle_has_a_readable_overview(registry):
    """每个注册进来的 bundle 都必须有能读到的总览(M4-1 登记一的结构性一半)。

    persona 的路由规则是「动手做某个领域的事之前先 read_skill 读总览」——这条规矩
    只有在总览**确实存在**时才兑现得了。finance 是第一个撞上的:它的要点
    (别把流水记进账本)只写在 SKILL.md 里,`skills: []` 让目录行连个方法名都不列,
    总览要是再缺失,那段话就成了谁也走不到的正文。
    """
    for bundle in registry.bundles:
        text = registry.read_skill(bundle.name)
        assert text.strip(), f"{bundle.name} 的 SKILL.md 是空的——总览不可达"


def test_finance_directory_line_lists_monthly_review(registry):
    """M4-4:manifest.skills 加了 monthly-review,目录行(前缀第1层)随之列出它。

    这是本里程碑第二次、也是最后一次目录行变动——D3 认可的重建点。
    """
    line = next(line for line in registry.directory_lines().splitlines() if "finance" in line)
    assert "monthly-review" in line


def test_read_skill_rejects_unknown_skill_name_in_finance(registry):
    """白名单校验对 finance 同样生效:skill 名来自模型输出,不许拿去拼路径。"""
    with pytest.raises(KeyError, match="monthly-review"):
        registry.read_skill("finance", "../../../etc/passwd")


def test_a_manifest_without_writes_is_rejected_loudly(tmp_path):
    """`writes:` 是必填的,哪怕是空的(M5-12)。

    给个默认值看着更friendly,代价是:一个忘了声明的 bundle 会照常装上,而
    `claimed_without_write` 那条留痕**从此对它永远不响**——没有任何报错,
    只是仪器悄悄哑了一块。宁可在装载时炸,而且要说清是哪个文件缺哪个字段。
    只读 bundle 写 `writes: []` 就行,一行的事。
    """
    _write_bundle(
        tmp_path, "x", "name: x\ndescription: 缺了 writes\ntools: [peek]\nreads: [peek]\n"
    )

    with pytest.raises(ValueError, match="writes"):
        Registry.load(tmp_path)


def test_write_tools_come_from_the_manifests(tmp_path):
    """哪些工具会写,判据在各自的 manifest,不在主控里维护一张清单
    ——那张清单会在下一个 bundle 加工具时悄悄过期,而过期的表现是仪器不响。"""
    reg = Registry.load(Path("bundles"))

    assert reg.write_tools() == {"propose_fact", "record_expense"}


def test_a_manifest_without_reads_is_rejected_loudly(tmp_path):
    """`reads:` 和 `writes:` 一样必填(M5-12 补)。

    给默认值的话,划分那条门禁就自动放行了——`tools - writes - reads` 恒为空,
    而那正是"加了写工具忘了登记"要被挡住的地方。两边都要求填,才逼得出那个决定。
    """
    _write_bundle(tmp_path, "x", "name: x\ndescription: 缺了 reads\ntools: [peek]\nwrites: []\n")

    with pytest.raises(ValueError, match="reads"):
        Registry.load(tmp_path)
