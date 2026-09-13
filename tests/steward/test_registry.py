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


def test_read_skill_without_name_lists_the_skills(registry):
    """M5-14:不带 skill 名 → **列出这个领域有哪些方法篇,不报错**。

    模型自然会先这么调一次;拿"读取失败"惩罚一个合理动作,它下次就绕着走,
    而绕法不可控。原来这一支读的是总览,而总览已经删了。
    """
    text = registry.read_skill("memory", None)

    assert "writing-facts" in text
    assert "什么该入账本" in text
    assert "失败" not in text


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
    lines = registry.directory_lines().splitlines()
    top = next(i for i, line in enumerate(lines) if line.startswith("- finance"))

    # M5-14:改成嵌套列表,方法篇各占一行。**这一行就是路由的全部出处**——总览没了,
    # 模型决定要不要读某篇,手里只有这句 desc。
    assert lines[top + 1].strip().startswith("* monthly-review")
    assert "怎么看一个月的账" in lines[top + 1]


def test_read_skill_rejects_unknown_skill_name_in_finance(registry):
    """白名单校验对 finance 同样生效:skill 名来自模型输出,不许拿去拼路径。"""
    with pytest.raises(KeyError, match="monthly-review"):
        registry.read_skill("finance", "../../../etc/passwd")


# ── M6-6d:工具名加 bundle 前缀 ───────────────────────────────────────────


def _cook_registry(tmp_path: Path, tools: str = "[boil, fry]") -> Registry:
    _write_bundle(tmp_path, "cook", f"name: cook\ndescription: 做饭\ntools: {tools}\n")
    return Registry.load(tmp_path)


def boil(minutes: int, salt: bool = False) -> str:
    """把东西煮 minutes 分钟。salt 为真时先放盐。"""
    return f"煮了 {minutes} 分钟" + (",放了盐" if salt else "")


def fry() -> str:
    """炒一下。"""
    return "炒好了"


def test_bundle_tools_are_prefixed_with_the_manifest_name(tmp_path):
    """★ 前缀从 manifest 的 `name` 来,调用方只说"这是哪个 bundle 的",拼不出别的前缀。"""
    registry = _cook_registry(tmp_path)

    out = registry.qualify_tools("cook", [boil, fry])

    assert [f.__name__ for f in out] == ["cook__boil", "cook__fry"]
    assert registry.get("cook").tool_name("boil") == "cook__boil"


def test_prefixing_changes_nothing_but_the_name(tmp_path):
    """签名、docstring、行为一个不动;原函数顺着 `__wrapped__` 找得回来(守卫按身份认靠这个)。"""
    import inspect

    (qualified,) = _cook_registry(tmp_path, "[boil]").qualify_tools("cook", [boil])

    assert inspect.signature(qualified) == inspect.signature(boil)
    assert inspect.getdoc(qualified) == inspect.getdoc(boil)
    assert qualified(3, salt=True) == boil(3, salt=True)
    assert inspect.unwrap(qualified) is boil
    assert boil.__name__ == "boil", "改的是包装,不许动 bundle 自己的函数对象"


@pytest.mark.parametrize(
    "handed",
    [
        pytest.param(["fry", "boil"], id="顺序不对"),
        pytest.param(["boil"], id="少一个"),
        pytest.param(["boil", "fry", "boil"], id="多一个"),
    ],
)
def test_prefixing_refuses_a_tool_list_the_manifest_does_not_declare(tmp_path, handed):
    """manifest 的 tools 是这个 bundle 有哪些工具的**唯一**声明。交出来的函数和它对不上,
    说明组装根拿错了 bundle 的名字(或者 manifest 忘了改)——前缀会贴错,必须当场炸。"""
    registry = _cook_registry(tmp_path)
    funcs = {"boil": boil, "fry": fry}

    with pytest.raises(ValueError, match="cook"):
        registry.qualify_tools("cook", [funcs[n] for n in handed])


def test_legacy_names_are_derived_from_the_manifests(registry):
    """旧名 → 新名是**推出来的**:每个 manifest 里的每个工具一条,不多不少。"""
    legacy = registry.legacy_tool_names()

    assert legacy["propose_fact"] == "memory__propose_fact"
    assert legacy["list_recent"] == "finance__list_recent"
    assert legacy["list_materials"] == "courses__list_materials"
    assert set(legacy.values()) == {b.tool_name(t) for b in registry.bundles for t in b.tools}
    assert len(legacy) == sum(len(b.tools) for b in registry.bundles)


def test_a_bare_name_two_bundles_share_is_left_out_of_the_legacy_map(tmp_path):
    """两个 bundle 各有一个 `read_note`:旧记录里那个 `read_note` 是谁的**说不清**,
    宁可认不出(那次往返照 L3 丢掉),不许猜一个塞进上下文。"""
    _write_bundle(tmp_path, "a", "name: courses\ndescription: 课\ntools: [read_note, add_file]\n")
    _write_bundle(tmp_path, "b", "name: diary\ndescription: 日记\ntools: [read_note]\n")

    legacy = Registry.load(tmp_path).legacy_tool_names()

    assert legacy == {"add_file": "courses__add_file"}
