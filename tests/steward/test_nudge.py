"""隔一阵问一嘴(M6-9)。

**这是"助手在场",不是"任务系统戳你"**。形状:到点了造一个 `source="nudge"` 的信封,内容是
**一条指令**,走正常的一轮(前缀、档案、话头、L0、工具全在);模型的回复就是要推的那句,
它判断没什么好说就回「不说」——**不推,不算失败**。

PLAN 那张验收清单逐条一个测试(外加两处论证:什么算"回了"、和聊天撞车怎么办)。
"模型"是第三方那一侧(T2),其余全是真的:真 Steward、真起居注、真出件箱、真库。
时间走假时钟,随机数给种子,**测试里不真等**。每次 `world(...)` 就是一次"进程起来"。
"""

import json
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from bundles.memory.server import build_memory_components, memory_tool_functions

from lararium.config import Settings
from lararium.db import connect
from lararium.envelope import Envelope
from lararium.steward.inbox import Inbox
from lararium.steward.journal import Journal
from lararium.steward.loop import Steward
from lararium.steward.model import ModelCallError, ModelReply
from lararium.steward.nudge import (
    REPEAT_DAYS,
    SHELF_LIFE,
    Nudger,
    NudgeState,
    Spoken,
    discard_expired,
    is_silent,
    load_nudge_prompt,
    render_instruction,
)
from lararium.steward.outbox import Outbox
from lararium.steward.registry import Registry
from lararium.steward.threads import Threads
from lararium.timeofday import QuietHours

TZ = "Asia/Shanghai"
SH = ZoneInfo(TZ)
MIN = timedelta(minutes=45)
MAX = timedelta(minutes=90)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2030, 1, day, hour, minute, tzinfo=SH)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Model:
    """模型那一侧。`script` 逐次:字符串 = 回这句;函数 = 拿工具表自己干活再回;异常 = 抛。"""

    def __init__(self, *script) -> None:
        self.script = list(script)
        self.seen = []

    async def run(self, ctx, tools, mcp_servers):
        self.seen.append(ctx)
        step = self.script.pop(0) if self.script else "嗯"
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step({f.__name__: f for f in tools})
        return ModelReply(text=step)


@dataclass
class World:
    steward: Steward
    nudger: Nudger
    conn: object
    clock: Clock
    model: Model


@pytest.fixture
def world(tmp_path, monkeypatch):
    def make(model: Model, clock: Clock, *, quiet="02:00-08:00", busy=None, seed=7) -> World:
        monkeypatch.setenv("LARARIUM_API_KEY", "sk-test")
        monkeypatch.setenv("LARARIUM_DATA_DIR", str(tmp_path))
        settings = Settings.load()
        conn = connect(tmp_path / "steward.sqlite")
        ledger, gate = build_memory_components(tmp_path)
        registry = Registry.load(Path("bundles"))
        memory = memory_tool_functions(gate)
        steward = Steward(
            settings=settings,
            inbox=Inbox(conn),
            journal=Journal(conn),
            registry=registry,
            ledger=ledger,
            gate=gate,
            model=model,
            persona="你是 Lararium。",
            outbox=Outbox(conn),
            threads=Threads(conn),
            bundle_tools=registry.qualify_tools("memory", memory),
            proposal_tool=memory.propose_fact,
        )
        nudger = Nudger(
            state=NudgeState(conn),
            submit=steward.inbox.put,
            chat_busy=busy or steward.inbox.has_unfinished,
            quiet=QuietHours.parse(quiet),
            timezone=TZ,
            channel="cli",
            instructions=load_nudge_prompt(),
            min_interval=MIN,
            max_interval=MAX,
            clock=clock,
            rng=random.Random(seed),
        )
        return World(steward, nudger, conn, clock, model)

    return make


async def drain(w: World) -> None:
    while (await w.steward.process_next()).kind != "empty":
        pass


