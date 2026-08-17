import pytest

from lararium.db import connect
from lararium.steward.outbox import Outbox


@pytest.fixture
def outbox(tmp_path):
    return Outbox(connect(tmp_path / "steward.sqlite"))


def test_put_then_take_returns_item_and_scopes_to_channel(outbox):
    outbox.put("env-1", "cli", "回复一", kind="reply")
    outbox.put("env-2", "web", "回复二", kind="reply")
    outbox.put("env-3", "cli", "回复三", kind="reply")

    items = outbox.take("cli", after=0)
    assert [i.content for i in items] == ["回复一", "回复三"]
    assert all(i.channel == "cli" for i in items)


def test_take_marks_delivered_but_still_returns_item(outbox):
    """at-least-once:delivered_at 只是观测字段,不是投递保证;同一条可再取,客户端按 seq 去重。"""
    seq = outbox.put("env-1", "cli", "回复一", kind="reply")

    first = outbox.take("cli", after=0)
    assert len(first) == 1
    assert first[0].delivered_at is not None

    second = outbox.take("cli", after=0)
    assert len(second) == 1
    assert second[0].seq == seq


def test_seq_is_monotonic_and_global_across_channels(outbox):
    s1 = outbox.put("env-1", "cli", "a")
    s2 = outbox.put("env-2", "web", "b")
    s3 = outbox.put("env-3", "cli", "c")

    assert s1 < s2 < s3  # 单调递增
    assert len({s1, s2, s3}) == 3  # 全局唯一(跨渠道不冲突)

    # take(after=seq) 只取比 seq 大的
    items = outbox.take("cli", after=s1)
    assert [i.seq for i in items] == [s3]
