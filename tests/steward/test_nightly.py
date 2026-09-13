"""夜间归拢自动跑(M6-8)。

`/sweep` 从手动变成每天一次。真机上手动已经吃过两次亏:M5-24 那次头两天的对话一次没归拢,
M6-8 立项那天又是四天没跑。所以这里钉的是四件事:

1. **一天一次,判据在库里**:重启不重跑;整晚不在,起来补**一次**(不补好几天的份)。
2. **聊天优先**:有信封在排队或在处理,不开跑。
3. **失败有上限,环境故障不记在这一天头上**(E4);没扫完当晚接着跑,有封顶。
4. **不新增推送**:唯一的出口还是那个每天最多一条的通知器。

时间一律走假时钟,测试里不真等。"模型"是第三方那一侧(T2),其余全是真的:
真 Sweeper、真起居注、真门控、真库。**每次 `make(...)` 就是一次"进程起来"**:新连接、新对象。
"""

import json
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from bundles.memory.server import build_memory_components

from lararium.db import connect
from lararium.steward import sweep as sweep_module
from lararium.steward.journal import Journal
from lararium.steward.model import ModelCallError
from lararium.steward.nightly import SWEEP_AT, NightlySweep, SweepDays
from lararium.steward.notice import make_daily_notifier
from lararium.steward.outbox import Outbox
from lararium.steward.sweep import Sweeper
from lararium.steward.threads import Threads
from lararium.timeofday import next_slot, slot_day

TZ = "Asia/Shanghai"
SH = ZoneInfo(TZ)
EMPTY_PLAN = json.dumps({"open": [], "close": [], "suggest": []})


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """2030 年 1 月的某个上海本地时刻。**放在将来**:起居注的 ts 是真时钟盖的,
    归拢只扫 ts 不晚于"现在"的事件,假时钟得在它们后面,喂进去的才是真对话。"""
    return datetime(2030, 1, day, hour, minute, tzinfo=SH)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Model:
    """模型那一侧。`script` 逐次给出:字符串 = 回这段话;整数 = 服务商回这个 HTTP 状态码。"""

    def __init__(self, *script: str | int) -> None:
        self.script = list(script)
        self.prompts: list[str] = []

    async def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        step = self.script.pop(0) if self.script else EMPTY_PLAN
        if isinstance(step, int):
            raise ModelCallError(f"HTTP {step}", retryable=True, status=step)
        return step


@pytest.fixture
def process(tmp_path):
    """一次"进程起来"。同一个 tmp_path 下的库是跨"进程"的那份真相。"""

    def make(model: Model, clock: Clock, *, busy=lambda: False):
        conn = connect(tmp_path / "steward.sqlite")
        ledger, gate = build_memory_components(tmp_path)
        journal = Journal(conn)
        outbox = Outbox(conn)
        notify = make_daily_notifier(
            journal=journal, outbox=outbox, conn=conn, timezone=TZ, channel="cli"
        )
        sweeper = Sweeper(
            journal, Threads(conn), gate, model, "测试指令", ledger=ledger, notify=notify
        )
        days = SweepDays(conn)
        nightly = NightlySweep(sweeper=sweeper, days=days, chat_busy=busy, timezone=TZ, clock=clock)
        return nightly, days, journal, conn

    return make


def talk(journal: Journal, text: str) -> int:
    return journal.append(uuid.uuid4().hex, "envelope", {"content": text})


async def settle_day(nightly: NightlySweep, clock: Clock, days: SweepDays, day, limit: int = 20):
    """一步步推到这一天了结。**有上限**:无限重试的变异在这里变成红,而不是挂住。"""
    delays: list[float] = []
    for _ in range(limit):
        delay = await nightly.step()
        delays.append(delay)
        if days.finished(day):
            return delays
        clock.advance(delay)
    raise AssertionError(f"推了 {limit} 步 {day} 还没了结——有什么在无限重试:{days.get(day)}")


# ── 什么时候跑:两个能搬走的小函数 ─────────────────────────────────────────────


