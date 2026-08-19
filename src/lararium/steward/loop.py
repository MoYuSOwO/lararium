import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from lararium.config import Settings
from lararium.db import transaction
from lararium.envelope import Envelope
from lararium.steward.assembler import Turn, assemble
from lararium.steward.inbox import Inbox
from lararium.steward.journal import Journal, estimate_tokens
from lararium.steward.model import ModelCallError, ModelClient, format_cache_log
from lararium.steward.outbox import Outbox
from lararium.steward.ports import GatePort, LedgerPort
from lararium.steward.registry import Registry
from lararium.steward.threads import Threads
from lararium.steward.tools import BuiltinTools

logger = logging.getLogger("lararium")

# L0 预算里给"工具 schema + 输出窗口"的固定留白(token)。工具 schema 实测约
# 500/请求;输出要占窗口,单用户交互给足 8000。M3-6 的低水位也要继承这个估算口径。
L0_RESERVE = 8000


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
        threads: Threads,
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
        self.threads = threads
        self.bundle_tools = bundle_tools or []
        self.mcp_servers = mcp_servers or []
        self.tools = BuiltinTools(
            journal,
            registry,
            settings.timezone,
            threads,
            recall_min_similarity=settings.recall_min_similarity,
        )

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

        # M3-3:认领后把当前开着的話头**冻结**进 meta——定时/事件信封也能带上。
        # 冻结的是此刻的快照,历史轮渲染的是这份,不是未来的最新(M3 全局约束第 2 条)。
        snapshot = [{"topic": t.topic, "note": t.note} for t in self.threads.open_threads()]
        if snapshot:
            env.meta["open_threads"] = snapshot

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
            # M3-3 顺带:ledger/directory 一轮读一次,预算估算和 assemble 共用同一份
            # (之前各读一遍,同轮内不会变但纯属冗余)。
            directory = self.registry.directory_lines()
            ledger_text = self.ledger.read()
            prefix_text = self.persona + directory + ledger_text
            # M3-6:L1(压缩索引块)供数给 assemble;一轮算一次,预算和渲染共用。
            l1_text = self.journal.l1_block(self.settings.compact_index_days)
            ctx = assemble(
                persona=self.persona,
                directory=directory,
                ledger=ledger_text,
                l1=l1_text,
                l0=self._recent_turns(prefix_text, l1_text),
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
            # 崩溃语义:回复先落出件箱,信封才算完成。M3-1 Step0 收掉 M2-6 遗留——
            # 两个语句各自动提交,崩在中间会留下「出件箱有回复、信封未完成」的半态,
            # 重启 recover_stale 重排队重算 → **重复回复**。放进同一事务:
            # 要么都落、要么都不落;都不落 → 重启重排队重算(多花一次 API 但只回一次),
            # 回复绝不静默吞(D10 at-least-once)。
            if self.inbox.conn is not self.outbox.conn:
                # 用异常不用 assert:python -O 会吞 assert,异连接下事务会静默退回旧 bug。
                raise RuntimeError("组装根必须给 inbox/outbox 注入同一连接——异连接下事务不成立")
            with transaction(self.inbox.conn):
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

    def _l0_token_budget(self, prefix_text: str, l1_text: str) -> int:
        # M3-1b/3-6:LARARIUM_L0_MAX_TOKENS 是**整个上下文预算**(200000=200k 窗口用满)。
        # 先扣前缀区(persona+目录+账本,由调用方 read-once 算好)、L1 压缩索引块、固定留白
        # (工具 schema + 输出窗口),余额才归 L0——「200k 是整窗,不是 L0 独占」的忠实实现。
        return max(
            0,
            self.settings.l0_max_tokens
            - estimate_tokens(prefix_text)
            - estimate_tokens(l1_text)
            - L0_RESERVE,
        )

    def _recent_turns(self, prefix_text: str, l1_text: str) -> list[Turn]:
        # M3-1:L0 按整个上下文预算的余额截断,l0_max_turns 只当轮数兜底。
        rows = self.journal.recent_turns_within_budget(
            max_tokens=self._l0_token_budget(prefix_text, l1_text),
            max_turns=self.settings.l0_max_turns,
        )
        return [
            Turn(
                user=r["user"],
                assistant=r["assistant"],
                source=r.get("source", "user"),
                channel=r.get("channel", "cli"),
                untrusted=r.get("untrusted", False),
                ts=r.get("ts"),
                # M3-3:历史轮带**当时冻结**的话头快照,渲染的是那份不是最新的
                open_threads=r.get("open_threads"),
            )
            for r in rows
        ]

    async def maybe_compact(self, compactor: Any) -> str | None:
        """M3-6 触发:上下文装不下时,把顶出低水位的未压缩轮压成 L1 索引。

        compactor 由组装根用**真 Gate** 造好传入(Steward 的 GatePort 不放 propose,
        单写者编进类型)。平常未顶满是 no-op;COMPACT=off 退回纯截断。
        """
        s = self.settings
        if s.compact != "on":
            return None
        l1_text = self.journal.l1_block(s.compact_index_days)
        prefix_text = self.persona + self.registry.directory_lines() + self.ledger.read()
        low_budget = max(
            0,
            s.compact_low_water
            - estimate_tokens(prefix_text)
            - estimate_tokens(l1_text)
            - L0_RESERVE,
        )
        keep_ids = {
            t["envelope_id"]
            for t in self.journal.recent_turns_within_budget(
                max_tokens=low_budget, max_turns=s.l0_max_turns
            )
        }
        to_compress = [e for e in self.journal.uncompressed_envelope_ids() if e not in keep_ids]
        if not to_compress:
            return None  # 上下文还没顶到压缩线,no-op
        rng = self.journal.min_max_ts(to_compress)
        if rng is None:
            return None
        result: Any = await compactor.run(rng[0], rng[1])
        return str(result.summary)
