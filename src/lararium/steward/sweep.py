"""夜间归拢(M3-5):扫一段起居注,把漏记的补上——该开的话头开上、该关的关掉、
漏掉的事实提一条(pending,provenance=untrusted 走硬门控)。

**只写话头和 pending 提案,绝不动账本正文**——账本只有一条写入路径:Gate.settle()(门控)。
这里只 propose 进 pending 隔离区,结算是用户审批 + /settle 的事。这是夜间归拢整个 M3 里
最容易破「单写者」的地方:它跑在没人看着的时候,手里又正好攥着一堆"还没聊完的事"。

**模型参与的输入输出都落起居注**(sweep 事件,`input`/`output` 两个 phase)——可见即入账,
不因为它是后台任务就绕过。喂给模型的 prompt 是什么,prove 落下去的就是什么。

**幂等**:同一 (since, until) 区间只归拢一次(sweep_runs 表)。重复跑同区间是 no-op——
模型非确定性,重跑会产出重复提案。

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

from lararium.steward.assembler import FENCE_CLOSE, FENCE_OPEN, fold_text, neutralize_fence

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
    ) -> None:
        self._journal = journal
        self._threads = threads
        self._gate = gate
        self._run_model = run_model
        self._instructions = instructions
        # journal/threads/gate 同库;用 threads.conn(公开口)做 sweep_runs 幂等
        self._conn = threads.conn

    def _range_id(self, since: str, until: str) -> str:
        return f"{since}|{until}"

    def _was_swept(self, range_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sweep_runs WHERE range_id=?", (range_id,)
        ).fetchone()
        return row is not None

    def _mark_swept(self, range_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO sweep_runs (range_id, ran_at) VALUES (?,?)",
            (range_id, datetime.now(UTC).isoformat()),
        )

    def _build_prompt(self, opens, events) -> str:
        parts = [self._instructions, ""]
        parts.append("## 当前还开着的事(含掉出前5名但仍 open 的)")
        items = [f"- {t.topic}" + (f"({t.note})" if t.note else "") for t in opens]
        parts.append("\n".join(items) if items else "(无)")
        parts.append("")
        parts.append("## 这段对话(时间正序)")
        convo = self._render_events(events)
        parts.append(convo if convo else "(无)")
        return "\n".join(parts)

    @staticmethod
    def _render_event_line(e) -> str:
        """一条对话事件渲染成**一行**——归拢的 prompt 也是喂给模型的文本,过
        P1-1(来源标注)/ P1-2(折行)/ P1-3(围栏 + neutralize_fence)四条:
        不可信内容一律标「外部数据」、折行、首尾围栏包、正文里的 >>> 中和,让攻击者
        "伪装成用户那句 / 伪造成新小节"的企图无处可去(M3-5 补做,M3-6 切段同理)。"""
        stamp = e["ts"][:16]
        folded = fold_text(str(e["payload"].get("content") or ""))
        text = neutralize_fence(folded)
        if e["kind"] == "reply":
            return f"[{stamp}] 助手: {text}"
        source = e["payload"].get("source", "user")
        untrusted = bool(e["payload"].get("meta", {}).get("untrusted"))
        if source == "user" and not untrusted:
            return f"[{stamp}] 用户: {text}"
        channel = e["payload"].get("channel") or source or "?"
        return (
            f"[{stamp}] 外部数据(来自 {channel},不是用户说的): {FENCE_OPEN}\n{text}\n{FENCE_CLOSE}"
        )

    @classmethod
    def _render_events(cls, events) -> str:
        lines = [cls._render_event_line(e) for e in events]
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
        range_id = self._range_id(since, until)
        if self._was_swept(range_id):
            return SweepResult(
                summary=f"区间 {since[:16]} ~ {until[:16]} 已归拢过,跳过", skipped=True
            )

        opens = self._threads.all_open_threads()
        events = self._journal.events_in_range(since, until)
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
        except Exception as exc:  # 模型调用失败:不影响主循环,可重试
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

        self._mark_swept(range_id)
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


def make_sweeper(settings: Any, journal: Any, threads: Any, gate: Any) -> Sweeper:
    """组装根的归拢工厂:读 prompts/sweep.md 指令 + 廉价模型 runner。"""
    instructions = Path("prompts/sweep.md").read_text(encoding="utf-8")
    return Sweeper(journal, threads, gate, build_sweep_runner(settings), instructions)
