"""todos bundle 的骨架契约(M6-7):独占自己的库、工具顺序冻结、抄来的围栏常量钉住。

行为在 `test_todos_tools.py`。
"""

import sqlite3
from pathlib import Path

import yaml
from bundles.todos.server import build


def test_build_creates_own_sqlite_in_own_dir(tmp_path):
    """数据产权:库在 data_dir/todos/ 下,data_dir 里没有别的库。"""
    build(tmp_path, timezone="Asia/Shanghai")
    assert list(tmp_path.rglob("*.sqlite")) == [tmp_path / "todos" / "todos.sqlite"]


def test_build_is_idempotent_and_keeps_rows(tmp_path):
    """重启 / 反复装配不炸,也不冲掉已经记下的待办。"""
    add = next(
        f for f in build(tmp_path, timezone="Asia/Shanghai").tools if f.__name__ == "add_todo"
    )
    add(title="交实验报告", due="2026-09-18")
    build(tmp_path, timezone="Asia/Shanghai")
    conn = sqlite3.connect(tmp_path / "todos" / "todos.sqlite")
    try:
        assert conn.execute("SELECT count(*) FROM todos").fetchone()[0] == 1
    finally:
        conn.close()


def test_tool_order_is_frozen_and_matches_manifest(tmp_path):
    """工具顺序即冻结顺序(前缀第 0 层):manifest 声明 == 实现暴露,以后只许追加在末尾。"""
    manifest = yaml.safe_load(Path("bundles/todos/manifest.yaml").read_text(encoding="utf-8"))
    got = [f.__name__ for f in build(tmp_path, timezone="Asia/Shanghai").tools]
    assert list(manifest["tools"]) == [
        "add_todo",
        "list_todos",
        "complete_todo",
        "update_todo",
        "delete_todo",
    ]
    assert got == list(manifest["tools"])
    assert manifest["name"] == "todos"


def test_fence_markers_match_the_stewards():
    """todos 和 finance 一样抄了一份围栏常量(bundle 不许 import steward)。抄了就会漂,钉在一起。"""
    from bundles.todos.server import FENCE_CLOSE, FENCE_OPEN

    from lararium.steward.assembler import FENCE_CLOSE as STEWARD_CLOSE
    from lararium.steward.assembler import FENCE_OPEN as STEWARD_OPEN

    assert (FENCE_OPEN, FENCE_CLOSE) == (STEWARD_OPEN, STEWARD_CLOSE)
