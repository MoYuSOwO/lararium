"""finance bundle —— 记账与消费分析(对话侧)。

M4-1 只立骨架:manifest + 独占 SQLite + 统一构造入口 `build(data_dir)`。
三个工具的**签名与文档在此定死**(工具 schema 是前缀第0层,顺序冻结后不许再动);
正体实现在 M4-2/3/4 落进来——那时只换函数体、不动签名,前缀区在本里程碑之后再无重建
(注册表/工具变更 = 重启,D3)。
"""

import sqlite3
from collections.abc import Callable
from pathlib import Path

from fastmcp import FastMCP

from bundles.runtime import BundleRuntime

# 架构测试 test_only_the_ledger_module_writes_files 只放行 ledger.py 写文件;
# bundle 的库是 SQLite,写入走 sqlite3 连接,不落那条 AST 的禁写面。M4-2 起扩展
# expenses 表(金额整数分),M4-4 补 list_recent 的索引,都不动本文件的构造契约。
_FINANCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _ensure_db(root: Path) -> None:
    """finance 独占自己的库(§5 数据产权):只碰 data_dir/finance/finance.sqlite。"""
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "finance.sqlite", isolation_level=None, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_FINANCE_SCHEMA)
    finally:
        conn.close()


def _tool_functions() -> list[Callable]:
    """M4-1 骨架态:签名/文档即最终版本(工具顺序冻结),函数体在 M4-2/3/4 逐个替换。
    工具边界不许抛异常(E2)——没接通时给模型一句人话,让它知道这步做不了而不是整轮炸掉。
    """

    def record_expense(
        amount: float,
        category: str,
        occurred_at: str | None = None,
        note: str | None = None,
    ) -> str:
        """记一笔支出。amount 为元(内部转整数分存储,不存浮点);category 必须是固定
        类目之一(餐饮|交通|日用|娱乐|医疗|人情|其他),非法类目返回可读提示并列出合法值;
        occurred_at 缺省用当前时间,给了就用给的。"""
        return "记一笔还没接通(本里程碑下一步实现),这笔先没记。"

    def query_spending(
        since: str,
        until: str,
        group_by: str,
    ) -> str:
        """按类目/按天聚合一段时间内的支出(since/until 格式 YYYY-MM-DD),返回总额 +
        每组一行结论;聚合在 SQL 里算完再返回,**绝不返回单笔流水**。"""
        return "查账还没接通(本里程碑后续实现),暂时查不了。"

    def list_recent(limit: int = 10) -> str:
        """列出最近几笔流水——全系统唯一返回原始流水的工具,因此硬封顶(上限 20),
        limit 为负数或超大值都钳制到上限。"""
        return "查最近流水还没接通(本里程碑后续实现),暂时查不了。"

    return [record_expense, query_spending, list_recent]


def build(data_dir: Path) -> BundleRuntime:
    """统一构造入口(bundle 契约):`build(data_dir) -> BundleRuntime`,至少含
    tools: list[Callable]。工具顺序由 manifest.yaml 与测试钉死(前缀第0层)。"""
    _ensure_db(Path(data_dir) / "finance")
    return BundleRuntime(tools=_tool_functions())


def create_server(data_dir: Path) -> FastMCP:
    """MCP 服务入口,和 memory 同形状;生产单独容器时由它接管。"""
    mcp = FastMCP("finance")
    for fn in build(data_dir).tools:
        mcp.tool()(fn)
    return mcp


if __name__ == "__main__":
    import os

    create_server(Path(os.environ.get("LARARIUM_DATA_DIR", "./data"))).run()
