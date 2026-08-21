"""M4-2 `record_expense` 的行为测试。

四条来自 PLAN M4-2 的验收点:落库并返回人话确认 / 金额存整数分 / 类目是固定小集合 /
`occurred_at` 缺省为"现在"。外加两条 E2 边界(坏金额、坏时间都不许抛)。

**为什么这里直接查库**(T1 说测行为不测实现):"金额存整数分,不存浮点"和
"落库的时间不带时区偏移"本身就是**存储层的约定**,不是可以从返回文本推断的东西——
返回文本显示 45.00 元,底下存的是 4500 还是 45.0 只有查库能分清。而这两条各自防着一个
真实的坑:浮点漂移会让月度合计对不上账;带偏移的 ISO 串会让 M4-3 的 `date()` 分组
按 UTC 切天(`date('2026-08-21T01:00:00+08:00')` = 2026-08-20),整月账错一天。
"""

import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from bundles.finance.server import CATEGORIES, build

SHANGHAI = "Asia/Shanghai"


def tool(runtime, name: str):
    return next(f for f in runtime.tools if f.__name__ == name)


def rows(data_dir: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(data_dir / "finance" / "finance.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM expenses ORDER BY id"))
    finally:
        conn.close()


@pytest.fixture
def record(tmp_path):
    return tool(build(tmp_path, timezone=SHANGHAI), "record_expense")


def test_records_the_expense_and_confirms_in_plain_words(record, tmp_path):
    """记一笔:落库,并且回一句人话(带金额和类目),不是 OK 也不是一坨 JSON。"""
    said = record(45, "餐饮", note="公司楼下")

    got = rows(tmp_path)
    assert len(got) == 1
    assert got[0]["category"] == "餐饮"
    assert got[0]["note"] == "公司楼下"
    assert "45" in said and "餐饮" in said


def test_amount_is_stored_as_integer_cents_without_float_drift(record, tmp_path):
    """金额存整数分,且四舍五入定在 Decimal 上,不是 int(amount * 100)。

    浮点存法会把误差带进账,月度合计以「对不上一分钱」的形式冒出来,
    而那时候你已经不记得是哪笔的问题了。

    **这几个值是挑过的,不许随手改成"更自然"的数字。** 写这条测试时第一版用的是
    0.1/0.2,跑变异检查(把实现换成 `amount * 100`)时它照样绿——两个原因叠在一起:
    `0.1 * 100` 在 IEEE754 里正好是 10.0(乘法不漂,漂的是 0.1+0.2),而 SQLite 的
    INTEGER 亲和性还会把整数值的浮点悄悄收成整数(10.0 → integer 10)。也就是说
    浮点实现能大摇大摆地过那一版测试。真会露馅的是小数第三位:`1.005 * 100` =
    100.49999999999999,浮点路径要么截成 100(少收一分)、要么原样落成 REAL;
    Decimal 四舍五入得 101。
    """
    record(28.35, "交通")
    record(1.005, "其他")
    record(33.333, "其他")

    got = rows(tmp_path)
    assert [r["amount_cents"] for r in got] == [2835, 101, 3333]

    conn = sqlite3.connect(tmp_path / "finance" / "finance.sqlite")
    types = [r[0] for r in conn.execute("SELECT typeof(amount_cents) FROM expenses")]
    conn.close()
    assert types == ["integer"] * 3, "金额列必须是整数,不许是 REAL"


def test_illegal_category_returns_readable_hint_and_records_nothing(record, tmp_path):
    """类目是固定小集合:非法类目要给可读提示 + 列出合法值(E2 不抛),且不许落库。

    为什么固定:自由文本会让模型每次发明新词(「吃饭」「餐饮」「外卖」各记一笔),
    聚合就废了——M4-3 的 GROUP BY 是照着这个小集合设计的。
    """
    said = record(45, "外卖")

    assert rows(tmp_path) == []
    for legal in CATEGORIES:
        assert legal in said, "提示里必须列全合法类目,模型才能自己纠正重试"
    assert "外卖" in said


