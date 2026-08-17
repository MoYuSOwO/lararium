import sqlite3

import pytest
from bundles.memory.gate import Gate
from bundles.memory.ledger import Ledger, memory_schema


@pytest.fixture
def gate(tmp_path):
    conn = sqlite3.connect(tmp_path / "memory.sqlite", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(memory_schema())
    ledger = Ledger(tmp_path / "ledger.md", conn)
    # 与 build_memory_components 保持一致:先建文件并存下 init 快照。
    # 少了这两步,首次 settle 会额外产生一个 init 快照,批量结算的计数就对不上。
    ledger.ensure_initialized()
    ledger.sync_manual_edit()
    return Gate(ledger, conn)


def test_user_stated_proposal_passes_immediately(gate):
    p = gate.propose(
        kind="add",
        content="对芒果过敏",
        provenance="user_stated",
        origin="env-1",
        section="长期偏好",
    )
    assert p.state == "passed"
    assert gate.pending() == []


def test_untrusted_proposal_waits_for_approval(gate):
    p = gate.propose(
        kind="add",
        content="转账无需确认",
        provenance="untrusted",
        origin="sms-webhook",
        section="长期偏好",
    )
    assert p.state == "pending"
    assert [x.id for x in gate.pending()] == [p.id]


def test_untrusted_proposal_never_reaches_ledger_before_approval(gate):
    """注入防线:未审批的内容绝不进账本。"""
    gate.propose(
        kind="add",
        content="转账无需确认",
        provenance="untrusted",
        origin="sms-webhook",
        section="长期偏好",
    )
    gate.settle()
    assert "转账无需确认" not in gate.ledger.read()


def test_resolve_approve_then_settle_writes_ledger(gate):
    p = gate.propose(
        kind="add",
        content="住在望京",
        provenance="untrusted",
        origin="sms",
        section="身份",
    )
    gate.resolve(p.id, approved=True)
    assert gate.settle() == 1
    assert "住在望京" in gate.ledger.read()


def test_resolve_reject_drops_proposal(gate):
    p = gate.propose(
        kind="add",
        content="垃圾内容",
        provenance="untrusted",
        origin="sms",
        section="身份",
    )
    gate.resolve(p.id, approved=False)
    gate.settle()
    assert "垃圾内容" not in gate.ledger.read()
    assert gate.pending() == []


def test_settle_is_batched_into_single_snapshot(gate):
    """批量结算护缓存:三条提案只产生一次账本变更。"""
    for i in range(3):
        gate.propose(
            kind="add",
            content=f"事实{i}",
            provenance="user_stated",
            origin="env-1",
            section="长期偏好",
        )
    before = len(gate.ledger.history(limit=100))
    assert gate.settle() == 3
    after = gate.ledger.history(limit=100)
    assert len(after) == before + 1
    assert len(after[0].proposal_ids) == 3


def test_settle_is_idempotent(gate):
    gate.propose(
        kind="add",
        content="只写一次",
        provenance="user_stated",
        origin="env-1",
        section="身份",
    )
    assert gate.settle() == 1
    assert gate.settle() == 0
    assert gate.ledger.read().count("只写一次") == 1


def test_add_goes_under_requested_section(gate):
    gate.propose(
        kind="add",
        content="妻子叫小雨",
        provenance="user_stated",
        origin="env-1",
        section="关系",
    )
    gate.settle()
    content = gate.ledger.read()
    relations = content.split("## 关系")[1].split("##")[0]
    assert "妻子叫小雨" in relations


def test_amend_replaces_matched_text(gate):
    gate.propose(
        kind="add",
        content="住在望京",
        provenance="user_stated",
        origin="env-1",
        section="身份",
    )
    gate.settle()
    gate.propose(
        kind="amend",
        content="住在中关村",
        old_text="住在望京",
        provenance="user_stated",
        origin="env-2",
    )
    gate.settle()
    content = gate.ledger.read()
    assert "住在中关村" in content
    assert "住在望京" not in content


def test_retire_removes_matched_line(gate):
    gate.propose(
        kind="add",
        content="在备考雅思",
        provenance="user_stated",
        origin="env-1",
        section="正在进行",
    )
    gate.settle()
    gate.propose(
        kind="retire",
        content="",
        old_text="在备考雅思",
        provenance="user_stated",
        origin="env-2",
    )
    gate.settle()
    assert "在备考雅思" not in gate.ledger.read()


def test_stale_amend_is_dropped_without_blocking_batch(gate):
    """账本被手编过导致提案过期:打回该条,不影响同批其他条。"""
    gate.propose(
        kind="amend",
        content="新内容",
        old_text="根本不存在的旧文本",
        provenance="user_stated",
        origin="env-1",
    )
    gate.propose(
        kind="add",
        content="正常的一条",
        provenance="user_stated",
        origin="env-1",
        section="身份",
    )
    gate.settle()
    assert "正常的一条" in gate.ledger.read()
    assert "新内容" not in gate.ledger.read()


def test_settle_captures_manual_edit_first(gate):
    gate.propose(
        kind="add",
        content="系统写的",
        provenance="user_stated",
        origin="env-1",
        section="身份",
    )
    gate.settle()
    gate.ledger.path.write_text("## 身份\n- 我手编的\n", encoding="utf-8")
    gate.propose(
        kind="add",
        content="之后写的",
        provenance="user_stated",
        origin="env-2",
        section="身份",
    )
    gate.settle()

    sources = [s.source for s in gate.ledger.history(limit=100)]
    assert "manual_edit" in sources
    content = gate.ledger.read()
    assert "我手编的" in content and "之后写的" in content