def test_the_slot_is_today_once_the_run_time_has_passed_and_yesterday_before_it():
    """「今天该不该跑」问的是**最近一个已经过去的 04:00 是哪天的**,按配置时区算。"""
    assert slot_day(at(10, 4, 0), SH, SWEEP_AT).isoformat() == "2030-01-10"
    assert slot_day(at(10, 3, 59), SH, SWEEP_AT).isoformat() == "2030-01-09"
    # 同一个瞬间,换一个时区就是另一天的那一班:上海 01-10 03:59 = UTC 01-09 19:59
    assert slot_day(at(10, 3, 59), ZoneInfo("UTC"), SWEEP_AT).isoformat() == "2030-01-09"
    assert slot_day(at(10, 12, 30), ZoneInfo("UTC"), SWEEP_AT).isoformat() == "2030-01-10"


def test_the_next_wake_is_the_next_run_time():
    assert next_slot(at(10, 3, 59), SH, SWEEP_AT) == at(10, 4, 0)
    assert next_slot(at(10, 4, 0), SH, SWEEP_AT) == at(11, 4, 0)
    assert next_slot(at(10, 23, 0), SH, SWEEP_AT) == at(11, 4, 0)


# ── 一天一次,判据在库里 ─────────────────────────────────────────────────────


async def test_a_day_that_has_run_is_not_run_again_after_a_restart(process):
    """★ 变异 1 的靶子:"今天跑过"放内存不落库 → 重启后重跑。

    重启之前先说一句新的,**保证重跑的话一定会调模型**——否则归拢自己的内容幂等
    (光标之后没新内容就跳过)会让"重跑了"和"没重跑"长得一模一样(T6 第 3 种假绿)。
    """
    clock = Clock(at(10, 4, 30))
    first = Model()
    nightly, days, journal, _ = process(first, clock)
    talk(journal, "今天食堂 12 块")
    await settle_day(nightly, clock, days, slot_day(clock(), SH, SWEEP_AT))
    assert len(first.prompts) == 1

    talk(journal, "晚上又聊了一句:下周三线代期中考")
    clock = Clock(at(10, 9, 0))
    second = Model()
    restarted, days, _, _ = process(second, clock)
    for _ in range(3):
        clock.advance(await restarted.step())

    assert second.prompts == [], "同一天重启之后又跑了一遍——'今天跑过'没落库"

    clock.now = at(11, 4, 0)
    await restarted.step()
    assert len(second.prompts) == 1 and "线代期中考" in second.prompts[0], (
        "阳性对照:第二天那一班要把新说的那句喂进去,否则上面那句 == [] 钉不住任何东西"
    )


async def test_a_night_the_process_was_away_is_made_up_once_not_once_per_day(process):
    """整晚不在(甚至几天不在),起来补**一次**:归拢按光标扫到现在,不需要一天补一份。"""
    clock = Clock(at(7, 4, 30))
    nightly, days, journal, _ = process(Model(), clock)
    talk(journal, "七号说的")
    await settle_day(nightly, clock, days, slot_day(clock(), SH, SWEEP_AT))

    for day in (8, 9, 10):
        talk(journal, f"{day} 号说的")
    clock = Clock(at(10, 12, 0))  # 8、9 号整晚不在,10 号中午才起来
    model = Model()
    nightly, days, _, conn = process(model, clock)
    await settle_day(nightly, clock, days, slot_day(clock(), SH, SWEEP_AT))
    while clock() < at(11, 3, 59):
        clock.advance(await nightly.step())

    assert len(model.prompts) == 1, f"补跑了 {len(model.prompts)} 次,应该只补一次"
    for day in (8, 9, 10):
        assert f"{day} 号说的" in model.prompts[0], "补的那一次要把这几天的都扫到"
    rows = [r["day"] for r in conn.execute("SELECT day FROM sweep_days ORDER BY day")]
    assert rows == ["2030-01-07", "2030-01-10"], "不在的那几天不该一天记一行"


async def test_starting_before_the_run_time_makes_up_the_night_before(process):
    """凌晨两点起来,昨天 04:00 那一班没跑过 → 现在补;今天 04:00 那一班照常。"""
    clock = Clock(at(10, 2, 0))
    nightly, days, journal, _ = process(Model(), clock)
    talk(journal, "九号晚上说的")

    await settle_day(nightly, clock, days, slot_day(clock(), SH, SWEEP_AT))

    assert days.finished(at(9, 0).date()) and not days.finished(at(10, 0).date())


