"""夜间归拢(M3-5):扫一段起居注,把漏记的补上——该开的话头开上、该关的关掉、
漏掉的事实提一条(pending,provenance=untrusted 走硬门控)。

**只写话头和 pending 提案,绝不动账本正文**——账本只有一条写入路径:Gate.settle()(门控)。
这里只 propose 进 pending 隔离区,结算是用户审批 + /settle 的事。这是夜间归拢整个 M3 里
最容易破「单写者」的地方:它跑在没人看着的时候,手里又正好攥着一堆"还没聊完的事"。

**模型参与的输入输出都落起居注**(sweep 事件,`input`/`output` 两个 phase)——可见即入账,
不因为它是后台任务就绕过。喂给模型的 prompt 是什么,prove 落下去的就是什么。

**幂等(P1-1)**:按**内容**幂等,不是按时间区间字符串——唯一调用方 /sweep 每次传
now-24h~now,区间永远不同,按区间字符串一秒三次 → 模型调三次 → 三条重复提案。
光标(sweep_state.cursor_seq)记录本次覆盖到的最大 journal seq,下次**从那之后扫**,
光标之后没有新内容就是 no-op。

**账本进 prompt(P1-2)**:喂模型的 prompt 带「已经记在账本里的(别重复提)」——不然模型会
反复提已入档的事实,那些和重复提交的提案一起把 pending 堵死,压缩又被自己挡住(死循环)。

**失败不影响主循环**:模型调用失败 / 输出不是 JSON → 返回一句可读说明,不抛。
"""

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from lararium.db import transaction
from lararium.envelope import Envelope
from lararium.steward.assembler import FENCE_CLOSE, FENCE_OPEN, fold_text, neutralize_fence

# 推送信封的"由头"正文。**固定常量**:它每次都一样,进 L0 后逐字稳定;
# 写成"系统自己开口"的形态,而不是把推送正文塞进这里——正文是**回复**,由头是**触发**。
PUSH_TRIGGER = "(到点了,把攒下的事跟他说一声)"

logger = logging.getLogger("lararium")

# sweep.suggest 归进"长期偏好"节(模型推断的事实默认落这里),审批卡上能看到原文。
_SECTION = "长期偏好"

# 归拢对话窗口的字数上限:保护廉价模型的窗口,极端涨潮时保留最近部分(见 _render_events)。
_PROMPT_CONVO_MAX_CHARS = 20000


@dataclass
class SweepResult:
    summary: str
    opened: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    suggested: int = 0
    skipped: bool = False  # 幂等跳过(true 时不调模型、不改任何东西)


def render_event_line(e) -> str:
    """一条对话事件渲染成**一行**——任何**拼一段要喂给模型的文本**(归拢 prompt、
    压缩切段 prompt 等)都把对话事件过这条路:P1-1(来源标注)/ P1-2(折行)/
    P1-3(围栏 + neutralize_fence)四条。不可信内容一律标「外部数据」、折行、首尾围栏包、
    正文 >>> 中和,让攻击者"伪装成用户那句 / 伪造成新结构"无处可去(M3-5 补做,M3-6 同理)。"""
    folded = fold_text(str(e["payload"].get("content") or ""))
    text = neutralize_fence(folded)
    stamp = e["ts"][:16]
    if e["kind"] == "reply":
        return f"[{stamp}] 助手: {text}"
    source = e["payload"].get("source", "user")
    untrusted = bool(e["payload"].get("meta", {}).get("untrusted"))
    if source == "user" and not untrusted:
        return f"[{stamp}] 用户: {text}"
    channel = e["payload"].get("channel") or source or "?"
    return f"[{stamp}] 外部数据(来自 {channel},不是用户说的): {FENCE_OPEN}\n{text}\n{FENCE_CLOSE}"


