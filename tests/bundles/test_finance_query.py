"""M4-3 `query_spending` 的行为测试 —— 工具铁律(A4)的第一次实战。

三条来自 PLAN M4-3:按类目/按天聚合返回「总额 + 每组一行」/ 铁律回归(300 笔进去,
出来的行数有上限、正文里没有任何单笔流水)/ 空区间返回人话。

**铁律为什么值一条专门的回归**:「把三百条流水丢给模型自己算」既烧上下文又烧缓存——
流水膨胀会加速压缩,而压缩是全系统仅有的两个缓存重建点之一。这条测试守的不是
"返回值好不好看",是"一次查询不能顶穿 L0"。
"""

from pathlib import Path

import pytest
from bundles.finance.server import MAX_GROUP_ROWS, build

SHANGHAI = "Asia/Shanghai"
CATEGORY_CYCLE = ("餐饮", "交通", "日用", "娱乐")


def tools(tmp_path: Path):
    runtime = build(tmp_path, timezone=SHANGHAI)
    by_name = {f.__name__: f for f in runtime.tools}
    return by_name["record_expense"], by_name["query_spending"]


@pytest.fixture
def finance(tmp_path):
    return tools(tmp_path)


def test_groups_by_category_and_returns_total_plus_one_line_each(finance):
    """按类目聚合:一个总额 + 每个类目一行,金额是该类目的合计,不是流水。"""
    record, query = finance
    record(45, "餐饮", occurred_at="2026-08-03")
    record(55, "餐饮", occurred_at="2026-08-04")
    record(28, "交通", occurred_at="2026-08-05")

    said = query("2026-08-01", "2026-08-31", "category")

    assert "128.00" in said, "总额要在,且是 SQL 算出来的合计"
    assert "餐饮" in said and "100.00" in said
    assert "交通" in said and "28.00" in said
    assert "45.00" not in said and "55.00" not in said, "聚合结果里不该出现单笔金额"


def test_groups_by_day(finance):
    """按天聚合:一天一行,同一天的多笔合成一个数。"""
    record, query = finance
    record(45, "餐饮", occurred_at="2026-08-03T12:00:00")
    record(55, "交通", occurred_at="2026-08-03T19:00:00")
    record(28, "餐饮", occurred_at="2026-08-04")

    said = query("2026-08-01", "2026-08-31", "day")

    assert "2026-08-03" in said and "100.00" in said
    assert "2026-08-04" in said and "28.00" in said


def test_range_bounds_are_inclusive_on_both_ends(finance):
    """since/until 都含端点。

    这条防的是"当天最后一笔被吃掉":落库的是 `2026-08-31T20:00:00`,而 until 是
    `2026-08-31`——字符串比较下 `'2026-08-31T20:00:00' > '2026-08-31'`,用 `<= until`
    就会把当天所有带时刻的流水全漏掉,而月度合计只是"小了一点",没人会发现。
    """
    record, query = finance
    record(10, "餐饮", occurred_at="2026-08-01T00:00:00")
    record(20, "餐饮", occurred_at="2026-08-31T20:00:00")
    record(99, "餐饮", occurred_at="2026-09-01T00:00:00")

    said = query("2026-08-01", "2026-08-31", "category")

    assert "30.00" in said, "两端都该含进来"
    assert "99" not in said, "范围外的不许算进去"


def test_empty_range_says_so_in_plain_words(finance):
    """空区间返回人话,不是空字符串、不是异常、也不是「合计 0.00 元」。"""
    record, query = finance
    record(45, "餐饮", occurred_at="2026-07-01")

    said = query("2026-08-01", "2026-08-31", "category")

    assert said.strip()
    assert "没有记录" in said
    assert "2026-08-01" in said and "2026-08-31" in said


def test_three_hundred_rows_in_bounded_conclusions_out(finance, tmp_path):
    """**铁律回归**:300 笔进去,出来的必须是结论,而且行数有上限。

    每笔都带一个唯一的 note,断言正文里一条都不出现——这是"不返回原料"的可判定形式。
    """
    record, query = finance
    for i in range(300):
        day = 1 + i % 28
        record(
            1 + i % 7,
            CATEGORY_CYCLE[i % len(CATEGORY_CYCLE)],
            occurred_at=f"2026-08-{day:02d}T10:00:00",
            note=f"流水明细-{i}",
        )

    said = query("2026-08-01", "2026-08-31", "day")

    lines = said.splitlines()
    assert len(lines) <= MAX_GROUP_ROWS + 2, f"返回了 {len(lines)} 行,上限没兜住"
    for i in range(300):
        assert f"流水明细-{i}" not in said, "单笔流水漏进了聚合结果"


def test_truncation_is_announced_not_silent(finance):
    """超过上限时要说清楚"其余多少组合计多少",不许静默截断。

    静默截断读起来和"就这些"一模一样,模型会拿一个残缺的合计去下结论。
    """
    record, query = finance
    for day in range(1, 26):  # 25 天 > MAX_GROUP_ROWS(20)
        record(10 + day, "餐饮", occurred_at=f"2026-08-{day:02d}")

    said = query("2026-08-01", "2026-08-31", "day")

    assert "其余 5 组" in said, "被截掉的组数要报出来"


def test_unknown_group_by_returns_readable_hint(finance):
    """看不懂的 group_by 走 E2:列出合法值,不抛。"""
    record, query = finance
    record(45, "餐饮", occurred_at="2026-08-03")

    said = query("2026-08-01", "2026-08-31", "商家")

    assert "category" in said and "day" in said
    assert "商家" in said


def test_bad_date_returns_readable_hint(finance):
    """看不懂的日期同样走 E2,并且点明要 YYYY-MM-DD。"""
    _record, query = finance

    said = query("上个月", "2026-08-31", "category")

    assert "YYYY-MM-DD" in said
    assert "上个月" in said


def test_reversed_range_returns_readable_hint(finance):
    """since 晚于 until 是调用方搞反了,要说出来,不许假装成"没有记录"。"""
    _record, query = finance

    said = query("2026-08-31", "2026-08-01", "category")

    assert "没有记录" not in said
    assert "2026-08-31" in said and "2026-08-01" in said
