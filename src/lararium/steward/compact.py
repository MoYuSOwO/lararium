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

**2、3 步任一没跑成,第 5 步就不做**(M5-30):正文不删守住的是"翻得到",
索引才是"会被翻到",而这两件事不是一回事。前置步骤失败时整批留在 L0,
下次低水位触发再来——失败不抛(别让一次模型抖动打崩整轮)、也不原地重试(那是空转)。

数字口径全部走 estimate_tokens + _render_overhead(渲染后形态,M3-1b/M3-3 定死):
整窗 200000、低水位 150000、索引保留 90 天,不自己发明。
"""

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from lararium.steward.sweep import build_sweep_runner, make_sweeper, render_event_line

logger = logging.getLogger("lararium")


@dataclass
class Segment:
    date: str
    topic: str
    conclusion: str
    envelope_id: str


@dataclass
class CutOutcome:
    """切段的结果。**空列表原来是重载的**:它既是"切段失败",也是"模型说没什么可切",
    而 `run()` 分不出这两件事——于是模型抖一下就变成"索引一条没建、那批却已经退出 L0"
    (M5-30)。所以先把它拆开:`failure` 非空 = 这次切段没成,`segments` 不作数。

    `failure` 是给人看的一句话(进摘要、进日志),不是错误码——调用方只需要判它空不空。
    """

    segments: list[Segment]
    failure: str = ""


@dataclass
class CompactResult:
    summary: str
    compressed_count: int = 0
    index_count: int = 0
    stopped: bool = False  # 没提交压缩标记:审批屏障 / 无窗可压 / 前置步骤失败
    # M5-30:把"停"再分一层。stopped 说的是"这一轮没压",failed 说的是"因为前置步骤
    # 没跑成"——屏障停是系统按设计拦的(要人去结案),失败是外面出了岔子(等下次重来)。
    # 两者的处置不同,所以不能只有一个位。
    failed: bool = False
    new_l1: str = ""


# 归拢是**以结果返回**失败的(不抛),而 `SweepResult` 里没有结构化的失败位。这里只能认
# 它摘要的前缀。耦合是真的,所以配了一条走真 Sweeper 的测试钉住它
# (`test_sweep_failure_blocks_compression`)——sweep.py 哪天改了措辞,那条当场红。
# 正经做法是给 `SweepResult` 加一个 failed 位,但 sweep.py 这一轮不归我动(M5-24 刚改完)。
_SWEEP_FAILURE_PREFIXES = ("归拢失败", "归拢:模型输出不是")


def _sweep_failed(result: Any) -> bool:
    """归拢**失败**(模型抛 / 输出解不开)算数,归拢**没扫完**(批数上限)不算数。

    分界的理由:

    - **失败**时筛子一条都没筛,而第 4 步「审批屏障再查」存在的理由正是"沉淀筛刚提的
      新 pending 也不能毁证据"——筛子没跑成的时候,那一步查到的 0 条是**假的安全**。
    - **没扫完**时筛子真的跑过、也真的提过它看到的那些,而且它的光标只推到实际喂进去的
      那一条(M5-24),剩下的下次接着补。把它也当红灯的话,积压期间压缩永远轮不上
      ——而那正是上下文最满、最需要压的时候。

    摘要为空(理论上到不了)按"没失败"算:宁可漏判一次,也不要凭一个空字符串挡住压缩。
    """
    return str(getattr(result, "summary", "")).startswith(_SWEEP_FAILURE_PREFIXES)


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
        timezone: str,
        notify: Callable[[str], None] | None = None,
    ) -> None:
        self._journal = journal
        self._gate = gate
        self._run_model = run_model
        self._instructions = cut_instructions
        self._sweeper = sweeper
        self._index_days = index_days
        self._tz = ZoneInfo(timezone)
        # P1-3:被审批屏障停时的通知(组装根注入带日限的通知器);None = 静默。
        self._notify = notify or (lambda _text: None)

    def _local_date(self, ts: str) -> str:
        """起居注 ts 是 UTC;索引行的日期必须走配置时区(L1 时间戳同 L0 的规矩)。

        凌晨对话:UTC 17:40 在 Asia/Shanghai 是次日 01:40,直接取 UTC 前 10 位会差一天
        (M1 Task 9 那个坑换了个地方)。
        """
        try:
            return datetime.fromisoformat(ts).astimezone(self._tz).date().isoformat()
        except ValueError:
            return ts[:10]

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
            # P1-3:被自己挡住不能悄悄——通知用户去结案,否则死循环没人知道
            self._notify(f"压缩暂停:{pending} 条待审提案,先 /pending 结案再压")
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
        cut = await self._cut(ids, ts_by_id, window_events)
        if cut.failure:
            return self._unfinished(cut.failure, ids)

        # 3. 沉淀筛:直接复用 M3-5 的 sweep(同一窗口)——一份实现,不许第二份。
        sweep = await self._sweeper.run(since, until)
        if _sweep_failed(sweep):
            # 归拢没跑成 → 下面那道屏障查到的 0 条是假的(见 `_sweep_failed`)。
            return self._unfinished(sweep.summary, ids)

        # 4. 审批屏障再查:沉淀筛刚提的新 pending 也不能毁证据。
        pending = self._pending_count()
        if pending:
            self._notify(f"压缩暂停:{pending} 条待审提案,先 /pending 结案再压")
            return CompactResult(
                f"沉淀筛提出了 {pending} 条待审,压缩停:先审完再压(/pending + /approve)",
                stopped=True,
            )

        # 5. 索引 + 标记(正文不删,只在 L0 面退出)。**两步一个事务**:索引写了一半崩掉、
        # 或者索引写完标记没写上,留下的都是"半份索引 + 没标记",而没标记意味着下次重压
        # 同一窗口、那半份再写一遍(M5-30 顺手收的那道缝)。
        with self._journal.transaction():
            for seg in cut.segments:
                self._journal.add_index(
                    seg.date, f"{seg.topic} · {seg.conclusion}", seg.envelope_id
                )
            self._journal.mark_compressed(ids)
        # 剪枝不进事务:它是独立的保留期维护,和"这一批压没压"没有一起成败的关系。
        self._journal.prune_index(self._index_days)
        new_l1 = self._journal.l1_block(self._index_days)
        return CompactResult(
            f"压缩 {len(ids)} 轮为 {len(cut.segments)} 条索引;L1 保留 {self._index_days} 天",
            compressed_count=len(ids),
            index_count=len(cut.segments),
            new_l1=new_l1,
        )

    def _unfinished(self, reason: str, ids: list[str]) -> CompactResult:
        """前置步骤没成功 → **不推进压缩标记**,这批留在 L0,等下次自然触发再来。

        三个"不"各有理由:

        - **不标记**:正文虽然不删,但索引一条没建就把这批推出 L0,等于只剩"翻得到"、
          不再"会被翻到"——而这两件事不是一回事(M5-30 的根)。
        - **不抛**:一次模型抖动不该把整轮打崩;吞下来至少还把失败落进了起居注,
          外面(worker)只会少压一次,对话照常。
        - **不重试**:压缩由低水位触发,标记没推进 → 这批下次还在待压队列里,自己会重来。
          在这里循环重试就是模型持续不可用时的一个烧钱空转。真正的触发点是 worker 的
          **空闲**块(队列从有活排到空,且距上次至少 5 分钟),所以重来的节奏由用户活动
          决定,不会自己转起来。
        """
        return CompactResult(
            f"压缩未完成:{reason};这 {len(ids)} 轮留在 L0,下次触发再压",
            stopped=True,
            failed=True,
        )

    async def _cut(
        self, ids: list[str], ts_by_id: dict[str, str], window_events: list[Any]
    ) -> CutOutcome:
        """切段:窗口对话按话题切成几段。每行带信封 id(内部 id,不是用户数据),
        模型每段回 envelope_ids 或 start;代码校验 id 在本窗口内——认不出的丢掉、
        缺的退回按位置分;日期取该段真正钩子所在日的本地日期。

        **失败一律走 `CutOutcome.failure`,不返回空列表**(M5-30)。失败有三条路:
        模型调用抛、回来的东西解不开、以及"窗口里有轮要压却一段都没切出来"
        ——最后这条也是失败:`run()` 只在 `ids` 非空时才叫到这里。
        """
        lines = [f"[{e['envelope_id']}] {render_event_line(e)}" for e in window_events]
        prompt = self._instructions + "\n\n" + "\n".join(lines)
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
            return CutOutcome([], f"切段模型失败({type(exc).__name__})")
        self._journal.append(cut_id, "sweep", {"phase": "output", "content": output, "kind": "cut"})

        try:
            parsed = json.loads(output)
            raw = parsed.get("segments") or []  # 不是 dict 的话 .get 抛,一并归到失败
        except Exception:
            # 第二条失败路径。这里原来有个「按一段整块处理」的兜底:把整窗压成一条内容是
            # 空话的索引行,然后照常标记压缩——那是拿一条假书签换掉整批对话的自动可见性,
            # 比不压更糟。解不开就是没切成,留着下次再切。
            logger.warning("compact: 切段输出不是可用的 JSON,这一批不压")
            return CutOutcome([], "切段输出不是可用的 JSON")
        if not isinstance(raw, list):
            return CutOutcome([], "切段输出的 segments 不是列表")

        valid = set(ids)
        used: set[str] = set()
        remaining = list(ids)  # 回退:按位置给还没用过的窗口信封
        segments: list[Segment] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            claimed = item.get("envelope_ids") or ([item["start"]] if item.get("start") else [])
            # 认得出的、还没给过的钩子优先
            good = [c for c in claimed if isinstance(c, str) and c in valid and c not in used]
            if good:
                hook = good[0]
            else:
                while remaining and remaining[0] in used:
                    remaining.pop(0)
                hook = remaining.pop(0) if remaining else ""
            if not hook:
                continue
            used.add(hook)
            segments.append(
                Segment(
                    date=self._local_date(ts_by_id.get(hook) or ""),
                    topic=str(item.get("topic") or "片段"),
                    conclusion=str(item.get("conclusion") or "见起居注"),
                    envelope_id=hook,
                )
            )
        if not segments:
            # 第三条失败路径:`ids` 非空却一段都没切出来——有轮要压就该至少有一段,
            # 这本身就是信号。当成"成功地什么都没切"正好走进 M5-30 那条 bug。
            return CutOutcome([], f"切段没切出任何一段(窗口里有 {len(ids)} 轮待压)")
        return CutOutcome(segments)


def make_compactor(
    settings: Any,
    journal: Any,
    gate: Any,
    threads: Any,
    registry: Any,
    ledger: Any = None,
    notify: Callable[[str], None] | None = None,
) -> Compactor:
    """组装根的压缩工厂:同一廉价模型 runner(切段)+ 复用 M3-5 的 Sweeper 做沉淀筛。"""
    cut_instructions = Path("prompts/cut.md").read_text(encoding="utf-8")
    runner = build_sweep_runner(settings)
    # 沉淀筛复用 M3-5 的 Sweeper,**不写第二份**——所以判据也自动是同一份(M5-23)。
    sweeper = make_sweeper(settings, journal, threads, gate, registry, ledger=ledger, notify=notify)
    return Compactor(
        journal,
        gate,
        runner,
        cut_instructions,
        sweeper,
        settings.compact_index_days,
        settings.timezone,
        notify=notify,
    )