def test_occurred_at_defaults_to_now_in_the_configured_timezone(tmp_path):
    """`occurred_at` 缺省为"现在",而且是**配置时区**的现在,不是操作系统本地时间。

    M1 Task 9 的教训:VPS 跑在 UTC 上,账就会和 current_time 差 8 小时——
    "今天中午吃饭"记成昨天,月末那几笔还会跨月。
    """
    for tz in (SHANGHAI, "America/New_York"):
        root = tmp_path / tz.replace("/", "_")
        tool(build(root, timezone=tz), "record_expense")(45, "餐饮")
        stored = datetime.fromisoformat(rows(root)[0]["occurred_at"])
        assert stored.tzinfo is None, "落库的是本地墙上时间,不带偏移(见模块 docstring)"
        expected = datetime.now(ZoneInfo(tz)).replace(tzinfo=None)
        assert abs((stored - expected).total_seconds()) < 5

    sh = datetime.fromisoformat(rows(tmp_path / "Asia_Shanghai")[0]["occurred_at"])
    ny = datetime.fromisoformat(rows(tmp_path / "America_New_York")[0]["occurred_at"])
    assert sh != ny, "两个时区记出同一个时间戳 = 根本没看时区,读的是系统本地时间"


def test_given_occurred_at_is_used_as_given(record, tmp_path):
    """给了就用给的:模型算好日期传进来,bundle 不许自作主张改成"现在"。"""
    record(45, "餐饮", occurred_at="2026-08-19T12:30:00")
    record(20, "交通", occurred_at="2026-08-18")

    assert [r["occurred_at"] for r in rows(tmp_path)] == [
        "2026-08-19T12:30:00",
        "2026-08-18T00:00:00",
    ]


def test_offset_aware_occurred_at_is_converted_into_the_configured_timezone(record, tmp_path):
    """带偏移的时间要先换算到配置时区再落库,不许原样存。

    原样存下去,M4-3 的 `date(occurred_at)` 会按 UTC 切天,整月的分组静悄悄错一天。
    """
    record(45, "餐饮", occurred_at="2026-08-19T23:30:00+00:00")

    assert rows(tmp_path)[0]["occurred_at"] == "2026-08-20T07:30:00"


def test_unparseable_occurred_at_returns_readable_hint_and_records_nothing(record, tmp_path):
    """看不懂的时间要给可读提示,不许抛,也不许悄悄退回"现在"。

    退回"现在"是最坏的选择:模型说的是「上周三」,账上落的是今天,而没有任何人会知道。
    相对时间该由模型先调 current_time 换算(finance 的 SKILL.md 写了这条)。
    """
    said = record(45, "餐饮", occurred_at="上周三")

    assert rows(tmp_path) == []
    assert "上周三" in said and "YYYY-MM-DD" in said


def test_non_positive_amount_returns_readable_hint_and_records_nothing(record, tmp_path):
    """0 和负数不是支出。落进去会让月度合计变成一道谜题。"""
    for bad in (0, -28):
        said = record(bad, "餐饮")
        assert "金额" in said

    assert rows(tmp_path) == []


def test_absurdly_large_amount_returns_readable_hint_instead_of_escaping(record, tmp_path):
    """大到 SQLite 存不下的金额也得走 E2,不许把异常扔出工具边界。

    `1e17` 元换算成分是 1e19,超出 int64(约 9.2e18),sqlite3 绑定时抛 OverflowError
    ——而 `except sqlite3.Error` 接不住它。真人说不出这个数,但 E2 的意义正是**边界上
    不推演可能性**:异常逃出去,那条信封会被毒消息范式标 failed 再冒泡(worker 活着,
    不致命),可它是**无声**死的,模型连自我纠正的机会都没有。M3-1「负数在 SQLite 里
    = 不限制」是同一类教训。
    """
    for bad in (1e17, -1e17, 10**19):
        said = record(bad, "餐饮")
        assert "金额" in said

    assert rows(tmp_path) == []