# ── 聊天优先 ─────────────────────────────────────────────────────────────────


async def test_it_does_not_start_while_a_turn_is_in_flight(process):
    """★ 变异 2 的靶子:有信封在排队或在处理,不开跑(和 PDF 转换器同一个 `chat_busy`)。"""
    chatting = {"busy": True}
    clock = Clock(at(10, 4, 0))
    model = Model()
    nightly, _, journal, _ = process(model, clock, busy=lambda: chatting["busy"])
    talk(journal, "凌晨四点还在聊")

    delays = [await nightly.step() for _ in range(3)]

    assert model.prompts == [], "聊天那一轮还没完,它就去调模型了"
    assert all(0 < d <= NightlySweep.CHAT_POLL for d in delays), f"让路要隔一小会儿再看:{delays}"
    chatting["busy"] = False
    await nightly.step()
    assert len(model.prompts) == 1, "阳性对照:聊完了就该跑"


# ── 失败、环境故障、没扫完 ───────────────────────────────────────────────────


async def test_a_failing_sweep_is_given_up_for_the_day_after_a_bounded_number_of_tries(process):
    """★ 变异 3 的靶子:分不清怪谁的失败(5xx、超时、回来的东西解不开)照记、封顶。

    封顶之后这一天就了结了;第二天那一班照常再试——光标没推过去,内容还在。
    """
    clock = Clock(at(10, 4, 0))
    model = Model(*([502] * 50))
    nightly, days, journal, _ = process(model, clock)
    talk(journal, "今天说的")
    day = slot_day(clock(), SH, SWEEP_AT)

    delays = await settle_day(nightly, clock, days, day)

    assert len(model.prompts) == NightlySweep.MAX_FAILURES
    assert days.get(day).outcome == "failed"
    assert all(d > 0 for d in delays[:-1]), f"失败之后原地立刻重来就是空转:{delays}"
    clock.advance(3600)
    await nightly.step()
    assert len(model.prompts) == NightlySweep.MAX_FAILURES, "了结的那一天又被试了一次"

    clock.now = at(11, 4, 0)
    await nightly.step()
    assert len(model.prompts) == NightlySweep.MAX_FAILURES + 1, "第二天那一班该照常再试"


async def test_the_failure_count_survives_a_restart(process):
    """次数在库里:崩了重启、再崩再重启,**不会**每次起来都白送一次机会(那也是重试到死)。"""
    clock = Clock(at(10, 4, 0))
    day = slot_day(clock(), SH, SWEEP_AT)
    for _ in range(NightlySweep.MAX_FAILURES - 1):
        nightly, days, journal, _ = process(Model(502), clock)
        talk(journal, "又说了一句")
        await nightly.step()
        assert not days.finished(day)

    last = Model(502)
    nightly, days, _, _ = process(last, clock)
    await nightly.step()

    assert len(last.prompts) == 1 and days.get(day).outcome == "failed"


@pytest.mark.parametrize("status", [401, 402, 403, 404, 429])
async def test_account_and_rate_failures_are_not_recorded_against_the_day(process, status):
    """★ 变异 4 的靶子(E4):key 换了、欠费了、限流了——**换哪一天来跑都一模一样**,
    不是这一天的错。不扣次数、不判终态;歇一阵再来,修好了当天就能跑成。"""
    clock = Clock(at(10, 4, 0))
    outage = NightlySweep.MAX_FAILURES + 2
    model = Model(*([status] * outage))
    nightly, days, journal, _ = process(model, clock)
    talk(journal, "今天说的")
    day = slot_day(clock(), SH, SWEEP_AT)

    delays = await settle_day(nightly, clock, days, day)

    assert len(model.prompts) == outage + 1, "故障期间被判了终态,修好之后这一天再也跑不成"
    assert days.get(day).outcome == "done"
    assert days.get(day).failures == 0, "环境故障记在了这一天头上"
    assert clock() < at(11, 4, 0), "阳性对照:这些都发生在同一天里"
    waits = delays[:outage]
    assert all(w >= NightlySweep.RETRY_AFTER for w in waits), f"故障期间在猛敲:{waits}"
    assert waits == sorted(waits), f"歇的时长该逐次拉长:{waits}"


