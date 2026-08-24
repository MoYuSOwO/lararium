"""M4-5d:可重试失败后重跑整轮,已经成功的工具**不许再执行一次**。

`inbox.release()` 把信封放回 pending,下次 claim 整轮从头跑;而失败那轮没有 reply、
不进 L0,重试时模型对上一次的成功一无所知。于是:用户说了一笔,库里两条;
propose 一次,账本两行——而账本是前缀区、每轮全量注入,`max_attempts=3`
能把一条记成三份。

**不走"有副作用就不重试"**:把可重试误判成终态是消息永久丢失,那个不对称是有意的
(`model.py` 的分类注释)。这里走的是:**按顺序回放上一次已成功的工具结果,
只从断点之后开始真执行。**

**为什么不能按 (工具名, 参数) 去重**:用户真在一轮里报两笔一模一样的 45 元午饭是合法的,
去重会把第二笔吃掉。按顺序回放 + 断点续跑没有这个假阳性——有一条反向测试钉住。
"""

import sqlite3
from pathlib import Path

import pytest
from bundles.finance.server import build as build_finance
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
from lararium.steward.threads import Threads


class ToolCallingModel:
    """真的去调它拿到的工具函数(框架就是这么干的),然后可以抛一个可重试错。

    `FakeModel` 只按剧本返回文本、从不碰工具,测不出副作用。要复现"工具已经执行、
    随后的模型请求才失败",必须有个真会调工具的假模型。
    """

    def __init__(self, script: list[list[tuple[str, tuple, dict]]], fail_on: set[int]) -> None:
        self._script = script
        self._fail_on = fail_on
        self.attempts = 0

    async def run(self, ctx, tools, mcp_servers):
        by_name = {f.__name__: f for f in tools}
        calls = self._script[min(self.attempts, len(self._script) - 1)]
        self.attempts += 1
        events = []
        for name, args, kwargs in calls:
            result = by_name[name](*args, **kwargs)
            events.append({"type": "tool_call", "tool": name, "args": kwargs, "tool_call_id": name})
            events.append(
                {"type": "tool_result", "tool": name, "content": result, "tool_call_id": name}
            )
        if self.attempts in self._fail_on:
            raise ModelCallError("503 假装限流", retryable=True)
        return ModelReply(text="记好了。", tool_events=events)


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setenv("LARARIUM_API_KEY", "sk-test")
    monkeypatch.setenv("LARARIUM_DATA_DIR", str(tmp_path))
    settings = Settings.load()
    conn = connect(tmp_path / "steward.sqlite")
    ledger, gate = build_memory_components(tmp_path)

    def make(script, fail_on):
        model = ToolCallingModel(script, fail_on)
        steward = Steward(
            settings=settings,
            inbox=Inbox(conn),
            journal=Journal(conn),
            registry=Registry.load(Path("bundles")),
            ledger=ledger,
            gate=gate,
            model=model,
            persona="你是 Lararium。",
            outbox=Outbox(conn),
            threads=Threads(conn),
            bundle_tools=[
                *memory_tool_functions(gate),
                *build_finance(tmp_path, timezone="Asia/Shanghai").tools,
            ],
        )
        return steward, model, gate, ledger

    return make


def rows(tmp_path):
    conn = sqlite3.connect(tmp_path / "finance" / "finance.sqlite")
    try:
        return list(conn.execute("SELECT amount_cents, occurred_at FROM expenses"))
    finally:
        conn.close()


LUNCH = (
    "record_expense",
    (),
    {"amount": 45, "category": "餐饮", "occurred_at": "2026-08-23T12:00"},
)
ALLERGY = (
    "propose_fact",
    (),
    {"kind": "add", "content": "对花生过敏", "provenance": "user_stated", "section": "长期偏好"},
)


