import sqlite3

import pytest
from bundles.memory.ledger import LEDGER_SECTIONS, Ledger, memory_schema


@pytest.fixture
def ledger(tmp_path):
    conn = sqlite3.connect(tmp_path / "memory.sqlite", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(memory_schema())
    return Ledger(tmp_path / "ledger.md", conn)


def test_ensure_initialized_creates_file_with_sections(ledger):
    assert ledger.ensure_initialized() is True
    content = ledger.read()
    for section in LEDGER_SECTIONS:
        assert f"## {section}" in content
    assert ledger.history()[0].source == "init"


def test_ensure_initialized_is_noop_when_file_exists(ledger):
    ledger.ensure_initialized()
    assert ledger.ensure_initialized() is False
    assert len(ledger.history()) == 1


def test_read_raises_loudly_when_file_is_missing(ledger):
    """账本丢了必须炸出来。悄悄返回空账本 = 助手静默失忆,没人会发现。"""
    ledger.ensure_initialized()
    ledger.write("## 身份\n- 对芒果过敏\n", source="approval_batch", proposal_ids=["p1"])
    ledger.path.unlink()

    with pytest.raises(FileNotFoundError, match="rollback"):
        ledger.read()
    # 历史仍在,可恢复
    assert "对芒果过敏" in ledger.history()[0].content


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


def test_r3_1_write_crash_leaves_file_old_and_no_false_manual_edit(ledger, monkeypatch):
    """R3-1:写文件写到一半抛 OSError →
    ①目标文件仍是崩前那份完整的(没截断)②重启后 sync_manual_edit 不误记成手编快照。"""
    old = "## 身份\n- 对芒果过敏\n"
    ledger.write(old, source="init", proposal_ids=[])
    assert ledger.read() == old
    new = old + "- 备考雅思\n"

    real_open = open

    def crashy_open(path, *a, **kw):
        if str(path).endswith(".tmp"):
            f = real_open(path, *a, **kw)
            orig_write = f.write  # 先抓住真写,再遮——否则 partial 里 f.write 递归自己

            def partial(data):
                orig_write(data[: len(data) // 2])  # 写一半
                raise OSError("写一半崩了")

            f.write = partial
            return f
        return real_open(path, *a, **kw)

    monkeypatch.setattr("io.open", crashy_open)  # Path.open 走 io.open,打这里才对
    with pytest.raises(OSError):
        ledger.write(new, source="approval_batch", proposal_ids=["p1"])
    monkeypatch.undo()

    # ① 目标文件仍是崩前那份完整的(没被截断)
    assert ledger.read() == old
    # 快照表已有"该写进去的那份"(latest = new)→ 可恢复可发现
    latest = ledger._conn.execute(
        "SELECT content FROM ledger_history ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert "备考雅思" in latest["content"]

    # ② "重启"(同一份文件+库)后 sync_manual_edit 不产生 manual_edit 快照——
    #    旧版文件对得上历史快照,是崩机残留,不是手编,不能被当成合法手编存进去
    fresh = Ledger(ledger.path, ledger._conn)
    assert fresh.sync_manual_edit() is False
    assert not [s for s in fresh.history() if s.source == "manual_edit"]


def test_r3_1_genuine_manual_edit_still_recorded(ledger):
    """真手编(内容匹配不上任何快照)仍要记 manual_edit——补洞不能把正经手编也咽了。"""
    ledger.ensure_initialized()
    orig = ledger.read()
    ledger.path.write_text(orig + "- 手编一条\n", encoding="utf-8")
    assert ledger.sync_manual_edit() is True
    assert any(s.source == "manual_edit" for s in ledger.history())