@pytest.mark.parametrize("status", [400, 413, 422])
async def test_a_rejected_request_gives_up_the_day_at_once(process, status):
    """服务商拒了这一次的内容:同一批再发一模一样,当天不再试。"""
    clock = Clock(at(10, 4, 0))
    model = Model(status, status, status)
    nightly, days, journal, _ = process(model, clock)
    talk(journal, "今天说的")
    day = slot_day(clock(), SH, SWEEP_AT)

    await settle_day(nightly, clock, days, day)

    assert len(model.prompts) == 1 and days.get(day).outcome == "failed"


def _one_event_per_batch(monkeypatch):
    """把一批的字数和批数压到最小:一次 run 只喂得进一条,造出"没扫完"。"""
    monkeypatch.setattr(sweep_module, "_PROMPT_CONVO_MAX_CHARS", 70)
    monkeypatch.setattr(sweep_module, "_SWEEP_MAX_BATCHES", 1)


def _cursor(conn) -> int:
    row = conn.execute("SELECT cursor_seq FROM sweep_state WHERE id=1").fetchone()
    return int(row["cursor_seq"]) if row else 0


async def test_an_unfinished_sweep_goes_on_the_same_night_up_to_a_cap(process, monkeypatch):
    """积压扫不完:当晚接着跑,**封顶**;剩下的归第二天那一班(光标只推到喂进去的那条)。"""
    _one_event_per_batch(monkeypatch)
    clock = Clock(at(10, 4, 0))
    model = Model()
    nightly, days, journal, conn = process(model, clock)
    seqs = [talk(journal, f"第{i}条" + "话" * 40) for i in range(10)]
    day = slot_day(clock(), SH, SWEEP_AT)

    await settle_day(nightly, clock, days, day)

    assert len(model.prompts) == NightlySweep.MAX_RUNS
    assert days.get(day).outcome == "unfinished"
    assert _cursor(conn) < seqs[-1], "阳性对照:封顶时确实还没扫完"

    clock.now = at(11, 4, 0)
    await nightly.step()
    assert f"第{NightlySweep.MAX_RUNS}条" in model.prompts[-1], "第二天从昨晚停下的地方接着扫"


async def test_an_unfinished_sweep_that_catches_up_the_same_night_is_done(process, monkeypatch):
    _one_event_per_batch(monkeypatch)
    clock = Clock(at(10, 4, 0))
    model = Model()
    nightly, days, journal, conn = process(model, clock)
    seqs = [talk(journal, f"第{i}条" + "话" * 40) for i in range(2)]
    day = slot_day(clock(), SH, SWEEP_AT)

    await settle_day(nightly, clock, days, day)

    assert len(model.prompts) == 2 and days.get(day).outcome == "done"
    assert _cursor(conn) == seqs[-1]


# ── 不新增推送 ───────────────────────────────────────────────────────────────


async def test_the_only_push_is_the_existing_once_a_day_notice(process, monkeypatch):
    """跑了三次、每次都提了待审,**只推一条**;跑失败了**一条不推**。唯一出口是那个通知器。"""
    _one_event_per_batch(monkeypatch)
    suggest = json.dumps({"open": [], "close": [], "suggest": ["在上学"]})
    clock = Clock(at(10, 4, 0))
    model = Model(suggest, suggest, suggest)
    nightly, days, journal, conn = process(model, clock)
    for i in range(10):
        talk(journal, f"第{i}条" + "话" * 40)
    await settle_day(nightly, clock, days, slot_day(clock(), SH, SWEEP_AT))
    assert len(model.prompts) == NightlySweep.MAX_RUNS, "阳性对照:确实提了三次"

    clock.now = at(11, 4, 0)
    failing = Model(502, 502, 502)
    nightly, days, _, _ = process(failing, clock)
    await settle_day(nightly, clock, days, slot_day(clock(), SH, SWEEP_AT))

    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM outbox ORDER BY seq")]
    assert kinds == ["notice"], f"出件箱里应该只有那一条待审通知:{kinds}"