async def test_a_retried_turn_does_not_record_the_expense_twice(system, tmp_path):
    """★ 复现探针:第一轮真调了工具再抛可重试错,第二轮整轮重跑 —— 库里必须只有一条。"""
    steward, model, _gate, _ledger = system([[LUNCH]], fail_on={1})
    steward.submit(Envelope.new(source="user", channel="cli", content="午饭 45"))

    first = await steward.process_next()
    second = await steward.process_next()

    assert (first.kind, second.kind) == ("retry_later", "replied")
    assert model.attempts == 2, "模型确实被调了两次(是重跑,不是没跑)"
    assert len(rows(tmp_path)) == 1, f"用户只说了一笔,库里却有 {len(rows(tmp_path))} 条"


async def test_a_retried_turn_does_not_double_the_ledger(system, tmp_path):
    """账本更糟:它是前缀区、每轮全量注入,一条重复就是永久的。"""
    steward, _model, gate, ledger = system([[ALLERGY]], fail_on={1})
    steward.submit(Envelope.new(source="user", channel="cli", content="我对花生过敏"))

    await steward.process_next()
    await steward.process_next()
    gate.settle()

    assert ledger.read().count("对花生过敏") == 1, ledger.read()


async def test_two_identical_expenses_in_one_turn_are_both_kept(system, tmp_path):
    """反向:用户真报两笔一模一样的午饭,两笔都要在——别修出个假阳性。"""
    steward, _model, _gate, _ledger = system([[LUNCH, LUNCH]], fail_on=set())
    steward.submit(Envelope.new(source="user", channel="cli", content="今天吃了两顿,各 45"))

    await steward.process_next()

    assert len(rows(tmp_path)) == 2


async def test_two_identical_expenses_survive_a_retry_without_duplicating(system, tmp_path):
    """两笔相同 + 重试:回放是**按顺序**的,所以两笔都在,而且不多不少。

    这条是上面那条和复现探针的交叉点——按 (工具名, 参数) 去重会在这里塌掉。
    """
    steward, _model, _gate, _ledger = system([[LUNCH, LUNCH]], fail_on={1})
    steward.submit(Envelope.new(source="user", channel="cli", content="今天吃了两顿,各 45"))

    await steward.process_next()
    await steward.process_next()

    assert len(rows(tmp_path)) == 2


async def test_a_diverging_retry_executes_from_the_fork(system, tmp_path):
    """重试时模型改了主意:第一次调 A,重试先调 B —— 从分叉点起真执行,不许乱套回放。"""
    steward, _model, _gate, _ledger = system([[("current_time", (), {})], [LUNCH]], fail_on={1})
    steward.submit(Envelope.new(source="user", channel="cli", content="午饭 45"))

    await steward.process_next()
    await steward.process_next()

    assert len(rows(tmp_path)) == 1, "分叉之后的调用必须真执行"


async def test_execution_is_journalled_even_when_the_turn_fails(system, tmp_path):
    """失败那轮的工具执行**必须落起居注**,否则回放没有数据可依。

    这是 loop 原来的一个缺口:tool_events 是 `model.run` **成功返回之后**才记的,
    run 抛异常时,已经执行掉的工具一条都没留下——副作用发生了,记录里却没有。
    """
    steward, _model, _gate, _ledger = system([[LUNCH]], fail_on={1})
    env = Envelope.new(source="user", channel="cli", content="午饭 45")
    steward.submit(env)

    await steward.process_next()

    executed = [e for e in steward.journal.replay(env.id) if e["kind"] == "tool_executed"]
    assert [e["payload"]["tool"] for e in executed] == ["record_expense"]
    assert executed[0]["payload"]["replayed"] is False


async def test_replayed_calls_are_marked_in_the_journal(system, tmp_path):
    """回放的那次也要留痕,并标明没有真执行——查重复记账时要分得清哪次真跑了。"""
    steward, _model, _gate, _ledger = system([[LUNCH]], fail_on={1})
    env = Envelope.new(source="user", channel="cli", content="午饭 45")
    steward.submit(env)

    await steward.process_next()
    await steward.process_next()

    flags = [
        e["payload"]["replayed"]
        for e in steward.journal.replay(env.id)
        if e["kind"] == "tool_executed"
    ]
    assert flags == [False, True]


