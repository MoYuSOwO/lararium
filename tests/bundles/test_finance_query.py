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


def test_a_full_year_by_day_is_still_bounded(finance):
    """上限放宽到装得下整月之后,「查一年 = 365 行」这条仍要防住。"""
    record, query = finance
    for month in range(1, 13):
        for day in (1, 15, 28):
            record(10, "餐饮", occurred_at=f"2026-{month:02d}-{day:02d}")

    said = query("2026-01-01", "2026-12-31", "day")

    assert len(said.splitlines()) <= MAX_GROUP_ROWS + 2
    assert "更早 5 天合计" in said


def test_day_groups_come_back_in_chronological_order(finance):
    """按天必须是**时间正序**,不是金额降序。

    分组键本身就是时间序,按金额降序等于把时间轴打散——日子会跳着来
    (07-04 / 07-25 / 07-11 …)。而且这两种排序不对称:「哪几天花得多」从正序的
    31 行里一眼能挑出来,「趋势」从金额 top-N 里**推不出来**。正序严格更强。
    monthly-review 的第一句方法就是「先看总额趋势」,照降序的输出做不到。
    """
    record, query = finance
    for day, amount in ((11, 5), (4, 90), (25, 40), (5, 70)):
        record(amount, "餐饮", occurred_at=f"2026-07-{day:02d}")

    said = query("2026-07-01", "2026-07-31", "day")

    days = [line.split()[1] for line in said.splitlines()[1:]]
    assert days == ["2026-07-04", "2026-07-05", "2026-07-11", "2026-07-25"]


def test_a_whole_month_by_day_is_never_truncated(finance):
    """整月按天查是财务 bundle 最常见的一次查询,**必须原样装得下**。

    上限不该照抄 MAX_SEARCH_HITS(20),该按"最常见的那个查询要装得下"来定:
    31 天一样防得住「查一年 = 365 行」。
    """
    record, query = finance
    for day in range(1, 32):
        record(10 + day, "餐饮", occurred_at=f"2026-07-{day:02d}")

    said = query("2026-07-01", "2026-07-31", "day")

    assert "未逐条列出" not in said, "整月按天被截断了——最常见的查询装不下"
    assert len(said.splitlines()) == 32, "表头 + 31 天,一天都不许少"
    for day in range(1, 32):
        assert f"2026-07-{day:02d}" in said


def test_day_truncation_keeps_the_most_recent_and_announces_the_earlier_part(finance):
    """真超限(查一年)时保留**最近**那段,更早的报成一行合计,不许静默截断。

    静默截断读起来和"就这些"一模一样,模型会拿残缺的合计去下结论。保留最近而不是
    最早:问"今年花了多少"的人,关心的是近况。
    """
    record, query = finance
    for day in range(1, 32):
        record(10, "餐饮", occurred_at=f"2026-05-{day:02d}")  # 31 天
    for day in range(1, 11):
        record(20, "餐饮", occurred_at=f"2026-06-{day:02d}")  # 再 10 天,共 41 天

    said = query("2026-05-01", "2026-06-30", "day")

    assert "更早 10 天合计 100.00 元" in said, "被砍掉的是最早那 10 天,要报出组数和合计"
    assert "2026-05-11" in said and "2026-06-10" in said, "最近的 31 天要留着"
    assert "2026-05-10" not in said, "最早那段不许逐条列出"
    assert "510.00" in said, "总额始终是全区间的(31*10 + 10*20)"


def test_group_by_accepts_the_forms_the_model_actually_writes(finance):
    """同义词表要收模型最容易写出的形式,包括带"按"字的。

    同义词表是代码、零前缀代价;让模型因为写了"按天"吃一次 E2 往返是白烧钱。
    """
    record, query = finance
    record(45, "餐饮", occurred_at="2026-08-03")

    for form in ("按类目", "类目", "分类", "按分类", "CATEGORY"):
        assert "餐饮 45.00 元" in query("2026-08-01", "2026-08-31", form)
    for form in ("按天", "天", "日", "按日", "date"):
        assert "2026-08-03 45.00 元" in query("2026-08-01", "2026-08-31", form)


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
