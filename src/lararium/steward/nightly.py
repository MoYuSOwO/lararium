"""夜间归拢自动跑(M6-8):`/sweep` 从手动变成每天一次。

**为什么要自动**:手动吃过两次亏。M5-24 头两天的对话一次没归拢过,攒到跨三天;M6-8 立项那天
真机起居注里最后一次归拢在四天前。归拢按光标扫,攒着不会丢,但**攒着的这几天里该开的话头没开、
该提的事实没提**,而那是静默的。

**这不是推送**。归拢跑完提出了待审,出口还是 `make_daily_notifier` 那一条(每天最多一条),
这里一个字都不往出件箱写。

**什么时候跑**:配置时区下每天 `SWEEP_AT`(04:00,人睡着、当天的话都说完了)。判据是
「**最近一个已经过去的 04:00 是哪天的**」(`slot_day`)——那一天在 `sweep_days` 里没了结,就该跑:

- 04:00 进程在 → 到点就跑;
- 进程整晚不在,10 点才起来 → 起来就补**一次**(今天那一班);
- 几天不在 → 还是只补**一次**:只看最近那一班,不一天补一份。归拢按光标扫到现在,补一次就全扫到了;
- 凌晨两点起来、昨天那一班没了结 → 现在补昨天那一班,04:00 今天那一班照常(多半光标之后没新内容,
  不调模型就了结)。

**跑完怎么算**(M5-30 立的形状:成功 / 失败 / 没扫完三样,各有处置):

| 这次 | 怎么办 |
|---|---|
| 扫完了(含"光标之后没新内容") | 这一天了结:`done` |
| 没扫完(撞批数上限) | 当晚接着跑,这一天最多 `MAX_RUNS` 次;到顶了结为 `unfinished`,剩下的归明天那一班 |
| 服务商拒了这次的内容(400/413/422) | 同一批再发一模一样,这一天当场了结为 `failed` |
| 账号/余额/配置/限流(401/402/403/404/429) | **不是这一天的错**(E4):不扣次数、不了结,歇一阵再来,歇的时长逐次拉长 |
| 分不清(5xx、超时、回来的东西解不开、代码层面的错) | 记一次,歇一阵再来;满 `MAX_FAILURES` 次了结为 `failed` |

计数都在库里:崩了重启不会白送一次机会。环境故障的"歇多久"放在内存里——那只是节奏,
不是判决;重启之后立刻再敲一次,换一个 key 修好的人正好想看到这个。

**手动 `/sweep` 不算"今天跑过"**:两者走同一个 `Sweeper` 实例的 `run`,手动的不写 `sweep_days`。
不需要算——手动跑过之后到了 04:00 那一班,光标之后没新内容就不调模型直接了结,代价是零;
而要让它算,就得先回答"晚上十点手动跑的那一次算哪一天那一班",那是给自己找麻烦。

**聊天优先**:有信封在排队或在处理(`chat_busy`,和 PDF 转换器同一个),不开跑。开跑之后
一次 run 最多几批模型调用,中途不停下——批与批之间停,光标推到哪儿都说得清,但那要改 Sweeper,
而凌晨四点撞上聊天的代价只是最多和聊天重叠这一次 run。

**形状留给以后搬**:判定"该不该跑"(`slot_day` + `SweepDays.finished`)和"下次什么时候醒"
(`next_slot`)是两个不依赖这个类的小函数。M6-9 搬走了(`lararium.timeofday`,多了一个参数
`at`):两处真正共有的只有"一天里的某个钟点",静默时段的起止问的也是它。**仍然没有通用调度器**。
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any, Literal
from zoneinfo import ZoneInfo

from lararium.steward.model import NOT_THE_REQUEST_STATUS, REQUEST_REJECTED_STATUS
from lararium.steward.sweep import Sweeper, SweepResult, nominal_window
from lararium.timeofday import next_slot, slot_day

logger = logging.getLogger("lararium")

# 每天几点跑(配置时区)。**不做成配置项**(G5):没人要改它;真要改是这一行。
SWEEP_AT = time(4, 0)

Outcome = Literal["done", "unfinished", "failed"]
Verdict = Literal["done", "unfinished", "rejected", "environment", "failed"]


def judge(result: SweepResult) -> Verdict:
    """一次 run 的结果该怎么处置。纯函数:按"怪不怪这一天"分,不按"要不要重试"分(E4)。"""
    if result.failed:
        if result.status in NOT_THE_REQUEST_STATUS:
            return "environment"
        if result.status in REQUEST_REJECTED_STATUS:
            return "rejected"
        return "failed"
    return "unfinished" if result.unfinished else "done"


@dataclass(frozen=True)
class SweepDay:
    day: date
    runs: int
    failures: int
    outcome: Outcome | None
    note: str


class SweepDays:
    """`sweep_days` 表:一天一行。查询和改状态分开(F4)。"""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def get(self, day: date) -> SweepDay | None:
        row = self._conn.execute(
            "SELECT runs, failures, outcome, note FROM sweep_days WHERE day=?", (day.isoformat(),)
        ).fetchone()
        if row is None:
            return None
        return SweepDay(day, int(row["runs"]), int(row["failures"]), row["outcome"], row["note"])

    def finished(self, day: date) -> bool:
        """这一天那一班了结了没有(done / unfinished / failed 都算了结)。"""
        record = self.get(day)
        return record is not None and record.outcome is not None

    def record_run(self, day: date, note: str) -> None:
        """没扫完的一次:runs + 1,不了结。"""
        self._conn.execute(
            "INSERT INTO sweep_days (day, runs, note, updated_at) VALUES (?, 1, ?, ?) "
            "ON CONFLICT(day) DO UPDATE SET runs=runs+1, note=excluded.note, "
            "updated_at=excluded.updated_at",
            (day.isoformat(), note, _stamp()),
        )

    def record_failure(self, day: date, note: str) -> None:
        """记在这一天头上的一次失败:failures + 1,不了结。"""
        self._conn.execute(
            "INSERT INTO sweep_days (day, failures, note, updated_at) VALUES (?, 1, ?, ?) "
            "ON CONFLICT(day) DO UPDATE SET failures=failures+1, note=excluded.note, "
            "updated_at=excluded.updated_at",
            (day.isoformat(), note, _stamp()),
        )

    def finish(self, day: date, outcome: Outcome, note: str) -> None:
        self._conn.execute(
            "INSERT INTO sweep_days (day, outcome, note, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(day) DO UPDATE SET outcome=excluded.outcome, note=excluded.note, "
            "updated_at=excluded.updated_at",
            (day.isoformat(), outcome, note, _stamp()),
        )


def _stamp() -> str:
    return datetime.now(UTC).isoformat()


class NightlySweep:
    """和 worker、PDF 转换器并排跑的后台任务。一圈 = `step()` 判一次、该跑就跑一次,
    然后睡到它说的时候。"""

    # 这一天最多跑几次(第一次 + 没扫完接着跑的)。一次 run 最多 6 批,三次就是一晚上 18 次
    # 廉价模型调用、36 万字——积压再大也分几晚补,光标不会丢。
    MAX_RUNS = 3
    # 记在这一天头上的失败最多几次。
    MAX_FAILURES = 3
    # 失败之后歇多久再来(秒),逐次翻倍,封顶 MAX_BACKOFF。
    RETRY_AFTER = 600.0
    MAX_BACKOFF = 7200.0
    # 聊天那一轮还没完时,隔多久再看一眼。
    CHAT_POLL = 5.0
    # 了结之后最长睡多久再自己看一眼:兜住系统时钟被调、机器睡眠这类"睡过头",看一眼是一次查库。
    IDLE_POLL = 3600.0

    def __init__(
        self,
        *,
        sweeper: Sweeper,
        days: SweepDays,
        chat_busy: Callable[[], bool],
        timezone: str,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._sweeper = sweeper
        self._days = days
        self._chat_busy = chat_busy
        self._tz = ZoneInfo(timezone)
        # 可注入的时钟和 sleep:测试走假时钟,不真等。
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep or asyncio.sleep
        self._outages = 0  # 连续几次环境故障(只管歇多久,不是判决,所以不落库)

    async def run(self) -> None:
        while True:
            await self._sleep(await self.step())

    async def step(self) -> float:
        """判一次、该跑就跑一次。返回离下次该醒还有几秒。"""
        now = self._clock()
        day = slot_day(now, self._tz, SWEEP_AT)
        if self._days.finished(day):
            return self._idle(now)
        if self._chat_busy():
            return self.CHAT_POLL
        try:
            result = await self._sweeper.run(*nominal_window(now))
        except Exception:
            # Sweeper 自己把模型那一侧的失败收成结果了;能抛到这里的是库、磁盘这类代码层面的错。
            # 处置:记在这一天头上、封顶、歇一阵——和"分不清怪谁"同一条路,别让后台杂活停摆。
            logger.exception("夜间归拢 %s:这一次出了代码层面的错", day)
            return self._count_failure(day, "代码层面的错(见日志)")
        return self._settle(day, result)

    def _settle(self, day: date, result: SweepResult) -> float:
        verdict = judge(result)
        status = f" {result.status}" if result.status else ""
        logger.info("夜间归拢 %s [%s%s]:%s", day, verdict, status, result.summary)
        if verdict == "environment":
            self._outages += 1
            return self._backoff(self._outages)
        self._outages = 0
        if verdict == "failed":
            return self._count_failure(day, result.summary)
        if verdict == "rejected":
            self._days.finish(day, "failed", result.summary)
        elif verdict == "unfinished":
            self._days.record_run(day, result.summary)
            record = self._days.get(day)
            if record is None or record.runs < self.MAX_RUNS:
                return 0.0
            self._days.finish(day, "unfinished", result.summary)
        else:
            self._days.finish(day, "done", result.summary)
        return self._idle(self._clock())

    def _count_failure(self, day: date, note: str) -> float:
        self._days.record_failure(day, note)
        record = self._days.get(day)
        failures = record.failures if record else self.MAX_FAILURES
        if failures >= self.MAX_FAILURES:
            self._days.finish(day, "failed", note)
            logger.warning("夜间归拢 %s:失败 %d 次,这一天不再试,明天那一班照常", day, failures)
            return self._idle(self._clock())
        return self._backoff(failures)

    def _backoff(self, times: int) -> float:
        return min(self.RETRY_AFTER * 2.0 ** (times - 1), self.MAX_BACKOFF)

    def _idle(self, now: datetime) -> float:
        until = (next_slot(now, self._tz, SWEEP_AT) - now).total_seconds()
        return max(0.0, min(until, self.IDLE_POLL))
