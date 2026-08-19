"""上下文压缩(M3-6)——M3 最后一块硬骨头。

触发:上下文用满 200k。产出:每段一行索引(日期 · 话题 · 一句结论 · 信封id),正文退出一线。
**不产状态卡**——"什么还开着"是话头的活(M3-2/3-3),两套数据必漂移。

编排(一个事件干完全部,按 DESIGN §7):
1. **审批屏障**(先查):pending 非空必须停——压缩要销毁提案的原始证据,证据没了没法审;
2. **切段**:待压缩窗口按话题切开(廉价模型,`LARARIUM_SWEEP_MODEL`,切错无大碍);
3. **沉淀筛**:**直接复用 M3-5 的 Sweeper,不许写第二份**(两份实现必漂移,P1-1 教训);
4. **审批屏障**(再查):沉淀筛刚提的新 pending 也不能毁证据,先审完;
5. **索引**:每段一行写进 l1_index,正文**不删**(append-only)只是标记压缩退出 L0;
6. **不反复**:已压缩的信封从 L0 排除,不会再压一次。

数字口径全部走 estimate_tokens + _render_overhead(渲染后形态,M3-1b/M3-3 定死):
整窗 200000、低水位 150000、索引保留 90 天,不自己发明。
"""

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lararium.steward.sweep import build_sweep_runner, make_sweeper, render_event_line

logger = logging.getLogger("lararium")


@dataclass
class Segment:
    date: str
    topic: str
    conclusion: str
    envelope_id: str


@dataclass
class CompactResult:
    summary: str
    compressed_count: int = 0
    index_count: int = 0
    stopped: bool = False  # 审批屏障 / 无窗可压
    new_l1: str = ""


class Compactor:
    """一次压缩的编排。依赖注入:journal/gate + run_model(切段 prompt->文本)+ sweeper(M3-5)。

    gate 是**真实 Gate**(组装根注入,同 Sweeper 的理由——Port 不放 propose 单写者编进类型)。
    sweeper 就是 M3-5 那只,沉淀筛直接复用,一个都不新写。
    """

    def __init__(
        self,
        journal,
        gate,
        run_model: Callable[[str], Awaitable[str]],
        cut_instructions: str,
        sweeper,
        index_days: int,
    ) -> None:
        self._journal = journal
        self._gate = gate
        self._run_model = run_model
        self._instructions = cut_instructions
        self._sweeper = sweeper
        self._index_days = index_days

    def _pending_count(self) -> int:
        try:
            return len(self._gate.pending())
        except Exception:
            return 0

    def _window(self, since: str, until: str) -> tuple[list[str], dict[str, str], list[Any]]:
        """窗口内**未压缩**的信封 id(时间正序)+ ts 索引 + 窗口事件(envelope/reply)。"""
        ids: list[str] = []
        ts_by_id: dict[str, str] = {}
        events = []
        seen: set[str] = set()
        for e in self._journal.events_in_range(since, until):
            eid = e["envelope_id"]
            if eid not in seen and not self._journal.is_compressed(eid):
                seen.add(eid)
                ids.append(eid)
                ts_by_id[eid] = e["ts"]
            events.append(e)
        return ids, ts_by_id, events

    async def run(self, since: str, until: str) -> CompactResult:
        # 1/4. 审批屏障:pending 非空必须停(证据销毁前必须结案,DESIGN §6.3)。
        pending = self._pending_count()
        if pending:
            return CompactResult(
                f"审批屏障:有 {pending} 条待审提案,压缩停——压缩要销毁提案原始证据,"
                "先 /pending 结案再压",
                stopped=True,
            )

        ids, ts_by_id, window_events = self._window(since, until)
        if not ids:
            return CompactResult(
                f"区间 {since[:16]} ~ {until[:16]} 没有未压缩的内容(已压过或为空)", stopped=True
            )

        # 2. 切段(模型);模型输入/输出同样落起居注(可见即入账)。
        segments = await self._cut(ids, ts_by_id, window_events)

        # 3. 沉淀筛:直接复用 M3-5 的 sweep(同一窗口)——一份实现,不许第二份。
        await self._sweeper.run(since, until)

        # 4. 审批屏障再查:沉淀筛刚提的新 pending 也不能毁证据。
        pending = self._pending_count()
        if pending:
            return CompactResult(
                f"沉淀筛提出了 {pending} 条待审,压缩停:先审完再压(/pending + /approve)",
                stopped=True,
            )

        # 5. 索引 + 标记(正文不删,只在 L0 面退出)
        for seg in segments:
            self._journal.add_index(seg.date, f"{seg.topic} · {seg.conclusion}", seg.envelope_id)
        self._journal.mark_compressed(ids)
        self._journal.prune_index(self._index_days)
        new_l1 = self._journal.l1_block(self._index_days)
        return CompactResult(
            f"压缩 {len(ids)} 轮为 {len(segments)} 条索引;L1 保留 {self._index_days} 天",
            compressed_count=len(ids),
            index_count=len(segments),
            new_l1=new_l1,
        )

    async def _cut(
        self, ids: list[str], ts_by_id: dict[str, str], window_events: list[Any]
    ) -> list[Segment]:
        """切段:窗口对话按话题切成几段(topic + conclusion),钩子信封由代码按顺序分。"""
        prompt = (
            self._instructions + "\n\n" + "\n".join(render_event_line(e) for e in window_events)
        )
        cut_id = f"cut-{uuid.uuid4().hex}"
        self._journal.append(cut_id, "sweep", {"phase": "input", "content": prompt, "kind": "cut"})
        try:
            output = await self._run_model(prompt)
        except Exception as exc:
            self._journal.append(
                cut_id,
                "sweep",
                {
                    "phase": "output",
                    "content": f"切段模型失败:{type(exc).__name__}: {exc}",
                    "kind": "cut",
                },
            )
            return []
        self._journal.append(cut_id, "sweep", {"phase": "output", "content": output, "kind": "cut"})

        try:
            parsed = json.loads(output)
            raw = parsed.get("segments") or []
        except Exception:
            logger.warning("compact: 切段输出不是 JSON,按一段整块处理")
            raw = [{"topic": "对话片段", "conclusion": "这一段对话(详见起居注)"}]
        # 按顺序把未分配的信封分给各段做钩子,日期取钩子信封的日期
        remaining = list(ids)
        segments: list[Segment] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            hook = remaining.pop(0) if remaining else (ids[0] if ids else "")
            date = (ts_by_id.get(hook) or "")[:10]
            segments.append(
                Segment(
                    date=date,
                    topic=str(item.get("topic") or "片段"),
                    conclusion=str(item.get("conclusion") or "见起居注"),
                    envelope_id=hook,
                )
            )
        return segments


def make_compactor(settings: Any, journal: Any, gate: Any, threads: Any) -> Compactor:
    """组装根的压缩工厂:同一廉价模型 runner(切段)+ 复用 M3-5 的 Sweeper 做沉淀筛。"""
    cut_instructions = Path("prompts/cut.md").read_text(encoding="utf-8")
    runner = build_sweep_runner(settings)
    sweeper = make_sweeper(settings, journal, threads, gate)
    return Compactor(journal, gate, runner, cut_instructions, sweeper, settings.compact_index_days)
