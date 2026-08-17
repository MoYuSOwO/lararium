import asyncio
import logging
from pathlib import Path

from bundles.memory.gate import Gate
from bundles.memory.ledger import Ledger
from bundles.memory.server import build_memory_components, memory_tool_functions

from lararium.config import Settings
from lararium.db import connect
from lararium.envelope import Envelope
from lararium.steward.inbox import Inbox
from lararium.steward.journal import Journal
from lararium.steward.loop import Steward
from lararium.steward.model import PydanticAIClient
from lararium.steward.registry import Registry

HELP = """可用命令(这些都不经过模型,是你直接对系统说话):
  /pending             列出待审提案
  /approve <id>        批准一条待审提案      ← 审批只能由你来
  /reject <id>         否决一条待审提案
  /settle              把已通过的提案落盘进账本
  /ledger              打印当前账本
  /history             列出账本快照
  /rollback <id>       把账本回滚到某个快照
  /replay <envelope_id>  重放某一轮
  /quit                退出(退出前自动结算)
"""


def build_steward(settings: Settings, ledger: Ledger, gate: Gate) -> Steward:
    """组装根。这是全系统唯一允许 import bundles 的地方(`.importlinter` 契约)。"""
    conn = connect(settings.data_dir / "steward.sqlite")
    return Steward(
        settings=settings,
        inbox=Inbox(conn),
        journal=Journal(conn),
        registry=Registry.load(Path("bundles")),
        ledger=ledger,
        gate=gate,
        model=PydanticAIClient(settings),
        persona=Path("prompts/persona.md").read_text(encoding="utf-8"),
        # M1 进程内挂载;M2 容器化时换成 MCP 传输,工具定义不变
        bundle_tools=memory_tool_functions(gate),
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings.load()
    # CLI 持有具体的 ledger / gate:审批、回滚这些命令走代码路径,
    # 不受 Steward 那两个最小 Port 接口的限制(Port 故意只暴露模型侧需要的能力)。
    ledger, gate = build_memory_components(settings.data_dir)
    steward = build_steward(settings, ledger, gate)

    # 上次若崩在处理途中,遗留的 processing 记录会把串行队列永久堵死(见 Task 2 补做)
    requeued, abandoned = steward.inbox.recover_stale()
    if requeued or abandoned:
        print(f"上次有未处理完的消息:{requeued} 条已重新排队,{abandoned} 条已放弃。")

    print("Lararium 已启动。输入 /help 看命令,/quit 退出。")

    while True:
        try:
            # 阻塞式 input 放线程池,别占着事件循环(ASYNC250)
            line = (await asyncio.to_thread(input, "\n你 > ")).strip()
        except (EOFError, KeyboardInterrupt):
            line = "/quit"

        if not line:
            continue
        if line == "/quit":
            n = steward.settle_if_needed()
            print(f"结算 {n} 条提案后退出。" if n else "退出。")
            return
        if line == "/help":
            print(HELP)
            continue
        if line == "/settle":
            print(f"已结算 {steward.settle_if_needed()} 条")
            continue
        if line == "/pending":
            items = gate.pending()
            print("\n".join(f"{p.id[:8]} [{p.kind}] {p.content}" for p in items) or "无待审")
            continue
        if line.startswith(("/approve ", "/reject ")):
            # 审批走代码,不过模型(DESIGN §6.3)。id 前缀匹配,方便手打。
            verb, prefix = line.split(maxsplit=1)
            matched = [p for p in gate.pending() if p.id.startswith(prefix.strip())]
            if len(matched) != 1:
                print(f"匹配到 {len(matched)} 条,请给出更精确的 id(用 /pending 查看)")
                continue
            gate.resolve(matched[0].id, approved=verb == "/approve")
            print(f"已{'批准' if verb == '/approve' else '否决'}:{matched[0].content}")
            continue
        if line == "/ledger":
            print(ledger.read())
            continue
        if line == "/history":
            for snap in ledger.history():
                print(f"#{snap.id} [{snap.ts[:19]}] {snap.source}")
            continue
        if line.startswith("/rollback "):
            ledger.rollback(int(line.split(maxsplit=1)[1]))
            print("账本已回滚,可用 /ledger 查看")
            continue
        if line.startswith("/replay "):
            for event in steward.journal.replay(line.split(maxsplit=1)[1]):
                print(f"  [{event['kind']}] {event['payload']}")
            continue

        steward.submit(Envelope.new(source="user", channel="cli", content=line))
        reply = await steward.process_next()
        print(f"\nLararium > {reply}")


def run_cli() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run_cli()
