"""一天里的某个钟点(M6-9 从 nightly 搬出来的那两个小函数)+ 静默时段。

静默时段两边都要守:隔一阵问一嘴(不醒、不发、不攒)和归拢的待审通知(攒到时段结束再发)。
**跨午夜的时段**(23:00~07:00)和不跨的(04:00~12:00)判法不一样,这里两种都钉住。
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from lararium.timeofday import QuietHours, next_slot, slot_day

SH = ZoneInfo("Asia/Shanghai")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2030, 1, day, hour, minute, tzinfo=SH)


def test_a_quiet_window_that_does_not_cross_midnight():
    quiet = QuietHours.parse("04:00-12:00")
    assert not quiet.contains(at(10, 3, 59), SH)
    assert quiet.contains(at(10, 4, 0), SH)
    assert quiet.contains(at(10, 11, 59), SH)
    assert not quiet.contains(at(10, 12, 0), SH), "结束那一刻已经不静默了"
    assert not quiet.contains(at(10, 23, 0), SH)
    assert quiet.ends_at(at(10, 5, 0), SH) == at(10, 12, 0)


def test_a_quiet_window_that_crosses_midnight():
    quiet = QuietHours.parse("23:00-07:00")
    assert not quiet.contains(at(10, 22, 59), SH)
    assert quiet.contains(at(10, 23, 0), SH)
    assert quiet.contains(at(11, 0, 30), SH)
    assert quiet.contains(at(11, 6, 59), SH)
    assert not quiet.contains(at(11, 7, 0), SH)
    assert not quiet.contains(at(11, 12, 0), SH), "白天不许被当成跨午夜那一段"
    # 23:30 进去的,结束在**第二天** 07:00;00:30 进去的,结束在当天 07:00
    assert quiet.ends_at(at(10, 23, 30), SH) == at(11, 7, 0)
    assert quiet.ends_at(at(11, 0, 30), SH) == at(11, 7, 0)


def test_the_default_window_is_two_to_eight():
    quiet = QuietHours.parse("02:00-08:00")
    assert not quiet.contains(at(10, 1, 59), SH)
    assert quiet.contains(at(10, 2, 0), SH)
    assert not quiet.contains(at(10, 8, 0), SH)


def test_quiet_hours_are_read_in_the_configured_timezone():
    """同一个瞬间,上海 03:00 是静默的;按 UTC 读是 19:00,不是。"""
    quiet = QuietHours.parse("02:00-08:00")
    instant = at(10, 3, 0)
    assert quiet.contains(instant, SH)
    assert not quiet.contains(instant, ZoneInfo("UTC"))


def test_equal_ends_mean_never_quiet():
    quiet = QuietHours.parse("00:00-00:00")
    assert not any(quiet.contains(at(10, h), SH) for h in range(24))


@pytest.mark.parametrize("raw", ["2-8", "02:00", "25:00-08:00", "02:00-08:60", "abc"])
def test_a_malformed_window_is_refused_at_startup(raw):
    with pytest.raises(ValueError, match="LARARIUM_QUIET_HOURS"):
        QuietHours.parse(raw)


def test_slot_day_and_next_slot_take_the_time_of_day():
    """从 nightly 搬出来的时候多了一个参数 `at`:归拢用 04:00,静默时段用它的起止。"""
    four = at(10, 4, 0).timetz().replace(tzinfo=None)
    assert slot_day(at(10, 3, 59), SH, four).isoformat() == "2030-01-09"
    assert slot_day(at(10, 4, 0), SH, four).isoformat() == "2030-01-10"
    assert next_slot(at(10, 3, 59), SH, four) == at(10, 4, 0)
    assert next_slot(at(10, 4, 0), SH, four) == at(11, 4, 0)