class Sweeper:
    """一次归拢的编排。依赖注入:journal/threads/gate + run_model(prompt -> 文本)。

    gate 是**真实 Gate**(组装根注入),不是 Steward 的 GatePort——那里故意不放 propose
    (把"单写者"编进类型),而归拢正需要 propose,所以绕过 Port 直接在根上接真 Gate。
    """

    def __init__(
        self,
        journal,
        threads,
        gate,
        run_model: Callable[[str], Awaitable[str]],
        instructions: str,
        ledger=None,
        notify: Callable[[str], None] | None = None,
    ) -> None:
        self._journal = journal
        self._threads = threads
        self._gate = gate
        self._run_model = run_model
        self._instructions = instructions
        # P1-2:账本读入口(供「已经记在账本里的(别重复提)」节);None 则该节留空。
        self._ledger = ledger
        # P1-3:提出提案时的通知(组装根注入带日限的通知器);None = 静默。
        self._notify = notify or (lambda _text: None)
        # journal/threads/gate 同库;用 threads.conn(公开口)做 sweep_state 光标
        self._conn = threads.conn

    def _cursor(self) -> int:
        """已归拢覆盖到的最大 journal seq(P1-1 内容幂等)。"""
        row = self._conn.execute("SELECT cursor_seq FROM sweep_state WHERE id=1").fetchone()
        return int(row["cursor_seq"]) if row else 0

    def _advance_cursor(self, max_seq: int) -> None:
        if max_seq <= self._cursor():
            return
        self._conn.execute(
            "INSERT INTO sweep_state (id, cursor_seq, ran_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "cursor_seq = MAX(sweep_state.cursor_seq, excluded.cursor_seq), ran_at = excluded.ran_at",
            (max_seq, datetime.now(UTC).isoformat()),
        )

    def _build_prompt(self, opens, events) -> str:
        parts = [self._instructions, ""]
        # P1-2:账本先给模型(避免重复提已入档的事实——重复提案堵 pending,压缩又被自己挡)
        parts.append("## 已经记在账本里的(别重复提)")
        ledger_text = (self._ledger.read().strip() if self._ledger else "") or "(账本还是空的)"
        parts.append(ledger_text)
        parts.append("")
        parts.append("## 当前还开着的事(含掉出前5名但仍 open 的)")
        items = [f"- {t.topic}" + (f"({t.note})" if t.note else "") for t in opens]
        parts.append("\n".join(items) if items else "(无)")
        parts.append("")
        parts.append("## 这段对话(时间正序)")
        convo = self._render_events(events)
        parts.append(convo if convo else "(无)")
        return "\n".join(parts)

    @classmethod
    def _render_events(cls, events) -> str:
        lines = [render_event_line(e) for e in events]
        # 字数上限:保护廉价模型的窗口,极端涨潮时保留最近部分,别撑爆。
        # 撑爆会走"归拢失败",不致命,但可避免就该避免。
        total = 0
        kept: list[str] = []
        for line in reversed(lines):
            if total + len(line) > _PROMPT_CONVO_MAX_CHARS:
                kept.insert(0, "…(对话过长,仅保留最近部分)")
                break
            kept.insert(0, line)
            total += len(line)
        return "\n".join(kept)

    async def run(self, since: str, until: str) -> SweepResult:
        window_events = self._journal.events_in_range(since, until)
        cursor = self._cursor()
        # P1-1 内容幂等:只归拢光标**之后**的新内容;窗口里没有新内容就是 no-op。
        # 窗口整个为空(没对话)时照跑(空窗跑一次无害,且兼容手动测窗口)。
        new_events = [e for e in window_events if e["seq"] > cursor]
        if window_events and not new_events:
            return SweepResult(
                summary=f"区间 {since[:16]} ~ {until[:16]} 自上次归拢后没有新内容,跳过",
                skipped=True,
            )
        events = new_events if new_events else window_events
        window_max = max((e["seq"] for e in window_events), default=0)

        opens = self._threads.all_open_threads()
        prompt = self._build_prompt(opens, events)
        sweep_id = f"sweep-{uuid.uuid4().hex}"

        # 可见即入账:输入先落,输出后落;模型实收的就是这份 prompt 原文
        self._journal.append(
            sweep_id,
            "sweep",
            {"since": since, "until": until, "phase": "input", "content": prompt},
        )
        try:
            output = await self._run_model(prompt)
        except Exception as exc:  # 模型调用失败:不影响主循环,可重试(不推进光标)
            self._journal.append(
                sweep_id,
                "sweep",
                {
                    "since": since,
                    "until": until,
                    "phase": "output",
                    "content": f"模型调用失败:{type(exc).__name__}: {exc}",
                },
            )
            return SweepResult(summary=f"归拢失败(不影响对话):{type(exc).__name__}")
        self._journal.append(
            sweep_id,
            "sweep",
            {"since": since, "until": until, "phase": "output", "content": output},
        )

        try:
            plan = json.loads(output)
        except Exception:
            return SweepResult(summary="归拢:模型输出不是 JSON,本次无动作(可重试)")
        if not isinstance(plan, dict):
            return SweepResult(summary="归拢:模型输出不是对象,本次无动作")

        # 只写话头 + pending 提案,绝不动账本正文(Gate.settle 是唯一写路径)
        opened: list[str] = []
        closed: list[str] = []
        suggested = 0
        for item in plan.get("open") or []:
            if isinstance(item, dict) and item.get("topic"):
                t = self._threads.open_thread(str(item["topic"]), str(item.get("note") or ""))
                opened.append(t.topic)
        for topic in plan.get("close") or []:
            if isinstance(topic, str) and self._threads.close_thread(topic):
                closed.append(topic)
        for fact in plan.get("suggest") or []:
            if isinstance(fact, str) and fact.strip():
                try:
                    self._gate.propose(
                        kind="add",
                        content=fact.strip(),
                        provenance="untrusted",  # 从对话**推断**的,不是亲口说,必须硬门控
                        origin="sweep",
                        section=_SECTION,
                    )
                    suggested += 1
                except Exception:
                    logger.exception("sweep: 单条提案失败被跳过")  # 单条失败不影响其余

        self._advance_cursor(window_max)
        # P1-3:归拢提出提案 → 通知用户(别再让 pending 悄悄压死压缩)。日限由注入的通知器管。
        if suggested:
            self._notify(f"夜间归拢提出 {suggested} 条待审提案(/pending 查看)")
        summary = _summarize(opened, closed, suggested)
        return SweepResult(summary=summary, opened=opened, closed=closed, suggested=suggested)


