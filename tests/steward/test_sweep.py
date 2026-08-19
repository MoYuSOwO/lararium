"""夜间归拢(sweep)测试(M3-5)。

验收方三条盯点:
1. 只改话头 + 提 pending 提案,账本一行不动(任何账本写入都必须走 Gate.settle);
2. 模型参与的输入输出都落起居注(可见即入账,不是后台任务就绕过);
3. 处理全部 open 话头,不只 open_threads() 露出的前 5(掉出前 5 名的那批没人能关)。
"""

import json

import pytest
from bundles.memory.server import build_memory_components

from lararium.db import connect
from lararium.steward.journal import Journal
from lararium.steward.sweep import Sweeper
from lararium.steward.threads import Threads

S, U = "2026-08-01T00:00:00+00:00", "2026-08-02T00:00:00+00:00"


@pytest.fixture
def sweeper_factory(tmp_path):
    def make(run_model, instructions="测试指令"):
        conn = connect(tmp_path / "steward.sqlite")
        ledger, gate = build_memory_components(tmp_path)
        return (
            Sweeper(Journal(conn), Threads(conn), gate, run_model, instructions),
            conn,
            gate,
            ledger,
        )

    return make


async def test_sweep_closes_thread_that_is_out_of_top5(sweeper_factory):
    """掉出 open_threads 前 5 名的话头仍是 open——归拢的 prompt 要能看到全部,
    别只处理露出来的那 5 条(M3-4 记的那笔)。"""
    calls: list[str] = []

    async def rm(prompt):
        calls.append(prompt)
        return json.dumps({"open": [], "close": ["话题0"], "suggest": []})

    sweeper, conn, _, _ = sweeper_factory(rm)
    for i in range(6):
        sweeper._threads.open_thread(f"话题{i}", "n")
    assert "话题0" not in [t.topic for t in sweeper._threads.open_threads()], "话题0 应掉出前5"

    result = await sweeper.run(S, U)
    assert "话题0" in calls[0], "prompt 必须列出全部 open 话头(含掉出前5的),模型才看得到"
    row = conn.execute("SELECT state FROM threads WHERE topic='话题0'").fetchone()
    assert row["state"] == "closed", "掉出前5 的话头也能被归拢关掉"
    assert "话题0" in result.closed


async def test_sweep_suggests_untrusted_fact_and_never_touches_ledger(sweeper_factory):
    """最要紧的一条:归拢只改话头 + 提 pending 提案,账本一行不动。
    往账本写一个字都必须走 Gate.settle()——这里是 propose 进 pending,没 settle。"""

    async def rm(prompt):
        return json.dumps({"open": [], "close": [], "suggest": ["对芒果过敏"]})

    sweeper, _, gate, ledger = sweeper_factory(rm)
    before = ledger.read()
    result = await sweeper.run(S, U)

    pending = gate.pending()
    assert len(pending) == 1
    assert pending[0].provenance == "untrusted", "模型从对话推断的,不是亲口说,必须硬门控"
    assert pending[0].content == "对芒果过敏"
    assert ledger.read() == before, "归拢绝不能直接写账本(单写者:只有 Gate.settle())"
    assert gate.unsettled_count() == 0, "没 settle——提案还躺在 pending 等审批"
    assert result.suggested == 1


async def test_sweep_journals_exact_model_input_and_output(sweeper_factory):
    """模型实收的那份必须落起居注(sweep 事件 input/output),不因后台任务就绕过。"""
    model_txt = '{"open": [{"topic": "装修", "note": "在比价"}], "close": [], "suggest": []}'
    seen: list[str] = []

    async def rm(prompt):
        seen.append(prompt)
        return model_txt

    sweeper, conn, _, _ = sweeper_factory(rm)
    sweeper._threads.open_thread("租房", "n")
    await sweeper.run(S, U)

    rows = list(conn.execute("SELECT payload FROM journal WHERE kind='sweep' ORDER BY seq"))
    assert len(rows) == 2, "input + output 两条"
    inh = json.loads(rows[0][0])
    outh = json.loads(rows[1][0])
    assert inh["phase"] == "input"
    assert inh["content"] == seen[0], "落的输入必须是模型实收的那份(逐字)"
    assert "租房" in inh["content"], "输入含全部 open 话头"
    assert outh["phase"] == "output"
    assert outh["content"] == model_txt, "落的输出必须是模型原文"


async def test_sweep_same_range_is_idempotent(sweeper_factory):
    """同一区间重复跑是 no-op:不重调模型、不再提提案。"""
    calls: list[str] = []

    async def rm(prompt):
        calls.append(prompt)
        return json.dumps({"open": [], "close": [], "suggest": ["喜欢喝美式"]})

    sweeper, _, gate, _ = sweeper_factory(rm)
    await sweeper.run(S, U)
    r2 = await sweeper.run(S, U)
    assert r2.skipped, "第二次同区间应跳过"
    assert len(calls) == 1, "模型只该被调一次"
    assert len(gate.pending()) == 1, "只提了一条,不因重跑重复提案"


async def test_sweep_model_failure_does_not_break(sweeper_factory):
    """扫描失败不影响主循环:返回可读结果、input 已入账、可重试。"""

    async def rm(prompt):
        raise RuntimeError("模型挂了")

    sweeper, conn, _, _ = sweeper_factory(rm)
    result = await sweeper.run(S, U)
    assert "归拢失败" in result.summary
    rows = list(conn.execute("SELECT payload FROM journal WHERE kind='sweep' ORDER BY seq"))
    assert rows and json.loads(rows[0][0])["phase"] == "input", "输入即使失败也已入账"
    # 没成功的区间不 mark swept → 可重试
    assert result.skipped is False


async def test_sweep_non_json_output_is_noop(sweeper_factory):
    async def rm(prompt):
        return "这不是 JSON,别听它的"

    sweeper, conn, gate, _ = sweeper_factory(rm)
    result = await sweeper.run(S, U)
    assert "不是 JSON" in result.summary
    assert len(gate.pending()) == 0
    assert row_count(conn, "threads") == 0  # 没开/没关任何话头


def row_count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
