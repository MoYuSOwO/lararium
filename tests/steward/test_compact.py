"""压缩(compact)测试(M3-6)——M3 最后一块硬骨头,八条逐条钉。

沉淀筛直接复用 M3-5 的 Sweeper:fixture 里用的是**真 Sweeper + 假模型**,
证明走的是 M3-5 那份实现,不是第二份。
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from bundles.memory.server import build_memory_components

from lararium.db import connect
from lararium.steward.compact import Compactor
from lararium.steward.journal import Journal
from lararium.steward.sweep import Sweeper
from lararium.steward.threads import Threads


@pytest.fixture
def compact_factory(tmp_path, monkeypatch):
    import lararium.steward.journal as _jm

    monkeypatch.setattr(_jm, "embed", lambda t: None)  # 压缩用不到语义向量,别拉模型

    def make(cut_model, sweep_model=None, index_days=90, timezone="Asia/Shanghai"):
        conn = connect(tmp_path / "steward.sqlite")
        ledger, gate = build_memory_components(tmp_path)
        journal = Journal(conn)
        threads = Threads(conn)

        async def _noop_sweep(prompt):
            return '{"open": [], "close": [], "suggest": []}'

        sweeper = Sweeper(journal, threads, gate, sweep_model or _noop_sweep, "测试归拢指令")
        compactor = Compactor(
            journal, gate, cut_model, "测试切段指令", sweeper, index_days, timezone
        )
        return compactor, conn, journal, threads, gate, ledger

    return make


def _window():
    now = datetime.now(UTC)
    return (now - timedelta(hours=1)).isoformat(), (now + timedelta(hours=1)).isoformat()


async def test_1_cuts_mixed_convo_into_segments(compact_factory):
    """切段:一段混合对话按话题切开,断言切出多段。"""
    calls = []

    async def cut(p):
        calls.append(p)
        return json.dumps(
            {
                "segments": [
                    {"topic": "月度复盘", "conclusion": "外卖超支"},
                    {"topic": "运动", "conclusion": "膝盖有点酸"},
                ]
            }
        )

    compactor, _, journal, _, _, _ = compact_factory(cut)
    journal.append("env-0", "envelope", {"content": "这个月外卖花了八百"})
    journal.append("env-0", "reply", {"content": "记下了,超支了"})
    journal.append("env-1", "envelope", {"content": "跑步三公里膝盖酸"})
    s, u = _window()
    result = await compactor.run(s, u)

    assert result.index_count == 2, "两段混合对话应切出两段"
    assert len(calls) == 1, "切段模型调一次"
    l1 = journal.l1_block(90)
    assert "· 月度复盘 · 外卖超支 · env-0" in l1
    assert "· 运动 · 膝盖有点酸 · env-1" in l1


async def test_2_cycles_sediment_through_real_sweeper(compact_factory):
    """沉淀筛:直接复用 M3-5 的 sweep(真 Sweeper 跑过,不许写第二份)。"""
    sweep_calls = []

    async def cut(p):
        return '{"segments": [{"topic": "T", "conclusion": "C"}]}'

    async def sweep_p(p):
        sweep_calls.append(p)
        return '{"open": [], "close": [], "suggest": []}'

    compactor, conn, journal, _, _, _ = compact_factory(cut, sweep_model=sweep_p)
    journal.append("env-1", "envelope", {"content": "聊了装修"})
    s, u = _window()
    result = await compactor.run(s, u)

    assert len(sweep_calls) == 1, "沉淀筛必须复用 M3-5 的 Sweeper,一份实现"
    assert result.index_count == 1
    # 真 Sweeper 的输入输出也落了起居注(sweep 事件)
    rows = list(conn.execute("SELECT payload FROM journal WHERE kind='sweep' ORDER BY seq"))
    assert len(rows) >= 2, "sweep 的 input/output 该有两条"


async def test_3_pending_barrier_stops_compression(compact_factory):
    """审批屏障:pending 非空时压缩必须停,不动手且说明原因。"""
    calls = []

    async def cut(p):
        calls.append(p)
        return '{"segments": []}'

    compactor, _, journal, _, gate, _ = compact_factory(cut)
    gate.propose(
        kind="add", content="待审一条", provenance="untrusted", origin="test", section="长期偏好"
    )
    journal.append("env-1", "envelope", {"content": "聊天内容"})
    s, u = _window()
    result = await compactor.run(s, u)

    assert result.stopped
    assert "审批屏障" in result.summary
    assert len(calls) == 0, "屏障在前,切段模型不该被调"
    assert journal.is_compressed("env-1") is False, "什么都没动"
    assert journal.l1_block(90) == "", "没写索引"


async def test_4_index_line_format_and_exclusion_from_l0(compact_factory):
    """索引:每段一行 日期 · 话题 · 一句结论 · 信封id,正文从 L0 退出。"""

    async def cut(p):
        return '{"segments": [{"topic": "消费", "conclusion": "外卖超支"}]}'

    compactor, _, journal, _, _, _ = compact_factory(cut)
    journal.append("env-0", "envelope", {"content": "外卖花超了"})
    journal.append("env-0", "reply", {"content": "记下了"})
    s, u = _window()
    await compactor.run(s, u)
    # 压缩后新来的这一轮不进本次窗口 → 保留在 L0;env-0 已压退
    journal.append("env-1", "envelope", {"content": "新的一轮"})

    l1 = journal.l1_block(90)
    assert "· 消费 · 外卖超支 · env-0" in l1, f"索引行格式:日期 · 话题 · 结论 · 信封id:\n{l1}"
    assert journal.is_compressed("env-0")
    # env-0 退出 L0 一线(正文仍在起居注,只不往近期上下文灌)
    keep = [t["envelope_id"] for t in journal.recent_turns_within_budget(max_tokens=10**9)]
    assert "env-0" not in keep and "env-1" in keep


async def test_5_compression_never_touches_prefix(compact_factory):
    """重写 L1:压缩只动 L1,前缀区(persona+目录+账本)逐字节不变。"""

    async def cut(p):
        return '{"segments": [{"topic": "T", "conclusion": "C"}]}'

    compactor, _, journal, _, _, ledger = compact_factory(cut)
    before = ledger.read()
    journal.append("env-1", "envelope", {"content": "聊天内容"})
    s, u = _window()
    await compactor.run(s, u)

    assert ledger.read() == before, "账本一行没动——压缩只写 l1_index/compressed 标记"
    # L1 在流水区,不进 system_prompt:同输入 assemble 的 system_prompt 逐字节相同
    from lararium.envelope import Envelope
    from lararium.steward.assembler import assemble

    led = ledger.read()
    env = Envelope.new(source="user", channel="cli", content="问")
    c1 = assemble(
        persona="P", directory="D", ledger=led, l1="", l0=[], envelope=env, timezone="Asia/Shanghai"
    )
    c2 = assemble(
        persona="P",
        directory="D",
        ledger=led,
        l1=journal.l1_block(90),
        l0=[],
        envelope=env,
        timezone="Asia/Shanghai",
    )
    assert c1.system_prompt == c2.system_prompt, "L1 是流水区的,不许进前缀"


async def test_8_does_not_recompress(compact_factory):
    """不反复压缩:已压成索引的信封不会再压一次(没有"摘要的摘要")。"""

    async def cut(p):
        return '{"segments": [{"topic": "T", "conclusion": "C"}]}'

    compactor, _, journal, _, _, _ = compact_factory(cut)
    journal.append("env-1", "envelope", {"content": "聊天内容"})
    s, u = _window()
    r1 = await compactor.run(s, u)
    r2 = await compactor.run(s, u)

    assert r1.index_count == 1
    assert r2.stopped and "没有未压缩" in r2.summary, "第二次同区间应无窗可压"
    assert journal.l1_block(90).count("env-1") == 1, "索引行只有一份,没重复"


def _raw_insert(conn, eid, kind, content, ts):
    """测试用:直接给起居注插一条带指定 ts 的事件(append 会用 now,这里要控日期)。"""
    payload = json.dumps({"content": content, "source": "user", "channel": "cli", "meta": {}})
    conn.execute(
        "INSERT INTO journal (envelope_id, kind, payload, search_text, ts) VALUES (?,?,?,?,?)",
        (eid, kind, payload, content, ts),
    )


async def test_hooks_and_dates_follow_segments_with_local_tz(compact_factory):
    """M3-6 补做:钩子/日期来自模型切段(id 校验),日期走配置时区——凌晨不差一天。"""

    async def cut(p):
        return json.dumps(
            {
                "segments": [
                    {"topic": "账", "conclusion": "花超", "envelope_ids": ["env-a"]},
                    {"topic": "装修", "conclusion": "在比价", "envelope_ids": ["env-b"]},
                    {"topic": "运动", "conclusion": "膝盖酸", "envelope_ids": ["env-c"]},
                ]
            }
        )

    compactor, conn, journal, _, _, _ = compact_factory(cut, timezone="Asia/Shanghai")
    days = [
        ("env-a", "2026-08-18T09:00:00+00:00", "记了笔账"),  # 上海 17:00 → 08-18
        ("env-b", "2026-08-19T08:00:00+00:00", "聊了装修"),  # 上海 16:00 → 08-19
        ("env-c", "2026-08-19T17:40:00+00:00", "跑了步"),  # 上海 08-20 01:40 → 次日
    ]
    for eid, ts, content in days:
        _raw_insert(conn, eid, "envelope", content, ts)
        _raw_insert(conn, eid, "reply", "收到", ts)
    await compactor.run("2026-08-18T00:00:00+00:00", "2026-08-21T00:00:00+00:00")

    lines = [ln for ln in journal.l1_block(90).splitlines() if ln.strip()]
    assert len(lines) == 3, lines
    assert "2026-08-18 · 账" in lines[0] and "env-a" in lines[0], lines
    assert "2026-08-19 · 装修" in lines[1] and "env-b" in lines[1], lines
    assert "2026-08-20 · 运动" in lines[2] and "env-c" in lines[2], (
        f"UTC 17:40 在 Asia/Shanghai 应是次日 08-20,不是 UTC 的 08-19:\n{lines}"
    )


async def test_hooks_fallback_when_model_gives_bad_id(compact_factory):
    """模型给不在窗口里的 id → 丢掉,该段退回按位置拿一个没分过的;id 不重复。"""

    async def cut(p):
        return json.dumps(
            {
                "segments": [
                    {"topic": "A", "conclusion": "c1", "envelope_ids": ["env-a"]},
                    {"topic": "B", "conclusion": "c2", "envelope_ids": ["ghost-id"]},  # 认不出
                    {
                        "topic": "C",
                        "conclusion": "c3",
                        "envelope_ids": ["env-b"],
                    },  # env-b 被 B 占了
                ]
            }
        )

    compactor, conn, journal, _, _, _ = compact_factory(cut, timezone="Asia/Shanghai")
    for i in range(3):
        _raw_insert(conn, f"env-{'abc'[i]}", "envelope", "内容", f"2026-08-19T0{i}:00:00+00:00")
    await compactor.run("2026-08-19T00:00:00+00:00", "2026-08-19T23:59:00+00:00")

    l1 = journal.l1_block(90)
    assert "ghost-id" not in l1, "认不出的 id 必须被丢"
    assert l1.count("env-") == 3, f"三段各拿一个窗口内的 id:\n{l1}"
    for eid in ("env-a", "env-b", "env-c"):
        assert eid in l1, eid