async def test_replay_follows_the_recorded_sequence_across_different_tools(system, tmp_path):
    """回放跟着**记录下来的那串顺序**走,跨不同工具也成立。

    第一轮 [current_time, record_expense] 都跑过了才失败;重试原样再来一遍,
    两次都该走回放,库里仍然只有一条。
    """
    steward, model, _gate, _ledger = system([[("current_time", (), {}), LUNCH]], fail_on={1})
    steward.submit(Envelope.new(source="user", channel="cli", content="午饭 45"))

    await steward.process_next()
    env_id = steward.journal.recent_turns(1)[0]["envelope_id"]
    await steward.process_next()

    assert model.attempts == 2
    assert len(rows(tmp_path)) == 1
    flags = [
        (e["payload"]["tool"], e["payload"]["replayed"])
        for e in steward.journal.replay(env_id)
        if e["kind"] == "tool_executed"
    ]
    assert flags == [
        ("current_time", False),
        ("record_expense", False),
        ("current_time", True),
        ("record_expense", True),
    ]


async def test_replay_falls_back_to_a_later_entry_with_the_same_name(system, tmp_path):
    """**位置优先,配不上就在剩余队列里向后按名字找**(M4-5d 补)。

    第二次尝试是从同一份上下文重新生成的,调用大体相同、顺序或参数略有漂移——
    裸 positional 在第一个对不上的位置就整段作废,于是后面每一次都真执行、每一样都重复。
    向后查找严格更优:没有任何场景比裸 positional 差,而它覆盖了这个机制真正要对付的情况。

    这里:上一轮 [propose_fact, record_expense] 都跑过了才失败,重试换成
    [record_expense, propose_fact] —— 两次都该走回放,两样都不许重复。
    """
    steward, _model, gate, ledger = system([[ALLERGY, LUNCH], [LUNCH, ALLERGY]], fail_on={1})
    steward.submit(Envelope.new(source="user", channel="cli", content="午饭 45,我对花生过敏"))

    await steward.process_next()
    await steward.process_next()
    gate.settle()

    assert len(rows(tmp_path)) == 1, "乱序重试把流水记重了"
    assert ledger.read().count("对花生过敏") == 1, ledger.read()


async def test_an_unconsumed_replay_entry_is_journalled_as_divergence(system, tmp_path):
    """分叉必须**可观测**:一轮结束时队列里还有没被消费的条目,就记一条事件。

    不改行为,只让它别是静默的——理由和"静默截断读起来和『就这些』一样"是同一条。
    真丢了一笔的那天,得有地方查。
    """
    steward, _model, _gate, _ledger = system([[("current_time", (), {})], [LUNCH]], fail_on={1})
    env = Envelope.new(source="user", channel="cli", content="午饭 45")
    steward.submit(env)

    await steward.process_next()
    await steward.process_next()

    diverged = [e for e in steward.journal.replay(env.id) if e["kind"] == "resume_diverged"]
    assert len(diverged) == 1
    assert diverged[0]["payload"]["unconsumed"] == ["current_time"]


async def test_tool_executed_records_the_arguments(system, tmp_path):
    """`tool_executed` 要带参数:审计"这一轮到底记了什么",光看 result 拼不出来。

    配对暂时不用它,但哪天要换配对口径,数据是现成的。这条 kind 不进 L0、不进检索索引。
    """
    steward, _model, _gate, _ledger = system([[LUNCH]], fail_on=set())
    env = Envelope.new(source="user", channel="cli", content="午饭 45")
    steward.submit(env)

    await steward.process_next()

    executed = next(e for e in steward.journal.replay(env.id) if e["kind"] == "tool_executed")
    assert executed["payload"]["args"]["amount"] == 45
    assert executed["payload"]["args"]["category"] == "餐饮"
