import asyncio
import logging

from bundles.memory.server import build_memory_components

from lararium.config import Settings
from lararium.envelope import Envelope
from lararium.gateway.commands import handle_command
from lararium.gateway.server import build_steward

# HELP / CommandResult / handle_command 已搬到 gateway/commands.py(CLI 客户端化后
# 不再 import bundles,而 handle_command 需要 Gate/Ledger 类型),cli 只从这里用 handle_command。


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
            # EOF/中断之后不要绕道 handle_command:万一那条路抛异常,
            # 兜底会 continue,而 EOF 永久为真 → 死循环。就地退出。
            print(handle_command("/quit", steward=steward, ledger=ledger, gate=gate).text)
            return

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
            outcome = await steward.process_next()
        except Exception as exc:
            # 一次 API 错误(限流/网络抖动/401)不能打死 CLI——它要"随时可用"。
            # loop.py 已记 error 事件并标记信封 failed,这里只需接住不让它冒泡。
            print(f"处理出错(不影响后续):{type(exc).__name__}: {exc}")
            continue
        if outcome.kind == "replied" and outcome.text:
            print(f"\nLararium > {outcome.text}")
        elif outcome.kind == "retry_later":
            # M2-3 起重试归 worker 管(指数退避);CLI 这只是告知,下轮会先重跑这条。
            print("\n(模型暂时不可用,将自动重试……)")


def run_cli() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run_cli()
