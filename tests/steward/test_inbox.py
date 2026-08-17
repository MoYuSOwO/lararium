from datetime import UTC, datetime, timedelta

import pytest

from lararium.db import connect
from lararium.envelope import Envelope
from lararium.steward.inbox import Inbox


@pytest.fixture
def inbox(tmp_path):
    return Inbox(connect(tmp_path / "steward.sqlite"))


def test_put_then_claim_returns_same_envelope(inbox):
    env = Envelope.new(source="user", channel="cli", content="你好")
    inbox.put(env)
    claimed = inbox.claim_next()
    assert claimed is not None
    assert claimed.id == env.id
    assert claimed.content == "你好"
    assert claimed.ts == env.ts


def test_claim_is_strictly_serial(inbox):
    """有一条在处理中时,不许认领第二条——这是可重放的前提。"""
    first = Envelope.new(source="user", channel="cli", content="第一条")
    second = Envelope.new(source="user", channel="cli", content="第二条")
    inbox.put(first)
    inbox.put(second)

    assert inbox.claim_next().id == first.id
    assert inbox.claim_next() is None  # first 还在 processing

    inbox.complete(first.id)
    assert inbox.claim_next().id == second.id


def test_claim_order_is_oldest_first(inbox):
    older = Envelope.new(source="cron", channel="scheduler", content="早")
    newer = Envelope.new(source="user", channel="cli", content="晚")
    older.ts = datetime.now(UTC) - timedelta(hours=1)
    inbox.put(newer)
    inbox.put(older)
    assert inbox.claim_next().content == "早"


def test_claim_returns_none_when_empty(inbox):
    assert inbox.claim_next() is None


def test_fail_marks_envelope_and_unblocks_queue(inbox):
    env = Envelope.new(source="user", channel="cli", content="炸了")
    nxt = Envelope.new(source="user", channel="cli", content="下一条")
    inbox.put(env)
    inbox.put(nxt)
    inbox.claim_next()
    inbox.fail(env.id, "boom")
    assert inbox.claim_next().id == nxt.id


def test_meta_roundtrips_as_json(inbox):
    env = Envelope.new(
        source="module_event",
        channel="finance",
        content="异常支出",
        meta={"event": "unusual_expense", "amount": 3000},
    )
    inbox.put(env)
    assert inbox.claim_next().meta == {"event": "unusual_expense", "amount": 3000}


def test_recover_stale_requeues_interrupted_envelope(tmp_path):
    """进程崩在处理途中,重启后队列不能永久卡死。"""
    db = tmp_path / "steward.sqlite"
    before_crash = Inbox(connect(db))
    env = Envelope.new(source="user", channel="cli", content="崩之前这条")
    before_crash.put(env)
    before_crash.claim_next()  # 认领后"崩溃",既没 complete 也没 fail

    restarted = Inbox(connect(db))
    assert restarted.claim_next() is None  # 遗留的 processing 把队列堵死了
    assert restarted.recover_stale() == (1, 0)
    claimed = restarted.claim_next()
    assert claimed is not None and claimed.id == env.id


def test_recover_stale_abandons_poison_message(inbox):
    """反复崩在同一条消息上就别再重试了,否则每次启动都崩一次。"""
    env = Envelope.new(source="user", channel="cli", content="毒消息")
    inbox.put(env)

    inbox.claim_next()
    assert inbox.recover_stale(max_attempts=2) == (1, 0)  # 第一次崩:重排队
    inbox.claim_next()
    assert inbox.recover_stale(max_attempts=2) == (0, 1)  # 第二次崩:放弃
    assert inbox.claim_next() is None
    assert inbox.pending_count() == 0


def test_recover_stale_is_noop_on_clean_start(inbox):
    inbox.put(Envelope.new(source="user", channel="cli", content="正常的"))
    assert inbox.recover_stale() == (0, 0)
    assert inbox.claim_next() is not None
