"""命令分派——CLI 与 HTTP 命令端点共用的同一套 `/` 命令处理。

**这个模块从此就是门控的开关(D12)**:`/approve` `/settle` `/rollback` 都从这里
直通 Gate/Ledger,不经模型。M5 上 `python_sandbox` 时,"沙箱无网络"就是防它被
**模型自己 POST** 到命令端点的那道墙——这两条约束是绑定的,谁也不许单独放松:
沙箱一旦联网,被注入的模型就能自己 /approve 把恶意事实永久写进账本。
"""

from dataclasses import dataclass

from bundles.memory.gate import Gate
from bundles.memory.ledger import Ledger

from lararium.steward.loop import Steward

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
    函数才守得住;二是 M2/M4 的 IM 按钮回调要走同一套分派,两份实现必然漂移。

    R2-1 教训:**先 strip**——CLI 会 `input().strip()`,但 M2 之后的真实调用方是 HTTP
    命令端点,它既不 strip 也不兜底。' /approve '(带尾空格)这类输入在 CLI 里抹掉了,
    在端点上就是一次书:line.split(maxsplit=1) 只有一个元素,解包/取 [1] 直接 500。
    防护必须写在真实调用方经过的那条路上。
    """
    line = line.strip()  # R2-1:入口统一 strip(HTTP 端点不 strip,这条防护不能只靠 CLI)
    parts = line.split(maxsplit=1)
    verb = parts[0]

    if verb == "/quit":
        # 退出是逃生口,不许被别的故障堵住:结算失败要说出来,但一定还是退出。
        # 少了这层,EOF 会把一次结算失败变成每秒数万行的死循环。
        try:
            n = steward.settle_if_needed()
        except Exception as exc:
            return CommandResult(
                f"退出前结算失败({type(exc).__name__}: {exc})。提案仍在库里,"
                f"修好账本后重启会自动结算。",
                should_quit=True,
            )
        return CommandResult(f"结算 {n} 条提案后退出。" if n else "退出。", should_quit=True)
    if verb == "/help":
        return CommandResult(HELP)
    if verb == "/settle":
        return CommandResult(f"已结算 {steward.settle_if_needed()} 条")
    if verb == "/pending":
        items = gate.pending()
        return CommandResult(
            "\n".join(f"{p.id[:8]} [{p.kind}] {p.content}" for p in items) or "无待审"
        )
    if verb in ("/approve", "/reject"):
        # 审批走代码,不过模型(DESIGN §6.3)。id 前缀匹配,方便手打。
        if len(parts) < 2 or not parts[1].strip():
            return CommandResult("这个命令需要一个参数(提案 id 前缀,用 /pending 查看)")
        matched = [p for p in gate.pending() if p.id.startswith(parts[1].strip())]
        if len(matched) != 1:
            return CommandResult(f"匹配到 {len(matched)} 条,请给出更精确的 id(用 /pending 查看)")
        gate.resolve(matched[0].id, approved=verb == "/approve")
        return CommandResult(f"已{'批准' if verb == '/approve' else '否决'}:{matched[0].content}")
    if verb == "/ledger":
        return CommandResult(ledger.read())
    if verb == "/history":
        snaps = "\n".join(f"#{snap.id} [{snap.ts[:19]}] {snap.source}" for snap in ledger.history())
        return CommandResult(snaps)
    if verb == "/rollback":
        if len(parts) < 2 or not parts[1].strip():
            return CommandResult("这个命令需要一个参数(快照 id,用 /history 查看)")
        try:
            ledger.rollback(int(parts[1].strip()))
        except (ValueError, KeyError):
            return CommandResult("回滚失败:快照 id 要是一个存在的编号,可用 /history 查看")
        return CommandResult("账本已回滚,可用 /ledger 查看")
    if verb == "/replay":
        if len(parts) < 2 or not parts[1].strip():
            return CommandResult("这个命令需要一个参数(信封 id,用 /history 重放任意一轮)")
        events = steward.journal.replay(parts[1].strip())
        return CommandResult("\n".join(f"  [{e['kind']}] {e['payload']}" for e in events))
    # 打错的命令绝不当聊天发给模型:/approve 漏 id、/aprove 拼错这种,
    # 发给模型既浪费 API 调用,又让用户误以为自己批准了提案。
    return CommandResult(f"未知命令:{line}。输入 /help 看可用命令。")
