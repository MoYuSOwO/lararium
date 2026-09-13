"""每天最多一条的待审通知(P1-3 立、M4-7 落成一轮对话、M6-9 守静默时段)。

**为什么从 `sweep.py` 搬出来**:它从来不只是归拢的——压缩被屏障停也用它;M6-9 又要给它加一个
"攒着、到点放出去"的后台那一圈。留在 `sweep.py` 里,那个文件就同时装着"扫起居注"和"什么时候
开口"两件事(S2)。搬家没改它原来的任何行为:同一个事务、同一张 `notice_log`、同一个由头。

**静默时段(M6-9)**:归拢 04:00 跑,跑完有待审,原来那条当场发——而用户凌晨两三点还在记宵夜,
四点很可能刚睡着。所以:

- 时段里来的通知**不发,攒进 `held_notice`**(一行,落库:崩了重启不丢);已经攒着一条就不再攒
  ——跨午夜的时段(23:00~07:00)里两个日期各来一条,07:00 那一刻用户也只该收到**一条**;
- 时段结束那一刻由 `run()` 放出去,走的是**同一个发送路径**(`_send`),名额按**发出去那天**算
  ——所以"每天最多一条"原样成立,没有第二条推送路径;
- 攒着的时候**起居注里一个字都不写**:没发出去就落一轮,模型会以为自己说过(M4-7 的"半条比没有更坏")。
- 待审不会过期,所以攒多久都照发;反过来,放出去那天的名额已经被占了(比如 01:00 刚发过一条),
  攒着的那条就作罢——每天最多一条是硬的,待审本身还在 `/pending` 里。
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from lararium.db import transaction
from lararium.envelope import Envelope
from lararium.timeofday import QuietHours

# 推送信封的"由头"正文。**固定常量**:它每次都一样,进 L0 后逐字稳定;
# 写成"系统自己开口"的形态,而不是把推送正文塞进这里——正文是**回复**,由头是**触发**。
PUSH_TRIGGER = "(到点了,把攒下的事跟他说一声)"

# 没有静默时段时的占位:起止相同 = 从不静默(`QuietHours.contains` 的约定)。
_NEVER_QUIET = QuietHours.parse("00:00-00:00")


class DailyNotifier:
    """调用它 = 投一条通知(可能攒着);`run()` 是和 worker 并排跑的那一圈,负责到点放出去。"""

    # 最长睡多久再自己看一眼:兜住系统时钟被调、机器睡眠。醒一次是一两次查库。
    IDLE_POLL = 3600.0

    def __init__(
        self,
        *,
        journal: Any,
        outbox: Any,
        conn: Any,
        timezone: str,
        channel: str,
        quiet: QuietHours | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._journal = journal
        self._outbox = outbox
        self.conn = conn
        self._tz = ZoneInfo(timezone)
        self._channel = channel
        self._quiet = quiet or _NEVER_QUIET
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep or asyncio.sleep

    def __call__(self, text: str) -> None:
        now = self._clock()
        if self._quiet.contains(now, self._tz):
            self._hold(text, now)
            return
        self._send(text, now)

    def _hold(self, text: str, now: datetime) -> None:
        # INSERT OR IGNORE:已经攒着一条就不再攒(见模块 docstring)
        self.conn.execute(
            "INSERT OR IGNORE INTO held_notice (id, content, held_at) VALUES (1, ?, ?)",
            (text, now.astimezone(UTC).isoformat()),
        )

    def _send(self, text: str, now: datetime) -> None:
        """占名额 + 起居注两条 + 出件箱一条,**同一个事务**(M4-7:半条比没有更坏)。

        以前它用假信封 id 投出件箱、起居注里什么都不写,而 L0 靠 `envelope` + `reply` 成对取——
        早上推「这个月餐饮 1240」,用户说「太多了吧」,模型没有任何上下文。现在由头当信封
        (`source="sweep"`,渲染走「系统触发」那一支,不伪装成用户),正文当回复,两者都进 L0。
        """
        today = now.astimezone(self._tz).date().isoformat()
        with transaction(self.conn):
            cur = self.conn.execute("INSERT OR IGNORE INTO notice_log (date) VALUES (?)", (today,))
            if cur.rowcount == 0:
                return  # 今天已经投过(DB 是唯一的判据,跨进程/重启都防)
            env = Envelope(
                id=uuid.uuid4().hex,
                source="sweep",
                channel=self._channel,
                content=PUSH_TRIGGER,
                ts=now.astimezone(UTC),
            )
            self._journal.append(
                env.id,
                "envelope",
                {
                    "content": env.content,
                    "source": env.source,
                    "channel": env.channel,
                    "meta": env.meta,
                    "ts": env.ts.isoformat(),
                },
            )
            self._journal.append(env.id, "reply", {"content": text})
            self._outbox.put(env.id, self._channel, text, kind="notice")

    def release(self) -> None:
        """不在静默时段里,就把攒着的那条放出去(没有就什么都不做)。删掉攒着的那行和发送同一个事务:
        崩在中间要么都没发生、要么都发生了,不会发两遍,也不会攒着的没了、用户却什么都没收到。"""
        now = self._clock()
        if self._quiet.contains(now, self._tz):
            return
        with transaction(self.conn):
            row = self.conn.execute("SELECT content FROM held_notice WHERE id=1").fetchone()
            if row is None:
                return
            self.conn.execute("DELETE FROM held_notice WHERE id=1")
            self._send(str(row["content"]), now)

    def step(self) -> float:
        """放一次、返回离下次该醒还有几秒:**总是醒在时段的边界上**(结束那一刻放出去,
        开始那一刻无事可做但不妨碍),中间最多睡 `IDLE_POLL`。"""
        self.release()
        now = self._clock()
        if self._quiet.contains(now, self._tz):
            boundary = self._quiet.ends_at(now, self._tz)
        else:
            boundary = self._quiet.starts_at(now, self._tz)
        return max(0.0, min((boundary - now).total_seconds(), self.IDLE_POLL))

    async def run(self) -> None:
        while True:
            await self._sleep(self.step())


def make_daily_notifier(
    *,
    journal: Any,
    outbox: Any,
    conn: Any,
    timezone: str,
    channel: str,
    quiet: QuietHours | None = None,
    clock: Callable[[], datetime] | None = None,
) -> DailyNotifier:
    """组装根的通知器工厂。**同一张表是唯一的判据**:造几个实例都一样(三处调用点各造一个,
    lifespan 里那个负责放出去)。"""
    return DailyNotifier(
        journal=journal,
        outbox=outbox,
        conn=conn,
        timezone=timezone,
        channel=channel,
        quiet=quiet,
        clock=clock,
    )
