import asyncio
import contextlib
from pathlib import Path

import pytest
from bundles.memory.server import build_memory_components, memory_tool_functions

from lararium.config import Settings
from lararium.db import connect
from lararium.envelope import Envelope
from lararium.steward.inbox import Inbox
from lararium.steward.journal import Journal
from lararium.steward.loop import Steward
from lararium.steward.model import ModelCallError, ModelReply
from lararium.steward.outbox import Outbox
from lararium.steward.registry import Registry
from lararium.steward.worker import Worker


class ProgrammableModel:
    """按脚本逐次消费:ModelReply 就返回,Exception 就抛(达到脚本尾回"嗯")。"""

    def __init__(self, script=None) -> None:
        self._script = list(script or [])
        self.calls = 0

    async def run(self, ctx, tools, mcp_servers):
        idx = self.calls
        self.calls += 1
        item = self._script[idx] if idx < len(self._script) else ModelReply(text="嗯")
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def worker_factory(tmp_path, monkeypatch):
    def make(script=None, *, sleep=None):
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
            model=ProgrammableModel(script),
            persona="你是 Lararium。",
            outbox=Outbox(conn),
            bundle_tools=memory_tool_functions(gate),
        )
        wake = asyncio.Event()
        worker = Worker(steward, wake, sleep=sleep)
        return worker, steward, ledger, gate

    return make


@contextlib.asynccontextmanager
async def running_worker(worker):
    task = asyncio.create_task(worker.run())
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def wait_until(cond, budget=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    while loop.time() < deadline:
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("wait_until 超时")


async def test_worker_processes_messages_in_fifo_order(worker_factory):
    worker, steward, _, _ = worker_factory(
        [ModelReply(text="回复一"), ModelReply(text="回复二"), ModelReply(text="回复三")]
    )
    async with running_worker(worker):
        for text in ("问一", "问二", "问三"):
            steward.submit(Envelope.new(source="user", channel="cli", content=text))
            worker.wake.set()
        await wait_until(lambda: len(steward.outbox.take("cli", after=0)) == 3)
        replies = [i.content for i in steward.outbox.take("cli", after=0)]
        assert replies == ["回复一", "回复二", "回复三"]


async def test_worker_waits_idle_then_wakes_on_new_message(worker_factory):
    worker, steward, _, _ = worker_factory([ModelReply(text="好")])
    async with running_worker(worker):
        await asyncio.sleep(0.05)
        # 队列空,worker 应停在等待,没干活
        assert len(steward.outbox.take("cli", after=0)) == 0
        steward.submit(Envelope.new(source="user", channel="cli", content="来了"))
        worker.wake.set()
        await wait_until(lambda: len(steward.outbox.take("cli", after=0)) == 1)
        assert steward.outbox.take("cli", after=0)[0].content == "好"


async def test_poison_message_does_not_break_worker(worker_factory):
    worker, steward, _, _ = worker_factory([ValueError("毒消息炸了"), ModelReply(text="好的后续")])
    poison = Envelope.new(source="user", channel="cli", content="会炸")
    async with running_worker(worker):
        steward.submit(poison)
        worker.wake.set()
        steward.submit(Envelope.new(source="user", channel="cli", content="正常的"))
        worker.wake.set()
        # 毒消息 produce 不了回复,但后续消息必须照常处理——worker 没陪葬
        await wait_until(lambda: len(steward.outbox.take("cli", after=0)) == 1)
        assert steward.outbox.take("cli", after=0)[0].content == "好的后续"
        row = steward.inbox._conn.execute(
            "SELECT state FROM inbox WHERE id=?", (poison.id,)
        ).fetchone()
        assert row["state"] == "failed"


async def test_idle_settlement_fires_when_queue_drains(worker_factory):
    worker, steward, ledger, gate = worker_factory()

    class ProposingModel:
        """处理期间(模拟 propose_fact 工具)落一条 user_stated 提案。"""

        def __init__(self, gate):
            self.gate = gate

        async def run(self, ctx, tools, mcp_servers):
            self.gate.propose(
                kind="add",
                content="处理期间记一条",
                provenance="user_stated",
                origin="test",
                section="长期偏好",
            )
            return ModelReply(text="记下了")

    steward.model = ProposingModel(gate)
    async with running_worker(worker):
        steward.submit(Envelope.new(source="user", channel="cli", content="第一问"))
        worker.wake.set()
        # 队列清空后 worker 应自动结算,账本里出现那条事实
        await wait_until(lambda: "处理期间记一条" in ledger.read())
        assert gate.unsettled_count() == 0


async def test_retryable_failure_backs_off_between_attempts(worker_factory):
    """可重试失败(429)后按 2**attempts 退避,绝不等 wake——否则任何新消息
    都会立刻重锤那条被限流的消息(流量越大敲得越狠)。"""
    sleeps: list[float] = []

    async def fake_sleep(secs):
        sleeps.append(secs)

    worker, steward, _, _ = worker_factory(
        [
            ModelCallError("status_code: 429, rate limited", retryable=True),
            ModelReply(text="终于好了"),
        ],
        sleep=fake_sleep,
    )
    async with running_worker(worker):
        steward.submit(Envelope.new(source="user", channel="cli", content="会被限流"))
        worker.wake.set()
        await wait_until(lambda: len(steward.outbox.take("cli", after=0)) == 1)
        assert steward.outbox.take("cli", after=0)[0].content == "终于好了"
        # 第一次失败 attempts=1 → 退避 2**1=2 秒;若把它当 empty 等 wake,sleeps 为空
        assert sleeps == [2.0]
