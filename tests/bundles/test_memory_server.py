import pytest
from bundles.memory.server import (
    build_memory_components,
    memory_tool_functions,
    read_ledger,
)


@pytest.fixture
def components(tmp_path):
    return build_memory_components(tmp_path)


def test_build_creates_ledger_and_gate(components, tmp_path):
    _ledger, gate = components
    assert (tmp_path / "memory" / "ledger.md").exists()
    assert gate.pending() == []


def test_read_ledger_returns_full_text(components, tmp_path):
    _ledger, gate = components
    gate.propose(
        kind="add",
        content="对芒果过敏",
        provenance="user_stated",
        origin="test",
        section="长期偏好",
    )
    gate.settle()
    assert "对芒果过敏" in read_ledger(tmp_path)


def test_tool_functions_have_fixed_order(components):
    """工具 schema 是前缀第0层,顺序变了每次启动都毁缓存。"""
    _, gate = components
    names = [f.__name__ for f in memory_tool_functions(gate)]
    assert names == ["propose_fact", "list_pending"]


def test_approval_is_not_reachable_from_the_model(components):
    """门控防的是被注入的模型。审批若是模型可调的工具,连调 propose+approve 即可绕过。"""
    _, gate = components
    exposed = {f.__name__ for f in memory_tool_functions(gate)}
    for forbidden in ("resolve_proposal", "settle_ledger", "rollback_ledger"):
        assert forbidden not in exposed


def test_propose_fact_tool_writes_through_gate(components):
    _ledger, gate = components
    propose_fact = memory_tool_functions(gate)[0]
    result = propose_fact("add", "对芒果过敏", "user_stated", section="长期偏好")
    assert "已记下" in result
    assert gate.unsettled_count() == 1


def test_propose_fact_tool_reports_bad_input_instead_of_crashing(components):
    """工具报错要让模型能自我纠正,不能把整轮炸掉。"""
    _, gate = components
    propose_fact = memory_tool_functions(gate)[0]
    result = propose_fact("add", "缺小节", "user_stated")
    assert "提案被拒绝" in result


def test_untrusted_proposal_tool_reports_pending(components):
    _, gate = components
    propose_fact = memory_tool_functions(gate)[0]
    result = propose_fact("add", "外部来的", "untrusted", section="身份")
    assert "待审" in result
    assert gate.unsettled_count() == 0


def test_manifest_tools_match_implementation(components):
    """manifest 声明的工具集必须与实现一致,否则前缀目录会撒谎。"""
    from pathlib import Path

    import yaml

    _, gate = components
    manifest = yaml.safe_load(Path("bundles/memory/manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == "memory"
    assert manifest["tools"] == [f.__name__ for f in memory_tool_functions(gate)]
    assert {"name", "desc"} <= set(manifest["skills"][0])


def test_skill_files_referenced_in_manifest_exist():
    from pathlib import Path

    import yaml

    root = Path("bundles/memory")
    manifest = yaml.safe_load((root / "manifest.yaml").read_text(encoding="utf-8"))
    assert (root / "skills" / "SKILL.md").exists()
    for skill in manifest["skills"]:
        assert (root / "skills" / f"{skill['name']}.md").exists()
