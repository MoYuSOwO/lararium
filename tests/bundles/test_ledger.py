import sqlite3

import pytest
from bundles.memory.ledger import LEDGER_SECTIONS, Ledger, memory_schema


@pytest.fixture
def ledger(tmp_path):
    conn = sqlite3.connect(tmp_path / "memory.sqlite", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(memory_schema())
    return Ledger(tmp_path / "ledger.md", conn)


def test_read_creates_file_with_sections(ledger):
    content = ledger.read()
    for section in LEDGER_SECTIONS:
        assert f"## {section}" in content


def test_write_persists_and_snapshots(ledger):
    ledger.write("## 身份\n- 名字是老黄\n", source="approval_batch", proposal_ids=["p1"])
    assert "老黄" in ledger.read()
    history = ledger.history()
    assert history[0].source == "approval_batch"
    assert history[0].proposal_ids == ["p1"]


def test_sync_manual_edit_captures_out_of_band_change(ledger):
    ledger.write("## 身份\n- 原始内容\n", source="init", proposal_ids=[])
    ledger.path.write_text("## 身份\n- 我手动改的\n", encoding="utf-8")

    assert ledger.sync_manual_edit() is True
    assert ledger.history()[0].source == "manual_edit"
    assert "我手动改的" in ledger.history()[0].content


def test_sync_manual_edit_is_noop_when_unchanged(ledger):
    ledger.write("## 身份\n- 内容\n", source="init", proposal_ids=[])
    assert ledger.sync_manual_edit() is False
    assert len(ledger.history()) == 1


def test_rollback_restores_content_and_records_new_snapshot(ledger):
    first = ledger.write("## 身份\n- 版本一\n", source="init", proposal_ids=[])
    ledger.write("## 身份\n- 版本二\n", source="approval_batch", proposal_ids=["p2"])

    ledger.rollback(first)
    assert "版本一" in ledger.read()
    assert ledger.history()[0].source == "rollback"
    assert len(ledger.history()) == 3  # 回滚本身也是一次变更


def test_history_is_newest_first(ledger):
    ledger.write("## 身份\n- A\n", source="init", proposal_ids=[])
    ledger.write("## 身份\n- B\n", source="approval_batch", proposal_ids=[])
    assert "B" in ledger.history()[0].content


def test_diff_shows_changed_lines(ledger):
    a = ledger.write("## 身份\n- 旧的\n", source="init", proposal_ids=[])
    b = ledger.write("## 身份\n- 新的\n", source="approval_batch", proposal_ids=[])
    text = ledger.diff(a, b)
    assert "-- 旧的" in text or "- 旧的" in text
    assert "新的" in text
