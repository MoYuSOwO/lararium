from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from fastmcp import FastMCP

from bundles.memory.gate import Gate
from bundles.memory.ledger import Ledger, memory_schema
from lararium.db import open_connection


def build_memory_components(data_dir: Path) -> tuple[Ledger, Gate]:
    root = Path(data_dir) / "memory"
    root.mkdir(parents=True, exist_ok=True)
    # M5-8:走 `open_connection` 而不是裸 sqlite3——**bundle 的库面对的是同一个线程池、
    # 同一个洞**。一条 assistant 消息里两个 `propose_fact` 并发跑,这条连接照样会烂。
    # 只 import `lararium.db`(基础设施),没碰 steward/gateway,契约照旧。
    conn = open_connection(root / "memory.sqlite")
    conn.executescript(memory_schema())
    ledger = Ledger(root / "ledger.md", conn)
    ledger.ensure_initialized()  # 唯一允许新建账本文件的地方:启动时
    ledger.sync_manual_edit()
    return ledger, Gate(ledger, conn)


def read_ledger(data_dir: Path) -> str:
    """代码级读取:账本全量注入前缀,所以**不做成 MCP 工具**——不能让模型"记得去查"
    才看得见事实(DESIGN §6.6)。

    每次调用都会新建一个连接,只供测试与外部脚本使用。对话循环里请复用
    Steward 持有的 Ledger 实例(`self.ledger.read()`),不要每轮调这个函数。
    """
    ledger, _ = build_memory_components(data_dir)
    return ledger.read()


class MemoryTools(NamedTuple):
    """Memory 交出来的两个工具,**按字段名拿,不按工具名认**(M6-6d 第零个坑)。

    组装根要把提案那个函数**对象**交给 Steward 去套 P0-1 守卫(不可信轮强制降档)。
    原来 Steward 拿 `__name__` 和一个字符串字面量比,注册时一加前缀那个 `if` 就永远不成立,
    守卫整个脱落,而且不报错。字段是 mypy 看得见的属性,改名改不掉它;
    它仍然是个元组,迭代出来就是冻结顺序的工具列表(`for fn in ...`、`[0]` 照旧能用)。
    """

    propose_fact: Callable[..., str]
    list_pending: Callable[..., list[dict]]


def memory_tool_functions(gate: Gate) -> MemoryTools:
    """**模型能碰的** Memory 工具,唯一定义处。进程内挂载与 MCP 注册共用,
    避免两条路径漂移。顺序固定——工具 schema 是前缀第0层(DESIGN §4)。

    这里**只有两个**,而且都不能直接改账本:
    - `memory__propose_fact` 只能把内容放进 pending 隔离区;
    - `memory__list_pending` 只读。

    审批(resolve)、结算(settle)、回滚(rollback)一律**不在这个列表里**。
    它们是 `Gate` / `Ledger` 的普通方法,只由 CLI 命令(M1)或 IM 按钮回调(M2)调用
    ——即 DESIGN §6.3 的「按钮回调走代码状态流转,不过模型」。

    为什么这条界线是硬的:门控防的是"被注入的模型"。如果审批本身是模型可调的工具,
    那么被注入的模型只需连调两次(propose 然后 approve)就能把恶意事实永久写进账本,
    整套门控形同虚设——它只挡得住一个还听话的模型,而听话的模型本来就不需要挡。
    """

    def propose_fact(
        kind: str,
        content: str,
        provenance: str,
        section: str | None = None,
        old_text: str | None = None,
    ) -> str:
        """递交一条账本变更提案。kind: add|amend|retire。
        provenance: user_stated(用户亲口说,自动放行)| untrusted(外部数据,需用户审批)。
        add 必须给 section(身份|关系|长期偏好|正在进行);amend/retire 必须给 old_text。"""
        try:
            # kind/provenance 是模型传来的 str,gate.propose 在运行时校验合法值;
            # Literal 约束的是代码侧调用方,工具边界故意放宽——模型输出不可信输入(L3)
            p = gate.propose(
                kind=kind,  # type: ignore[arg-type]
                content=content,
                provenance=provenance,  # type: ignore[arg-type]
                origin="steward",
                section=section,
                old_text=old_text,
            )
        except ValueError as exc:
            return f"提案被拒绝:{exc}"
        if p.state == "passed":
            return f"已记下(提案 {p.id[:8]},将在下次结算落盘):{content}"
        return f"已提交待审(提案 {p.id[:8]}),需用户确认后才会入账本:{content}"

    def list_pending() -> list[dict]:
        """列出等待用户审批的提案。呈现给用户时必须说明这是待审内容,不是已确认的事实。"""
        return [
            {
                "id": p.id,
                "kind": p.kind,
                "content": p.content,
                "old_text": p.old_text,
                "section": p.section,
                "origin": p.origin,
            }
            for p in gate.pending()
        ]

    return MemoryTools(propose_fact=propose_fact, list_pending=list_pending)


def create_server(data_dir: Path) -> FastMCP:
    """MCP 形状(以后拆容器用)。**工具注册裸名**:命名空间是 MCP 客户端的事——客户端把服务名
    接在前面(Claude Code 的 `mcp__<服务>__<工具>`、pydantic-ai 的 `.prefixed()`),服务端
    再带一份前缀就成了双重前缀。拆容器那天,Steward 那一侧照注册表的同一条规则把名字拼成
    `memory__propose_fact`,回话里引用的名字才对得上。"""
    _, gate = build_memory_components(data_dir)
    mcp = FastMCP("memory")
    for fn in memory_tool_functions(gate):
        mcp.tool()(fn)
    return mcp


if __name__ == "__main__":
    import os

    create_server(Path(os.environ.get("LARARIUM_DATA_DIR", "./data"))).run()