async def say(w: World, text: str, *, source="user") -> None:
    """用户(或数据面)说一句,并把这一轮跑完。"""
    meta = {"untrusted": True} if source != "user" else {}
    w.steward.inbox.put(
        Envelope(
            id=uuid.uuid4().hex,
            source=source,
            channel="cli",
            content=text,
            meta=meta,
            ts=w.clock().astimezone(UTC),
        )
    )
    await drain(w)


def nudge_rows(w: World) -> list:
    return w.conn.execute(
        "SELECT id, content, meta, ts, state, error FROM inbox WHERE source='nudge' ORDER BY ts"
    ).fetchall()


def pushed(w: World) -> list[str]:
    """出件箱里**所有**条目的正文(不按渠道、不按过期筛)——"用户那边能看到的全集"。"""
    return [r[0] for r in w.conn.execute("SELECT content FROM outbox ORDER BY seq").fetchall()]


async def next_nudge(w: World, limit: int = 12) -> datetime | None:
    """照后台那一圈推时钟,直到叫醒一次(投进一个信封、那一轮跑完)。返回叫醒的时刻。

    **有上限**:"无人应答照样追加"这类变异在这里变成"返回了时刻",而不是挂住。"""
    before = len(nudge_rows(w))
    for _ in range(limit):
        delay = w.nudger.step()
        if len(nudge_rows(w)) > before:
            fired = w.clock()
            await drain(w)
            w.clock.advance(30)  # 那一轮跑了一会儿;不推的话,紧接着那句用户消息会和问候同一刻
            return fired
        w.clock.advance(max(delay, 1.0))
    return None


# ── "不发"走得通 ──────────────────────────────────────────────────────────────


def test_silence_is_recognised_with_or_without_the_word():
    assert is_silent("不说")
    assert is_silent("  「不说」\n")
    assert is_silent("")
    assert not is_silent("不说这个了,晚饭吃了吗")


async def test_saying_nothing_pushes_nothing_and_is_not_a_failure(world):
    """★ 变异「模型回空时仍然推一条」的靶子。**不说是正常出口**:不产生任何出件箱消息,
    信封走到 done(不是 failed),起居注里没有 error——但这一轮照样完整落了痕。"""
    w = world(Model("好的", "不说"), Clock(at(10, 10, 0)))
    await say(w, "午饭吃了面")

    assert await next_nudge(w) is not None, "场景没发生:根本没叫醒"

    (row,) = nudge_rows(w)
    assert pushed(w) == ["好的"], "只该有回用户那一句"
    assert (row["state"], row["error"]) == ("done", None)
    kinds = [e["kind"] for e in w.steward.journal.replay(row["id"])]
    assert "error" not in kinds
    assert kinds[0] == "envelope" and kinds[-1] == "reply"


# ── 间隔真的随机,不对齐整点 ─────────────────────────────────────────────────


async def test_the_gap_is_random_within_the_range_and_never_on_the_hour(world):
    """★ 变异「间隔写死成常量」的靶子。连着 20 次:每次用户说一句,量到下一次叫醒隔了多久。"""
    w = world(Model(), Clock(at(10, 9, 0)), quiet="00:00-00:00")
    gaps: list[timedelta] = []
    fires: list[datetime] = []
    for i in range(20):
        spoke = w.clock()
        await say(w, f"第 {i} 句")
        fired = await next_nudge(w)
        assert fired is not None
        gaps.append(fired - spoke)
        fires.append(fired)

    assert all(MIN <= g <= MAX for g in gaps), gaps
    assert len(set(gaps)) > 1, "20 次间隔全一样:写死了"
    assert all(f.second or f.microsecond for f in fires), "落在整分整秒上:是定时任务的味道"


# ── 不重复 ──────────────────────────────────────────────────────────────────


