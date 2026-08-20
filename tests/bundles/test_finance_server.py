"""finance bundle 的骨架契约测试(M4-1)。

M4-1 不测工具行为(record_expense 的行为在 test_finance_record.py),测的是四条骨架契约:
1. finance 独占自己的 SQLite,落在自己的目录下(§5 数据产权);
2. finance 的库与 steward 的库物理分离、零表重叠(不许碰 steward.sqlite);
3. 工具顺序冻结,且 manifest 声明 == 实现暴露(工具 schema 是前缀第0层);
4. skills/SKILL.md 存在且可读(分层路由第二层)。
"""

import sqlite3
from pathlib import Path

import pytest
import yaml
from bundles.finance.server import build


@pytest.fixture
def runtime(tmp_path):
    return build(tmp_path, timezone="Asia/Shanghai")


def test_build_creates_own_sqlite_in_own_dir(runtime, tmp_path):
    """finance 独占自己的库:文件在 data_dir/finance/ 下,data_dir 里没有别的库。"""
    assert (tmp_path / "finance" / "finance.sqlite").exists()
    dbs = list(tmp_path.rglob("*.sqlite"))
    assert dbs == [tmp_path / "finance" / "finance.sqlite"]


def test_finance_db_never_leaks_into_steward_sqlite(runtime, tmp_path):
    """跨模块零流量直到主控:finance 的表一个都不许出现在 steward 的库里。

    这是产权不变量,不是抽查——finance 的 build 只开自己的连接,结构上碰不到
    steward.sqlite;这条测试守住"以后别在 finance 里偷偷 connect(..steward..)"。
    """
    fin = sqlite3.connect(tmp_path / "finance" / "finance.sqlite")
    fin_tables = {
        r[0]
        for r in fin.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','virtual')"
            " AND name NOT LIKE 'sqlite_%'"
        )
    }
    fin.close()
    assert fin_tables  # 独占库里确实建了东西,重叠检查不是空断言

    from lararium.db import connect

    conn = connect(tmp_path / "steward.sqlite")
    stew_tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','virtual')"
            " AND name NOT LIKE 'sqlite_%'"
        )
    }
    conn.close()
    assert not (stew_tables & fin_tables), (
        f"finance 的表 {sorted(fin_tables & stew_tables)} 混进了 steward.sqlite——"
        "数据产权被破坏(流水不许上浮的物理层)。"
    )


def test_build_is_idempotent_and_reentrant(tmp_path):
    """服务器重启/测试反复装配不炸、不留重复表,也不清掉已有流水。"""
    tool = next(
        f for f in build(tmp_path, timezone="Asia/Shanghai").tools if f.__name__ == "record_expense"
    )
    tool(45, "餐饮")
    build(tmp_path, timezone="Asia/Shanghai")

    conn = sqlite3.connect(tmp_path / "finance" / "finance.sqlite")
    n_expenses = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='expenses'"
    ).fetchone()[0]
    n_rows = conn.execute("SELECT count(*) FROM expenses").fetchone()[0]
    conn.close()
    assert n_expenses == 1
    assert n_rows == 1, "重新 build 不许把已经记下的流水冲掉"


def test_tool_order_is_frozen_and_matches_manifest(runtime):
    """工具顺序即冻结顺序(§5):manifest 声明的顺序 == 实现暴露的顺序,从第二位起不许插队。

    manifest 的 tools 是设计时的权威;实现必须逐名对齐,否则前缀目录会撒谎。
    """
    manifest = yaml.safe_load(Path("bundles/finance/manifest.yaml").read_text(encoding="utf-8"))
    got = [f.__name__ for f in runtime.tools]
    assert list(manifest["tools"]) == [
        "record_expense",
        "query_spending",
        "list_recent",
    ]
    assert got == list(manifest["tools"])


def test_skill_overview_exists(runtime):
    root = Path("bundles/finance")
    assert (root / "skills" / "SKILL.md").exists()
    assert (root / "skills" / "SKILL.md").read_text(encoding="utf-8").lstrip().startswith("#")


def test_manifest_is_parseable_and_names_finance(runtime):
    manifest = yaml.safe_load(Path("bundles/finance/manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == "finance"
    assert manifest["description"] == "记账与消费分析"


def test_build_returns_uniform_runtime_shape(runtime):
    """统一构造入口:build(data_dir) -> BundleRuntime,至少带 tools: list[Callable]。
    memory 那套多的 ledger/gate 走 ports 单列(§6.1),领域 bundle 只要这个通用形状。"""
    from bundles.runtime import BundleRuntime

    assert isinstance(runtime, BundleRuntime)
    assert hasattr(runtime, "tools") and isinstance(runtime.tools, list)
