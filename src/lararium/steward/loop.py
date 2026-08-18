import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from lararium.config import Settings
from lararium.envelope import Envelope
from lararium.steward.assembler import Turn, assemble
from lararium.steward.inbox import Inbox
from lararium.steward.journal import Journal
from lararium.steward.model import ModelCallError, ModelClient, format_cache_log
from lararium.steward.outbox import Outbox
from lararium.steward.ports import GatePort, LedgerPort
from lararium.steward.registry import Registry
from lararium.steward.tools import BuiltinTools

logger = logging.getLogger("lararium")


@dataclass(frozen=True)
class TurnOutcome:
    """process_next 的结果,让 worker 按 kind 分流——避免 None 一词多义(队列空 vs 可重试)。

    - replied:本轮消费了一个信封走到终态(成功回复,或终态失败发了 notice)。
      worker 据此知道自己"忙过",队列排空时该触发空闲结算。
    - empty:收件箱空,null 之外的明确信号。
    - retry_later:可重试失败,信封已放回 pending。attempts 是本次失败时的已尝试次数,
      worker 用它做指数退避(2**attempts,封顶 60s)——绝不等 wake,否则任何新消息
      都会立刻重锤那条被限流的消息。
    """

    kind: Literal["replied", "empty", "retry_later"]
    text: str | None = None  # replied 且是成功回复时:回复正文
    attempts: int = 0  # retry_later 时:本次失败已尝试次数(退避用)


class Steward:
    def __init__(
        self,
        *,
        settings: Settings,
        inbox: Inbox,
        journal: Journal,
        registry: Registry,
        ledger: LedgerPort,
        gate: GatePort,
        model: ModelClient,
        persona: str,
        outbox: Outbox,
        bundle_tools: list[Callable] | None = None,
        mcp_servers: list[Any] | None = None,
    ) -> None:
        self.settings = settings
        self.inbox = inbox
        self.journal = journal
        self.registry = registry
        self.ledger = ledger
        self.gate = gate
        self.model = model
        self.persona = persona
        self.outbox = outbox
        self.bundle_tools = bundle_tools or []
        self.mcp_servers = mcp_servers or []
        self.tools = BuiltinTools(journal, registry, settings.timezone)

    def all_tools(self) -> list[Callable]:
        """内置工具在前、bundle 工具在后,顺序固定——工具 schema 是前缀第0层。"""
        return self.tools.as_tool_functions() + self.bundle_tools

    def submit(self, envelope: Envelope) -> None:
        self.inbox.put(envelope)

    def settle_if_needed(self) -> int:
        """把已通过的提案批量落盘。落盘会改前缀,所以只在明确的时机调用。"""
        return self.gate.settle()

    async def process_next(self) -> TurnOutcome:
        env = self.inbox.claim_next()
        if env is None:
            return TurnOutcome(kind="empty")

        self.journal.append(
            env.id,
            "envelope",
            {
                "content": env.content,
                "source": env.source,
                "channel": env.channel,
                "meta": env.meta,
                "ts": env.ts.isoformat(),
            },
        )

        try:
            ctx = assemble(
                persona=self.persona,
                directory=self.registry.directory_lines(),
                ledger=self.ledger.read(),
                l1="",  # M3 压缩接管后填充
                l0=self._recent_turns(),
                envelope=env,
                timezone=self.settings.timezone,
            )
            self.journal.append(
                env.id,
                "prompt",
                {
                    "system_prompt": ctx.system_prompt,
                    "messages": ctx.messages,
                },
            )

            reply = await self.model.run(ctx, self.all_tools(), self.mcp_servers)

            for event in reply.tool_events:
                payload = {k: v for k, v in event.items() if k != "type"}
                self.journal.append(env.id, event["type"], payload)

            self.journal.append(
                env.id,
                "reply",
                {
                    "content": reply.text,
                    "cache_hit_tokens": reply.cache_hit_tokens,
                    "prompt_tokens": reply.prompt_tokens,
                    "completion_tokens": reply.completion_tokens,
                },
            )
            logger.info(format_cache_log(reply))
            # 崩溃语义:回复先落出件箱,信封才算完成。中间崩了,重启后 recover_stale
            # 重排队、重算一轮(多花一次 API 钱),但绝不静默吞回复——用户至少收到一次。
            self.outbox.put(env.id, env.channel, reply.text, kind="reply")
            self.inbox.complete(env.id)
            return TurnOutcome(kind="replied", text=reply.text)

        except ModelCallError as exc:
            # 隔离盒已经把 pydantic-ai 的异常分类成自家类型,这里只认 retryable。
            self.journal.append(env.id, "error", {"content": str(exc)})
            attempts = self.inbox.attempts(env.id)
            if exc.retryable and attempts < self.settings.max_attempts:
                self.inbox.release(env.id)  # 回 pending,attempts 已在 claim 时 +1
                return TurnOutcome(kind="retry_later", attempts=attempts)
            self.inbox.fail(env.id, str(exc))
            self.outbox.put(
                env.id,
                env.channel,
                f"这条消息处理失败({exc}),已放弃:{env.content[:50]}",
                kind="notice",
            )
            return TurnOutcome(kind="replied")  # 终态:发 notice,消费了槽位

        except Exception as exc:
            # 非模型错误 = 代码 bug:留痕、标记 failed、向上冒泡(毒消息范式,worker 会接)。
            self.journal.append(env.id, "error", {"content": f"{type(exc).__name__}: {exc}"})
            self.inbox.fail(env.id, f"{type(exc).__name__}: {exc}")
            raise

    def _recent_turns(self) -> list[Turn]:
        rows = self.journal.recent_turns(limit=self.settings.l0_max_turns)
        return [
            Turn(
                user=r["user"],
                assistant=r["assistant"],
                source=r.get("source", "user"),
                channel=r.get("channel", "cli"),
                untrusted=r.get("untrusted", False),
                ts=r.get("ts"),
            )
            for r in rows
        ]