async def test_a_topic_already_raised_is_handed_back_as_do_not_repeat(world):
    """同一个话头两次唤醒:第二次那一轮,模型收到的指令里**逐字**带着上次主动说过的那句,
    外加"别拿同一件事再开口"。过了 N 天就不再列。

    **判断是模型的**(说哪件事、算不算同一件):这里钉的是"材料和规矩真的送到了它眼前"。
    """
    w = world(Model("好", "课程表那事你还想录吗", "周末吧", "今天忙吗"), Clock(at(10, 10, 0)))
    await say(w, "今天没课")
    first = await next_nudge(w)
    await say(w, "周末再说")
    assert await next_nudge(w) is not None

    template = load_nudge_prompt()
    expected = render_instruction(
        template, recent=[Spoken(ts=first, content="课程表那事你还想录吗")], timezone=TZ
    )
    assert w.model.seen[-1].messages[-1]["content"].endswith(expected)
    assert "课程表那事你还想录吗" in expected

    # N 天之后:不再列
    w.clock.now = w.clock() + timedelta(days=REPEAT_DAYS + 1)
    w.clock.now = w.clock().replace(hour=10)
    await say(w, "我回来了")
    assert await next_nudge(w) is not None
    assert "课程表那事你还想录吗" not in nudge_rows(w)[-1]["content"]


# ── 无人应答不追加 ──────────────────────────────────────────────────────────


async def test_no_answer_to_the_last_nudge_means_no_next_one(world):
    """★ 变异「无人应答照样追加」的靶子。上一条主动消息之后**没有用户的话**,就不再叫醒
    ——数据面进来一条短信不算"他回了";用户开口之后才接着来。"""
    w = world(Model("好", "在忙什么呢", "收到"), Clock(at(10, 9, 0)))
    await say(w, "早")
    assert await next_nudge(w) is not None
    calls = len(w.model.seen)

    assert await next_nudge(w, limit=10) is None
    await say(w, "【某银行】您尾号 1234 的卡消费 38 元", source="module_event")
    assert await next_nudge(w, limit=10) is None, "一条短信进站不代表用户在场"
    assert len(w.model.seen) == calls + 1, "只多了回短信那一次,没有第二次主动开口"

    await say(w, "刚才在开会")
    assert await next_nudge(w) is not None


async def test_a_user_away_for_a_day_is_not_nudged(world):
    """人不在场就不叫:最后一句话已经超过 24 小时(微信窗口),说了也送不到。"""
    w = world(Model("好"), Clock(at(10, 9, 0)))
    await say(w, "出差去了")
    w.clock.now = at(11, 10, 0)
    restarted = world(Model(), w.clock)
    assert await next_nudge(restarted, limit=10) is None


# ── 过期就丢,查得到 ────────────────────────────────────────────────────────


async def test_the_shelf_life_is_short_and_never_outlives_the_window(world):
    w = world(Model("好", "在忙什么呢", "好", "晚上好"), Clock(at(10, 9, 0)))
    await say(w, "早")
    fired = await next_nudge(w)
    (row,) = nudge_rows(w)
    expires = datetime.fromisoformat(json.loads(row["meta"])["expires_at"])
    assert expires == fired + SHELF_LIFE

    # 用户最后一句在前一天 10:00:窗口 10:00 就关,保质期跟着窗口走,不是叫醒之后再给两小时
    w.clock.now = at(12, 10, 0)
    await say(w, "明天见")
    w.clock.now = at(13, 8, 0)
    fired = await next_nudge(w)
    assert fired is not None and fired + SHELF_LIFE > at(13, 10, 0), "场景没发生"
    expires = datetime.fromisoformat(json.loads(nudge_rows(w)[-1]["meta"])["expires_at"])
    assert expires == at(13, 10, 0)


