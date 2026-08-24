import functools
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


def _jsonable(value: Any) -> Any:
    """把工具参数收成能进 JSON 的形状。生产里它们本来就来自 JSON,这层只防意外。"""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


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
        # P0-1 纵深:本轮信封是否不可信(认领时定格);不可信轮任何 propose 强制降档。
        self._active_untrusted = False
        # M4-5d 断点续跑:上一次尝试已确立的工具结果序列、逐条消费标记、位置游标。
        self._resume_queue: list[tuple[str, str]] = []
        self._resume_consumed: list[bool] = []
        self._resume_cursor = 0
        self._active_envelope_id = ""
        self.tools = BuiltinTools(
            journal,
            registry,
            settings.timezone,
            threads,
            recall_min_similarity=settings.recall_min_similarity,
        )

    def all_tools(self) -> list[Callable]:
        """内置工具在前、bundle 工具在后,顺序固定——工具 schema 是前缀第0层。

        两层包装,都不动签名与 docstring(`functools.wraps` + 转发调用),所以**工具
        schema 逐字节不变**——有报文级测试钉住。

        - P0-1 纵深:propose_fact 包一层,本轮信封不可信时把模型传的 provenance 强制降档
          untrusted(`user_stated` 自动放行,不可信轮绝不能自动放行)。
        - M4-5d:**所有**工具再包一层断点续跑。放最外层是必须的——回放时连内层的
          propose 守卫都不该走到,因为那次调用这一轮压根没发生。
        """
        tools = list(self.tools.as_tool_functions())
        for t in self.bundle_tools:
            if getattr(t, "__name__", "") == "propose_fact":
                tools.append(self._guard_propose_fact(t))
            else:
                tools.append(t)
        return [self._resumable(t) for t in tools]

    def _resumable(self, original: Callable[..., Any]) -> Callable[..., Any]:
        """重试时按顺序回放上一次已成功的调用,只从断点之后开始真执行(M4-5d)。

        **位置优先,配不上就在剩余队列里向后按名字找。** 第 k 次调用先对上一次的第 k 次;
        对不上,再从游标往后找第一个同名且未消费的条目顶上。

        为什么**不是**按 (工具名, 参数) 去重:用户真在一轮里报两笔一模一样的 45 元午饭
        是合法的,去重会把第二笔吃掉——位置优先没有这个假阳性。
        为什么要有向后查找:第二次尝试是从同一份上下文重新生成的,调用大体相同、顺序或
        参数略有漂移;裸 positional 在第一个对不上的位置就整段作废,后面每一次都真执行、
        每一样都重复。向后查找严格更优——没有任何场景比裸 positional 差。

        **残余风险,别当它解决了**:若重试把某个早先的调用换成了另一个同名、事实上是
        另一件事的调用,向后查找会拿旧结果把它顶掉,那件新事永远不会执行——**这是丢,
        不是重**。裸 positional 在同一场景下是全量重执行(重)。两边都不干净;选这条是因为
        "模型既丢掉一个早先调用、又补上一个同名新调用"比"顺序/参数漂移"少见得多。
        分叉留痕见 `_journal_resume_divergence`。

        不论回放还是真执行都落一条 `tool_executed`:下一次重试要的是**累计**序列,
        而且查重复记账时得分得清哪一次真跑过。这条 kind 不进 L0、不进检索索引。
        """

        @functools.wraps(original)
        def resumable(*args: Any, **kwargs: Any) -> Any:
            name = getattr(original, "__name__", "")
            replayed = self._take_resumed_result(name)
            result = original(*args, **kwargs) if replayed is None else replayed
            if self._active_envelope_id:
                self.journal.append(
                    self._active_envelope_id,
                    "tool_executed",
                    {
                        "tool": name,
                        # 审计要参数:"这一轮到底记了什么"光看 result 拼不出来;
                        # 而且哪天要换配对口径,数据是现成的。配对暂时不用它。
                        "args": _jsonable(kwargs),
                        "positional": [str(a) for a in args],
                        "result": str(result),
                        "replayed": replayed is not None,
                    },
                )
            return result

        return resumable

    def _take_resumed_result(self, name: str) -> str | None:
        """位置优先,配不上就在剩余队列里向后按名字找;都没有就返回 None(真执行)。

        **如实交代:位置优先那一支目前是行为冗余的。** 游标之前的条目必然已消费
        (while 只跳已消费的,位置命中会消费掉当前条),所以"从游标扫"和"从第一个未消费扫"
        是同一件事——只按名字匹配时,这两支的结果永远相同(变异检查里删掉位置分支,
        测试全绿,而且这次是真的等价,不是测试没咬住)。
        留着它不是摆设:一旦配对口径引入参数比较(`tool_executed` 现在已经记了 args),
        "第 k 次优先"就会和"同名任取"分开,那时这一支才真正开始起作用。
        在那之前它是零成本的语义声明。
        """
        while (
            self._resume_cursor < len(self._resume_queue)
            and self._resume_consumed[self._resume_cursor]
        ):
            self._resume_cursor += 1
        if (
            self._resume_cursor < len(self._resume_queue)
            and self._resume_queue[self._resume_cursor][0] == name
        ):
            self._resume_consumed[self._resume_cursor] = True
            self._resume_cursor += 1
            return self._resume_queue[self._resume_cursor - 1][1]
        # 位置对不上:往后找同名未消费的顶上。**不移动游标**——被跳过的条目留在原地,
        # 后面的调用还能配上它们(这正是"顺序漂移也能兜住"的那一半)。
        for i in range(self._resume_cursor, len(self._resume_queue)):
            if not self._resume_consumed[i] and self._resume_queue[i][0] == name:
                self._resume_consumed[i] = True
                return self._resume_queue[i][1]
        return None

    def _journal_resume_divergence(self, envelope_id: str) -> None:
        """一轮结束时队列里还有没被消费的条目 = 发生了分叉,记一条(不改行为,只留痕)。

        理由和「静默截断读起来和『就这些』一样」是同一条:既然不把坏行为钉成规格,
        至少让它留下痕迹——真丢了一笔的那天,得有地方查。
        """
        left = [
            self._resume_queue[i][0]
            for i in range(len(self._resume_queue))
            if not self._resume_consumed[i]
        ]
        if not left:
            return
        self.journal.append(
            envelope_id,
            "resume_diverged",
            {"unconsumed": left, "total": len(self._resume_queue)},
        )

    def _guard_propose_fact(self, original: Callable[..., str]) -> Callable[..., str]:
        @functools.wraps(original)
        def guarded(
            kind: str,
            content: str,
            provenance: str,
            section: str | None = None,
            old_text: str | None = None,
        ) -> str:
            if self._active_untrusted:
                provenance = "untrusted"  # 不可信轮:模型传什么都没用,一律降档待审
            return original(kind, content, provenance, section=section, old_text=old_text)

        return guarded

    def submit(self, envelope: Envelope) -> None:
        self.inbox.put(envelope)

    def settle_if_needed(self) -> int:
        """把已通过的提案批量落盘。落盘会改前缀,所以只在明确的时机调用。"""
        return self.gate.settle()

    async def process_next(self) -> TurnOutcome:
        env = self.inbox.claim_next()
        if env is None:
            return TurnOutcome(kind="empty")

        # P0-1 纵深:本轮信封的信任度在认领时定格。不可信轮里模型传什么 provenance
        # 都会被降档成 untrusted(门控不建立在"渲染永远不出错"的假设上——这次就是
        # 渲染没被走到才出的安全洞)。
        self._active_untrusted = bool(env.meta.get("untrusted", False))

        # M4-5d:**必须赶在记本次 envelope 事件之前**取——那个事件是尝试之间的分界线。
        self._resume_queue = self.journal.last_attempt_tool_results(env.id)
        self._resume_consumed = [False] * len(self._resume_queue)
        self._resume_cursor = 0
        self._active_envelope_id = env.id

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

            try:
                reply = await self.model.run(ctx, self.all_tools(), self.mcp_servers)
            finally:
                # 成功与失败都要留痕:失败那轮的分叉同样是"上一次确立过、这次没走到"。
                self._journal_resume_divergence(env.id)

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
        # M4-5c:回放的工具名必须是**注册过的**,认不出的整次往返丢掉。模型可以喊一个
        # 不存在的工具名,框架照样把这次 tool-call 记进起居注——那串名字就是模型可控
        # 文本。挡在进上下文这一步,"封闭词表"才当得起(L3)。
        known = {getattr(f, "__name__", "") for f in self.all_tools()}
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
                exchanges=tuple(e for e in r.get("exchanges", ()) if e.name in known),
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
