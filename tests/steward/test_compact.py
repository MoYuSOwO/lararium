"""压缩(compact)测试(M3-6)——M3 最后一块硬骨头,八条逐条钉。

沉淀筛直接复用 M3-5 的 Sweeper:fixture 里用的是**真 Sweeper + 假模型**,
证明走的是 M3-5 那份实现,不是第二份。
"""

import json
import sqlite3
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

    def make(cut_model, sweep_model=None, index_days=90, timezone="Asia/Shanghai", notify=None):
        conn = connect(tmp_path / "steward.sqlite")
        ledger, gate = build_memory_components(tmp_path)
        journal = Journal(conn)
        threads = Threads(conn)

        async def _noop_sweep(prompt):
            return '{"open": [], "close": [], "suggest": []}'

        sweeper = Sweeper(
            journal, threads, gate, sweep_model or _noop_sweep, "测试归拢指令", ledger=ledger
        )
        compactor = Compactor(
            journal, gate, cut_model, "测试切段指令", sweeper, index_days, timezone, notify=notify
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


async def test_p1_compact_stop_notifies(compact_factory):
    """P1-3:压缩被审批屏障停 → 投 notice;用户不再毫不知情(死循环的解药)。"""
    from datetime import UTC, datetime, timedelta

    notices: list[str] = []

    async def cut(p):
        return '{"segments": [{"topic": "T", "conclusion": "C"}]}'

    compactor, _, journal, _, gate, _ = compact_factory(cut, notify=notices.append)
    gate.propose(
        kind="add", content="待审一条", provenance="untrusted", origin="test", section="长期偏好"
    )
    journal.append("env-1", "envelope", {"content": "聊天内容"})
    now = datetime.now(UTC)
    s, u = (now - timedelta(hours=1)).isoformat(), (now + timedelta(hours=1)).isoformat()
    result = await compactor.run(s, u)
    assert result.stopped and "审批屏障" in result.summary
    assert notices and "压缩暂停" in notices[0], "被挡住必须通知用户"


async def test_p1_death_loop_broken_sweep_proposal_blocks_then_notice_then_continues(
    compact_factory,
):
    """组合(死循环三段合起来是验收重点):归拢提出提案 → 压缩被自己挡住 → 但用户收到
    notice(知道该结案,死循环就解了);结案后再压缩**能继续**,不是永久挡死。"""
    from datetime import UTC, datetime, timedelta

    notices: list[str] = []

    async def cut(p):
        return '{"segments": [{"topic": "消费", "conclusion": "外卖超支"}]}'

    async def surg_rm(prompt):  # 沉淀筛的假模型:这次会提一条提案
        return '{"open": [], "close": [], "suggest": ["外卖超支"]}'

    compactor, _, journal, _, gate, _ = compact_factory(
        cut, sweep_model=surg_rm, notify=notices.append
    )
    journal.append("env-1", "envelope", {"content": "外卖这个月花超了"})
    now = datetime.now(UTC)
    s, u = (now - timedelta(hours=1)).isoformat(), (now + timedelta(hours=1)).isoformat()

    r1 = await compactor.run(s, u)
    assert r1.stopped and "沉淀筛" in r1.summary, "沉淀筛提的 pending 挡住第一轮压缩"
    assert notices, "被自己挡住不能悄悄——用户得收到 notice"
    assert not journal.is_compressed("env-1"), "被挡住时没索引没标记"

    for p in gate.pending():  # 用户结案(硬门控审完)
        gate.resolve(p.id, approved=True)
    r2 = await compactor.run(s, u)  # 同窗口:沉淀筛的光标已推进(无新内容),pending 已清
    assert r2.index_count == 1 and journal.is_compressed("env-1"), "结案后再压能继续,不是永久挡死"


# ── M5-30:前置步骤失败了,那批对话不许退出 L0 ────────────────────────────────
#
# `_cut` 失败时返回 `[]`,而 `[]` 同时也是"模型说没什么可切"——`run()` 分不出这两件事,
# 于是模型抖一下 = **索引一条没建,那批对话却已经被标记压缩**。正文没删(不可协商第 3 条
# 守着),但自动可见性没了,而"能翻到"和"会被翻到"不是一回事。
# 三条切段失败路径各钉一条,外加归拢失败、以及索引与标记之间那道缝。


def _nothing_committed(journal, *envelope_ids):
    """前置步骤没成功时的共同断言:一条索引都没建,一个信封都没退出 L0。"""
    assert journal.l1_block(90) == "", "前置步骤没成功,一条索引都不该建"
    for eid in envelope_ids:
        assert journal.is_compressed(eid) is False, f"{eid} 不许退出 L0"


async def test_cut_model_failure_keeps_the_batch_in_l0(compact_factory):
    """失败路径一:切段模型抛。那批 ids 不许被标记已压缩,索引也不许有。"""
    sweep_calls = []

    async def cut(p):
        raise RuntimeError("上游 502")

    async def sweep_p(p):
        sweep_calls.append(p)
        return '{"open": [], "close": [], "suggest": []}'

    compactor, conn, journal, _, _, _ = compact_factory(cut, sweep_model=sweep_p)
    journal.append("env-1", "envelope", {"content": "外卖这个月花超了"})
    s, u = _window()
    result = await compactor.run(s, u)

    assert result.failed and result.stopped, "切段失败 = 这一轮压缩没完成"
    assert result.compressed_count == 0 and result.index_count == 0
    assert "切段" in result.summary, f"摘要要说清楚卡在哪一步:{result.summary}"
    _nothing_committed(journal, "env-1")
    assert sweep_calls == [], "切段就没成,不必再往下烧一次模型调用"
    rows = list(conn.execute("SELECT payload FROM journal WHERE kind='sweep'"))
    assert any("切段模型失败" in r["payload"] for r in rows), "吞下来的失败必须落起居注"


async def test_unparseable_cut_output_keeps_the_batch_in_l0(compact_factory):
    """失败路径二:模型回了东西但 JSON 解不开(别只测抛异常那条)。

    这里原来有个「按一段整块处理」的兜底——把整窗压成一条内容是空话的索引行,
    然后照常标记压缩。那是**拿一条假书签换掉整批对话的自动可见性**,比不压更糟。
    """

    async def cut(p):
        return "抱歉,我没法完成这个请求。"

    compactor, _, journal, _, _, _ = compact_factory(cut)
    journal.append("env-1", "envelope", {"content": "外卖这个月花超了"})
    s, u = _window()
    result = await compactor.run(s, u)

    assert result.failed and result.stopped
    assert result.index_count == 0
    _nothing_committed(journal, "env-1")


async def test_zero_segments_for_a_nonempty_window_is_a_failure(compact_factory):
    """失败路径三:JSON 解得开、格式也对,但一段都没切出来。

    `ids` 非空却切出 0 段本身就是失败信号——有轮要压就该至少有一段;
    把它当成"成功地什么都没切"就正好走进了这条 bug。
    """

    async def cut(p):
        return '{"segments": []}'

    compactor, _, journal, _, _, _ = compact_factory(cut)
    journal.append("env-1", "envelope", {"content": "外卖这个月花超了"})
    s, u = _window()
    result = await compactor.run(s, u)

    assert result.failed and result.stopped
    _nothing_committed(journal, "env-1")


async def test_sweep_failure_blocks_compression(compact_factory):
    """归拢(沉淀筛)失败 → 压缩同样不提交。

    第 4 步「审批屏障再查」存在的理由正是"沉淀筛刚提的新 pending 也不能毁证据";
    筛子**根本没跑成**的时候,那一步查到的 0 条是**假的安全**。

    这条同时钉住了 compact 认定"归拢失败"的判据。M5-30 那版认的是摘要前缀;M6-8 起是
    `SweepResult.failed`——走的仍是真 Sweeper,sweep.py 哪天不再置这一位,这条当场红。
    """

    async def cut(p):
        return '{"segments": [{"topic": "消费", "conclusion": "外卖超支"}]}'

    async def sweep_boom(p):
        raise RuntimeError("上游 502")

    compactor, _, journal, _, _, _ = compact_factory(cut, sweep_model=sweep_boom)
    journal.append("env-1", "envelope", {"content": "外卖这个月花超了"})
    s, u = _window()
    result = await compactor.run(s, u)

    assert result.failed and result.stopped
    assert "归拢" in result.summary, f"摘要要说清楚卡在哪一步:{result.summary}"
    _nothing_committed(journal, "env-1")


async def test_sweep_not_finishing_does_not_block_compression(compact_factory):
    """判断的分界:归拢**失败**挡住压缩,归拢**没扫完**(批数上限)不挡。

    没扫完时筛子真的跑过、也真的提过它看到的那些,而且光标只推到实际喂进去的那条
    (M5-24),剩下的下次接着补。拿"没扫完"当红灯的话,积压期间压缩永远轮不上
    ——而那正是上下文最满、最需要压的时候。
    """

    async def cut(p):
        return '{"segments": [{"topic": "消费", "conclusion": "外卖超支"}]}'

    async def sweep_p(p):
        return '{"open": [], "close": [], "suggest": []}'

    compactor, conn, journal, _, _, _ = compact_factory(cut, sweep_model=sweep_p)
    # 造出"一次 run 扫不完":50 条、每条 3000 字 ≈ 15 万字,远超「6 批,每批 2 万字」的上限。
    for i in range(50):
        journal.append(f"env-{i}", "envelope", {"content": "账" * 3000})
    s, u = _window()
    result = await compactor.run(s, u)

    cursor = conn.execute("SELECT cursor_seq FROM sweep_state WHERE id=1").fetchone()["cursor_seq"]
    last = conn.execute("SELECT MAX(seq) AS s FROM journal WHERE kind='envelope'").fetchone()["s"]
    assert cursor < last, "阳性对照:这一批必须真的没扫完,否则下面几句钉不住任何东西"
    assert not result.failed and not result.stopped, "没扫完不是失败,压缩照常提交"
    assert result.index_count == 1 and result.compressed_count == 50
    assert journal.is_compressed("env-0")


async def test_index_and_mark_commit_together(compact_factory, monkeypatch):
    """`add_index` 和 `mark_compressed` 之间那道缝:索引写到一半崩掉,不许留下半份。

    留下半份的后果不是"少一行":标记没写上,下次同一窗口会重压,那半份索引跟着再写一遍。
    """

    async def cut(p):
        return json.dumps(
            {
                "segments": [
                    {"topic": "A", "conclusion": "c1", "envelope_ids": ["env-a"]},
                    {"topic": "B", "conclusion": "c2", "envelope_ids": ["env-b"]},
                ]
            }
        )

    compactor, _, journal, _, _, _ = compact_factory(cut)
    journal.append("env-a", "envelope", {"content": "第一段"})
    journal.append("env-b", "envelope", {"content": "第二段"})

    real_add = journal.add_index
    written: list[str] = []

    def boom(date, line, envelope_id):
        written.append(envelope_id)
        if len(written) == 2:
            raise sqlite3.OperationalError("database is locked")
        real_add(date, line, envelope_id)

    monkeypatch.setattr(journal, "add_index", boom)
    s, u = _window()
    with pytest.raises(sqlite3.OperationalError):
        await compactor.run(s, u)

    assert len(written) == 2, "阳性对照:第一条索引确实写进去过,崩的是第二条"
    assert journal.l1_block(90) == "", "半份索引必须跟着回滚,不能留在库里"
    _nothing_committed(journal, "env-a", "env-b")