async def test_an_expired_nudge_is_dropped_and_the_drop_is_in_the_journal(world):
    w = world(Model("好", "在忙什么呢"), Clock(at(10, 9, 0)))
    await say(w, "早")
    fired = await next_nudge(w)
    outbox, journal = w.steward.outbox, w.steward.journal

    fresh = fired + SHELF_LIFE - timedelta(minutes=1)
    assert discard_expired(outbox=outbox, journal=journal, now=fresh) == 0
    assert [i.content for i in outbox.take("cli", 1, now=fresh)] == ["在忙什么呢"]

    stale = fired + SHELF_LIFE + timedelta(seconds=1)
    assert discard_expired(outbox=outbox, journal=journal, now=stale) == 1
    assert outbox.take("cli", 1, now=stale) == [], "过期的不许再交给适配器"
    assert discard_expired(outbox=outbox, journal=journal, now=stale) == 0, "同一条不许记两次"

    dropped = w.conn.execute(
        "SELECT envelope_id, payload FROM journal WHERE kind='outbox_expired'"
    ).fetchall()
    assert len(dropped) == 1
    assert dropped[0]["envelope_id"] == nudge_rows(w)[0]["id"]
    assert "在忙什么呢" in dropped[0]["payload"]


# ── 关得掉 ──────────────────────────────────────────────────────────────────


async def test_once_turned_off_it_stays_off(world):
    """用户说"别发了",她自己调工具关掉;之后连跑五次唤醒零条,重启照样零条,用户接着聊天也不会自己又开。"""

    def turn_off(tools):
        result = tools["stop_nudging"]()
        return ModelReply(text=f"好,不打扰你了({result})")

    w = world(Model("好", "在忙什么呢", turn_off), Clock(at(10, 9, 0)))
    await say(w, "早")
    await next_nudge(w)
    await say(w, "别主动发消息了")

    for _ in range(5):
        w.clock.advance(MAX.total_seconds())
        w.nudger.step()
    restarted = world(Model(), w.clock)
    await say(restarted, "今天有点累")
    for _ in range(5):
        restarted.clock.advance(MAX.total_seconds())
        restarted.nudger.step()

    assert len(nudge_rows(restarted)) == 1, "关掉之后又叫醒了"


# ── 落痕完整 / 不伪装成用户 / 用户看不到指令 ─────────────────────────────────


async def test_a_pushed_nudge_is_a_full_turn_that_the_next_reply_can_see(world):
    """M4-7 的金样:用户接着回一句,模型那一轮的上下文里**看得到**自己刚才主动说了什么。"""
    w = world(Model("好", "课程表那事你还想录吗", "那就周末"), Clock(at(10, 10, 0)))
    await say(w, "今天没课")
    await next_nudge(w)
    (row,) = nudge_rows(w)
    kinds = [e["kind"] for e in w.steward.journal.replay(row["id"])]
    assert (kinds[0], kinds[-1]) == ("envelope", "reply"), kinds

    await say(w, "周末吧")

    messages = w.model.seen[-1].messages
    assert {"role": "assistant", "content": "课程表那事你还想录吗"} in messages


async def test_the_instruction_renders_as_a_system_trigger_not_as_the_user(world):
    """★ 这一条最要紧,也是变异「指令渲染成用户说的话」的靶子。

    伪装成用户的话,历史里就是"用户说过『你主动跟我说一句』",**之后每一轮**模型都会以为
    用户要它主动说话。断言**全量**(T6 第 5 种):整条消息逐字等于系统触发那一支的样子。
    """
    w = world(Model("好", "在忙什么呢", "哈哈"), Clock(at(10, 10, 0)))
    await say(w, "今天没课")
    fired = await next_nudge(w)
    (row,) = nudge_rows(w)
    await say(w, "在写作业")

    stamp = fired.astimezone(SH).isoformat(timespec="seconds")
    instruction = render_instruction(load_nudge_prompt(), recent=[], timezone=TZ)
    assert row["content"] == instruction
    messages = w.model.seen[-1].messages
    index = messages.index({"role": "assistant", "content": "在忙什么呢"})
    assert messages[index - 1] == {
        "role": "user",
        "content": f"[{stamp}] (系统触发 · nudge/cli) {instruction}",
    }


