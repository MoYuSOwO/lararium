from pathlib import Path

import pytest
from bundles.memory.server import build_memory_components

from lararium.config import Settings
from lararium.db import connect
from lararium.steward.inbox import Inbox
from lararium.steward.journal import Journal
from lararium.steward.loop import Steward
from lararium.steward.outbox import Outbox
from lararium.steward.registry import Registry


class _StubModel:
    async def run(self, ctx, tools, mcp_servers):
        raise AssertionError("命令分派不应触达模型")


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setenv("LARARIUM_API_KEY", "sk-test")
    monkeypatch.setenv("LARARIUM_DATA_DIR", str(tmp_path))
    settings = Settings.load()
    conn = connect(tmp_path / "steward.sqlite")
    ledger, gate = build_memory_components(tmp_path)
    steward = Steward(
        settings=settings,
        inbox=Inbox(conn),
        journal=Journal(conn),
        registry=Registry.load(Path("bundles")),
        ledger=ledger,
        gate=gate,
        model=_StubModel(),
        persona="P",
        outbox=Outbox(conn),
    )
    return steward, ledger, gate


def test_rollback_with_bad_argument_returns_text_not_exception(system):
    from lararium.gateway.commands import CommandResult, handle_command

    steward, ledger, gate = system
    result = handle_command("/rollback abc", steward=steward, ledger=ledger, gate=gate)
    assert isinstance(result, CommandResult)
    assert result.should_quit is False
    assert "回滚" in result.text


def test_rollback_with_unknown_snapshot_returns_text_not_exception(system):
    from lararium.gateway.commands import CommandResult, handle_command

    steward, ledger, gate = system
    result = handle_command("/rollback 999", steward=steward, ledger=ledger, gate=gate)
    assert isinstance(result, CommandResult)
    assert "快照" in result.text or "回滚" in result.text


def test_misspelled_command_is_unknown(system):
    from lararium.gateway.commands import handle_command

    steward, ledger, gate = system
    result = handle_command("/aprove x", steward=steward, ledger=ledger, gate=gate)
    assert "未知命令" in result.text


def test_approve_with_unknown_prefix_reports_zero_matches(system):
    from lararium.gateway.commands import handle_command

    steward, ledger, gate = system
    result = handle_command("/approve nope", steward=steward, ledger=ledger, gate=gate)
    assert "匹配到 0 条" in result.text


def test_quit_flag_and_settles_passed_proposals(system):
    from lararium.gateway.commands import handle_command

    steward, ledger, gate = system
    # 一条已通过未结算的提案
    gate.propose(
        kind="add",
        content="对芒果过敏",
        provenance="user_stated",
        origin="test",
        section="长期偏好",
    )
    result = handle_command("/quit", steward=steward, ledger=ledger, gate=gate)
    assert result.should_quit is True
    assert "结算" in result.text
    assert steward.settle_if_needed() == 0  # 已结算,无剩


def test_pending_reports_empty(system):
    from lararium.gateway.commands import handle_command

    steward, ledger, gate = system
    result = handle_command("/pending", steward=steward, ledger=ledger, gate=gate)
    assert "无待审" in result.text


def test_quit_still_exits_when_settlement_fails():
    """退出是用户最后的逃生口,不许被别的故障堵住。

    结算失败要报告,但 should_quit 必须仍然为真——否则 EOF 会把它变成死循环
    (EOF 永久为真 → 每轮重新映射成 /quit → 每轮再抛一次)。
    """
    from lararium.gateway.commands import handle_command

    class BoomSteward:
        def settle_if_needed(self):
            raise RuntimeError("账本文件不见了")

    result = handle_command("/quit", steward=BoomSteward(), ledger=None, gate=None)
    assert result.should_quit is True
    assert "账本文件不见了" in result.text
