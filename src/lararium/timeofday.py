"""一天里的钟点,按配置时区算。

**为什么是单独一个模块**:M6-8 的夜间归拢把「最近一个已经过去的 04:00 是哪天的」「下一个 04:00
是什么时候」写成了两个不依赖类的小函数,并且说好了留给下一个要定时的人搬。M6-9 来搬了——
隔一阵问一嘴和归拢的待审通知都要守**同一个静默时段**,而静默时段问的正是同一类问题
(「现在是不是在 02:00~08:00 里」「这一段几点结束」)。三处用,放进一个有名字的概念里,
不放进 `nightly` 也不放进 `nudge`(S1)。

**不是通用调度器**(G5):没有 cron 表达式、没有任务表,只有"一天里的某个钟点"这一个概念。
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def slot_day(now: datetime, tz: ZoneInfo, at: time) -> date:
    """最近一个已经过去的 `at` 是哪一天的(正好 `at` 那一刻算今天)。"""
    local = now.astimezone(tz)
    return local.date() if local.time() >= at else local.date() - timedelta(days=1)


def next_slot(now: datetime, tz: ZoneInfo, at: time) -> datetime:
    """`now` 之后下一个 `at`(严格之后:正好 `at` 那一刻,下一个是明天的)。"""
    return datetime.combine(slot_day(now, tz, at) + timedelta(days=1), at, tzinfo=tz)


_WINDOW = re.compile(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})$")


@dataclass(frozen=True)
class QuietHours:
    """静默时段 `[start, end)`,按配置时区读。`start > end` 就是跨午夜(23:00~07:00);
    `start == end` 表示没有静默时段。"""

    start: time
    end: time

    @classmethod
    def parse(cls, raw: str) -> "QuietHours":
        """`HH:MM-HH:MM`。写错在启动时炸,别等到半夜推送时才发现(和 PUSH_CHANNEL 同一个理由)。"""
        match = _WINDOW.fullmatch(raw.strip())
        if match is None:
            raise ValueError(
                f"LARARIUM_QUIET_HOURS 格式错:{raw!r},应为 HH:MM-HH:MM(如 02:00-08:00)"
            )
        h1, m1, h2, m2 = (int(g) for g in match.groups())
        try:
            return cls(start=time(h1, m1), end=time(h2, m2))
        except ValueError as exc:
            raise ValueError(f"LARARIUM_QUIET_HOURS 时刻不对:{raw!r}({exc})") from exc

    def contains(self, now: datetime, tz: ZoneInfo) -> bool:
        t = now.astimezone(tz).time()
        if self.start == self.end:
            return False
        if self.start < self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end  # 跨午夜

    def ends_at(self, now: datetime, tz: ZoneInfo) -> datetime:
        """`now` 之后这一段在几点结束。只在 `contains(now)` 时有意义:下一个 `end` 就是它。"""
        return next_slot(now, tz, self.end)

    def starts_at(self, now: datetime, tz: ZoneInfo) -> datetime:
        """`now` 之后下一段从几点开始。"""
        return next_slot(now, tz, self.start)