async def test_the_user_never_sees_the_instruction(world):
    """出件箱里只有回复那一句。**模型那一轮炸了也不许**顺着"处理失败"的通知把指令带出去。"""
    w = world(Model("好", "在忙什么呢"), Clock(at(10, 10, 0)))
    await say(w, "今天没课")
    await next_nudge(w)
    assert pushed(w) == ["好", "在忙什么呢"]

    broken = world(Model("嗯", ModelCallError("HTTP 400", retryable=False, status=400)), w.clock)
    await say(broken, "刚才在写作业")
    assert await next_nudge(broken) is not None
    assert pushed(broken) == ["好", "在忙什么呢", "嗯"], "失败通知里带出了指令"
    lines = [
        x.strip() for x in nudge_rows(broken)[-1]["content"].splitlines() if len(x.strip()) > 6
    ]
    assert lines, "场景没发生:指令是空的"
    assert not [x for x in lines for text in pushed(broken) if x[:12] in text]


# ── 那一轮能用工具 ──────────────────────────────────────────────────────────


async def test_the_nudge_turn_can_call_tools(world):
    """唤醒那一轮是**正常的一轮**:工具是通的(不然它只能凭前缀里那点东西说话)。"""

    def look_around(tools):
        tools["current_time"]()
        tools["list_threads"]()
        return ModelReply(text="课程表那事还录吗")

    w = world(Model("好", look_around), Clock(at(10, 10, 0)))
    w.steward.threads.open_thread("课程表", "想录进来,还没弄")
    await say(w, "今天没课")
    await next_nudge(w)

    (row,) = nudge_rows(w)
    ran = [
        e["payload"]["tool"]
        for e in w.steward.journal.replay(row["id"])
        if e["kind"] == "tool_executed"
    ]
    assert ran == ["current_time", "list_threads"]
    assert pushed(w)[-1] == "课程表那事还录吗"


# ── 静默时段 / 聊天优先 / 默认 off ──────────────────────────────────────────


async def test_quiet_hours_neither_wake_nor_save_up_a_nudge(world):
    """01:50 说完最后一句,到点正落在 02:00~08:00 里:这段时间一次都不醒,**也不攒**
    ——08:00 之后隔一阵(不是 08:00 整)才来一次,而且只来一次。"""
    w = world(Model("晚安", "早呀"), Clock(at(10, 1, 50)))
    await say(w, "睡了")

    fired = await next_nudge(w, limit=20)

    assert fired is not None
    local = fired.astimezone(SH)
    assert at(10, 8, 0) + MIN <= local <= at(10, 8, 0) + MAX, local
    assert len(nudge_rows(w)) == 1


async def test_it_gives_way_while_a_turn_is_in_flight(world):
    """撞车:到点时正有信封在排队/在处理 → 这一次让开,不投;用户那句跑完,间隔从那句重新算。"""
    busy = {"now": True}
    w = world(Model("好", "好"), Clock(at(10, 10, 0)), busy=lambda: busy["now"])
    await say(w, "今天没课")

    assert await next_nudge(w, limit=10) is None
    assert w.nudger.step() == Nudger.CHAT_POLL

    busy["now"] = False
    spoke = w.clock()
    await say(w, "我又来了")
    fired = await next_nudge(w)
    assert fired is not None and fired - spoke >= MIN


def test_it_is_off_by_default_and_quiet_hours_default_to_two_to_eight(monkeypatch):
    monkeypatch.setenv("LARARIUM_API_KEY", "sk-test")
    settings = Settings.load()
    assert settings.nudge is False
    assert settings.quiet_hours == QuietHours.parse("02:00-08:00")
    assert (settings.nudge_min_minutes, settings.nudge_max_minutes) == (45.0, 90.0)
