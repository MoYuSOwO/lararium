from pathlib import Path

import pytest

from lararium.steward.registry import Registry


@pytest.fixture
def registry():
    return Registry.load(Path("bundles"))


def test_load_discovers_memory_bundle(registry):
    assert "memory" in [b.name for b in registry.bundles]


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
    assert "三个判据" in text


def test_read_skill_rejects_unknown_bundle(registry):
    with pytest.raises(KeyError, match="finance"):
        registry.read_skill("finance", None)


def test_read_skill_rejects_path_traversal(registry):
    """skill 名来自模型输出,必须挡住路径穿越。"""
    with pytest.raises(KeyError):
        registry.read_skill("memory", "../../../etc/passwd")
