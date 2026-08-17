import asyncio
import logging
from dataclasses import dataclass
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


@dataclass(frozen=True)
class CommandResult:
    text: str
    should_quit: bool = False


def handle_command(line: str, *, steward: Steward, ledger: Ledger, gate: Gate) -> CommandResult:
    """执行一条 / 命令,返回要打印的文本。不打印、不退出进程——那是调用方的事。

    抽出来的理由有两个:一是 `/rollback abc` 这类坏参数不能打死 CLI,而只有可测的
    函数才守得住;二是 M2 的 IM 按钮回调要走同一套分派,两份实现必然漂移。
    """
    if line == "/quit":
        n = steward.settle_if_needed()
        return CommandResult(f"结算 {n} 条提案后退出。" if n else "退出。", should_quit=True)
    if line == "/help":
        return CommandResult(HELP)
    if line == "/settle":
        return CommandResult(f"已结算 {steward.settle_if_needed()} 条")
    if line == "/pending":
        items = gate.pending()
        return CommandResult(
            "\n".join(f"{p.id[:8]} [{p.kind}] {p.content}" for p in items) or "无待审"
        )
    if line.startswith(("/approve ", "/reject ")):
        # 审批走代码,不过模型(DESIGN §6.3)。id 前缀匹配,方便手打。
        verb, prefix = line.split(maxsplit=1)
        matched = [p for p in gate.pending() if p.id.startswith(prefix.strip())]
        if len(matched) != 1:
            return CommandResult(f"匹配到 {len(matched)} 条,请给出更精确的 id(用 /pending 查看)")
        gate.resolve(matched[0].id, approved=verb == "/approve")
        return CommandResult(f"已{'批准' if verb == '/approve' else '否决'}:{matched[0].content}")
    if line == "/ledger":
        return CommandResult(ledger.read())
    if line == "/history":
        snaps = "\n".join(f"#{snap.id} [{snap.ts[:19]}] {snap.source}" for snap in ledger.history())
        return CommandResult(snaps)
    if line.startswith("/rollback "):
        try:
            ledger.rollback(int(line.split(maxsplit=1)[1]))
        except (ValueError, KeyError):
            return CommandResult("回滚失败:快照 id 要是一个存在的编号,可用 /history 查看")
        return CommandResult("账本已回滚,可用 /ledger 查看")
    if line.startswith("/replay "):
        events = steward.journal.replay(line.split(maxsplit=1)[1])
        return CommandResult("\n".join(f"  [{e['kind']}] {e['payload']}" for e in events))
    # 打错的命令绝不当聊天发给模型:/approve 漏 id、/aprove 拼错这种,
    # 发给模型既浪费 API 调用,又让用户误以为自己批准了提案。
    return CommandResult(f"未知命令:{line}。输入 /help 看可用命令。")


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
        if line.startswith("/"):
            try:
                result = handle_command(line, steward=steward, ledger=ledger, gate=gate)
            except Exception as exc:
                # 兜底:校验挡已知的坏输入,这里挡没想到的。任何 / 命令出错都不能打死 CLI。
                print(f"命令出错(不影响后续):{type(exc).__name__}: {exc}")
                continue
            if result.should_quit:
                print(result.text)
                return
            print(result.text)
            continue

        steward.submit(Envelope.new(source="user", channel="cli", content=line))
        try:
            reply = await steward.process_next()
        except Exception as exc:
            # 一次 API 错误(限流/网络抖动/401)不能打死 CLI——它要"随时可用"。
            # loop.py 已记 error 事件并标记信封 failed,这里只需接住不让它冒泡。
            print(f"处理出错(不影响后续):{type(exc).__name__}: {exc}")
            continue
        print(f"\nLararium > {reply}")


def run_cli() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run_cli()
