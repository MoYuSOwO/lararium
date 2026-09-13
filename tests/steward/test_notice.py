"""归拢的待审通知守静默时段(M6-9 顺带)。

M6-8 验收留给用户的问题:归拢 04:00 跑,跑完有待审,那条通知**当场发**——而用户凌晨两三点
还在记宵夜,四点很可能刚睡着,微信会响。所以:

- 静默时段里**不发,攒着**;时段结束那一刻发出去(待审不会过期);
- 仍然是**那一个**通知器、仍然**每天最多一条**,没有第二条推送路径;
- 攒着这件事**在库里**:重启不丢。

时间一律走假时钟。每次 `make(...)` 就是一次"进程起来"。
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from lararium.db import connect
from lararium.steward.journal import Journal
from lararium.steward.notice import make_daily_notifier
from lararium.steward.outbox import Outbox
from lararium.timeofday import QuietHours

TZ = "Asia/Shanghai"
SH = ZoneInfo(TZ)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2030, 1, day, hour, minute, tzinfo=SH)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def process(tmp_path):
    def make(clock: Clock, quiet: str = "02:00-08:00"):
        conn = connect(tmp_path / "steward.sqlite")
        notifier = make_daily_notifier(
            journal=Journal(conn),
            outbox=Outbox(conn),
            conn=conn,
            timezone=TZ,
            channel="wechat",
            quiet=QuietHours.parse(quiet),
            clock=clock,
        )
        return notifier, conn

    return make


def pushed(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT content FROM outbox ORDER BY seq").fetchall()]


def run_release(notifier, clock: Clock, until: datetime) -> list[datetime]:
    """照后台那一圈跑:判一次、睡到它说的时候。返回每次出件箱多出东西的时刻。"""
    conn = notifier.conn
    seen = len(pushed(conn))
    released: list[datetime] = []
    for _ in range(200):
        if clock() >= until:
            return released
        delay = notifier.step()
        if len(pushed(conn)) > seen:
            seen = len(pushed(conn))
            released.append(clock())
        clock.advance(delay)
    raise AssertionError("推了 200 步还没到头——醒的节奏不对")


def test_a_notice_raised_in_quiet_hours_is_held_not_pushed(process):
    """★ 变异「静默时段里归拢通知照发」的靶子。凌晨四点那一班提出了待审:一个字都不往外发。"""
    clock = Clock(at(10, 4, 0))
    notifier, conn = process(clock)

    notifier("夜间归拢提出 2 条待审提案(/pending 查看)")

    assert pushed(conn) == []
    assert conn.execute("SELECT count(*) FROM journal").fetchone()[0] == 0, (
        "没发出去就不许在起居注里留一轮:模型会以为自己说过"
    )


def test_the_held_notice_goes_out_the_moment_quiet_hours_end(process):
    clock = Clock(at(10, 4, 0))
    notifier, conn = process(clock)
    notifier("夜间归拢提出 2 条待审提案(/pending 查看)")

    released = run_release(notifier, clock, until=at(10, 12, 0))

    assert released == [at(10, 8, 0)], "要在时段结束**那一刻**发,不是之后某个整点轮询到的时候"
    assert pushed(conn) == ["夜间归拢提出 2 条待审提案(/pending 查看)"]
    # 发出去的那一刻才落成一轮完整对话(M4-7 的口径),由头是 sweep 那一支
    rows = conn.execute("SELECT kind, payload FROM journal ORDER BY seq").fetchall()
    assert [r["kind"] for r in rows] == ["envelope", "reply"]
    assert '"source": "sweep"' in rows[0]["payload"]


def test_held_survives_a_restart_and_the_day_still_gets_at_most_one(process):
    """攒着的判据在库里:04:00 攒下、进程崩了、08:00 之前重新起来——照样发,而且只发一条。"""
    clock = Clock(at(10, 4, 0))
    first, _ = process(clock)
    first("夜间归拢提出 2 条待审提案(/pending 查看)")

    clock.advance(3600)
    second, conn = process(clock)  # 重启
    second("压缩被待审挡住了")  # 时段里又来一条:已经攒着一条了,不再攒第二条

    run_release(second, clock, until=at(10, 9, 0))
    second("下午又提了一条")  # 当天名额已经被早上那条占了

    assert pushed(conn) == ["夜间归拢提出 2 条待审提案(/pending 查看)"]

    clock.now = at(11, 10, 0)
    second("第二天的")
    assert pushed(conn)[-1] == "第二天的", "名额按天算,第二天照常"


def test_a_window_across_midnight_holds_one_notice_not_one_per_date(process):
    """23:00~07:00:23:30 攒一条(属 10 号),00:30 又来一条(属 11 号,名额是空的)
    ——07:00 那一刻用户也只该收到**一条**。"""
    clock = Clock(at(10, 23, 30))
    notifier, conn = process(clock, quiet="23:00-07:00")
    notifier("第一条")
    clock.now = at(11, 0, 30)
    notifier("第二条")

    released = run_release(notifier, clock, until=at(11, 12, 0))

    assert released == [at(11, 7, 0)]
    assert pushed(conn) == ["第一条"]


def test_outside_quiet_hours_the_notice_goes_straight_out(process):
    clock = Clock(at(10, 15, 0))
    notifier, conn = process(clock)
    notifier("夜间归拢提出 1 条待审提案(/pending 查看)")
    assert pushed(conn) == ["夜间归拢提出 1 条待审提案(/pending 查看)"]