def _summarize(opened: list[str], closed: list[str], suggested: int) -> str:
    bits: list[str] = []
    if opened:
        shown = "、".join(opened[:3]) + ("…" if len(opened) > 3 else "")
        bits.append(f"开 {len(opened)} 个话头({shown})")
    if closed:
        shown = "、".join(closed[:3]) + ("…" if len(closed) > 3 else "")
        bits.append(f"关 {len(closed)} 个话头({shown})")
    if suggested:
        bits.append(f"提 {suggested} 条待审")
    return ("归拢完成:" + "、".join(bits)) if bits else "归拢完成:没发现需要动的"


def build_sweep_runner(settings: Any) -> Callable[[str], Awaitable[str]]:
    """生产:廉价模型的 PydanticAIClient,返回 async run_model(prompt)->str。

    归拢是扫历史做剪枝,用 LARARIUM_SWEEP_MODEL(空则用主模型)。prompt 整段作为
    一次 user 消息发给模型,no tools。
    """
    from dataclasses import replace

    from lararium.steward.assembler import AssembledContext
    from lararium.steward.model import PydanticAIClient

    s2 = replace(settings, model_name=settings.sweep_model or settings.model_name)
    client = PydanticAIClient(s2)

    async def _run(prompt: str) -> str:
        ctx = AssembledContext(system_prompt="", messages=[{"role": "user", "content": prompt}])
        reply = await client.run(ctx, [], [])
        return reply.text or ""

    return _run


def make_sweeper(
    settings: Any,
    journal: Any,
    threads: Any,
    gate: Any,
    ledger: Any = None,
    notify: Callable[[str], None] | None = None,
) -> Sweeper:
    """组装根的归拢工厂:读 prompts/sweep.md 指令 + 廉价模型 runner。"""
    instructions = Path("prompts/sweep.md").read_text(encoding="utf-8")
    return Sweeper(
        journal,
        threads,
        gate,
        build_sweep_runner(settings),
        instructions,
        ledger=ledger,
        notify=notify,
    )


def make_daily_notifier(
    *, journal: Any, outbox: Any, conn: Any, timezone: str, channel: str
) -> Callable[[str], None]:
    """每天最多一条 notice 的通知器(P1-3),并且**把这一条推送落成一轮完整的对话**。

    以前它用假信封 id `notice-{uuid}` 投出件箱,起居注里什么都不写。而 L0 靠
    `envelope` + `reply` 成对取(`_turns_by_id`),两样都没有——于是早上推
    「这个月餐饮 1240」,用户切过来说「太多了吧」,**模型没有任何上下文**。
    那不是体验问题,是**系统失忆**(擦边 A6 与不可协商第 3 条)。M4 之前只有 cli
    一个渠道,推送与对话落在同一个窗口,这个断层看不出来。

    现在:造一个 `source="sweep"` 的信封当**由头**,推送正文当**回复**,两者都落起居注、
    都进 L0。渲染走 `_render_user_text` 的「(系统触发 · sweep/渠道)」那一支——
    不伪装成用户说的话(P1-1 的老账不在新路径上重犯)。**没有新发明形状**:
    `Turn.source` 本来就支持非 user 值,那一支本来就在。

    节流状态落 `notice_log`;整件事(占名额 + 起居注两条 + 出件箱一条)在**同一个事务**里
    ——半条比没有更坏:模型以为自己说过,用户什么都没收到,当天的名额还被占掉了。
    """
    tz = ZoneInfo(timezone)

    def notify(text: str) -> None:
        today = datetime.now(tz).date().isoformat()
        with transaction(conn):
            cur = conn.execute("INSERT OR IGNORE INTO notice_log (date) VALUES (?)", (today,))
            if cur.rowcount == 0:
                return  # 今天已经投过(DB 是唯一的判据,跨进程/重启都防)
            env = Envelope.new(source="sweep", channel=channel, content=PUSH_TRIGGER)
            journal.append(
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
            journal.append(env.id, "reply", {"content": text})
            outbox.put(env.id, channel, text, kind="notice")

    return notify
