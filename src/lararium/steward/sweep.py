"""夜间归拢(M3-5):扫一段起居注,把漏记的补上——该开的话头开上、该关的关掉、
漏掉的事实提一条(pending,provenance=untrusted 走硬门控)。

**只写话头和 pending 提案,绝不动账本正文**——账本只有一条写入路径:Gate.settle()(门控)。
这里只 propose 进 pending 隔离区,结算是用户审批 + /settle 的事。这是夜间归拢整个 M3 里
最容易破「单写者」的地方:它跑在没人看着的时候,手里又正好攥着一堆"还没聊完的事"。

**模型参与的输入输出都落起居注**(sweep 事件,`input`/`output` 两个 phase)——可见即入账,
不因为它是后台任务就绕过。喂给模型的 prompt 是什么,prove 落下去的就是什么。

**幂等(P1-1)**:按**内容**幂等,不是按时间区间字符串——唯一调用方 /sweep 每次传
now-24h~now,区间永远不同,按区间字符串一秒三次 → 模型调三次 → 三条重复提案。
光标(sweep_state.cursor_seq)记录**实际喂给模型的最大 journal seq**,下次从那之后扫,
光标之后没有新内容就是 no-op。

**扫哪一段:下界是光标,不是 since(M5-24)**。原来两条边都由时间窗定,而光标推的是
"窗口内最大 seq"——`光标 < seq < 窗口下界` 那一段谁都没扫过,推完还再也回不来
(`_advance_cursor` 只增不减)。缺口攒下来要**分批**喂:一次全塞进去,字数上限会把最早的
砍掉,那是换一种方式丢同样的东西。每批喂完推一次光标,**只推到这一批实际喂进去的那条**。

**账本进 prompt(P1-2)**:喂模型的 prompt 带「已经记在账本里的(别重复提)」——不然模型会
反复提已入档的事实,那些和重复提交的提案一起把 pending 堵死,压缩又被自己挡住(死循环)。

**失败不影响主循环**:模型调用失败 / 输出不是 JSON → 返回一句可读说明,不抛。

**一个进程一个 Sweeper,`run` 互斥(M6-8)**:手动 `/sweep`、夜间那一班、压缩里的沉淀筛
共用同一个实例。三条路可能同时到(上线那一刻补跑刚开始、人正好敲了一句 /sweep),
不互斥的话两边读到同一个光标,同一段对话各喂一遍,提案成双。
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from lararium.steward.assembler import FENCE_CLOSE, FENCE_OPEN, fold_text, neutralize_fence
from lararium.steward.model import ModelCallError

logger = logging.getLogger("lararium")

# sweep.suggest 归进"长期偏好"节(模型推断的事实默认落这里),审批卡上能看到原文。
# **写死的:归拢这条路上模型从没被问过小节**(对话那条路问了,见 memory 的 propose_fact)。
# 后果只是账本里归错格——整份账本每轮都进前缀,没有任何代码按小节分支(M5-24 查证)。
_SECTION = "长期偏好"

# 一批对话的字数上限:保护廉价模型的窗口。**这是分批的刀口,不是截断的刀口**
# ——装不下的留给下一批(见 _batches),不是丢掉。原来它是截断:超了就只保留最近部分,
# 而光标照样推到最新那条,被砍掉的最早那几条**喂都没喂就永久跳过**(M5-24 的第二张脸)。
_PROMPT_CONVO_MAX_CHARS = 20000

# 一次 run 最多扫几批。缺口攒了两周就是几千条,一次全补完等于一晚上几十次模型调用;
# 分次补:光标每批都推进,进度不会丢,摘要里会说还剩多少。
_SWEEP_MAX_BATCHES = 6

# 一次 run 最多从起居注取多少条待扫事件(够填满 _SWEEP_MAX_BATCHES 批,多取没用)。
_SCAN_LIMIT = 2000


@dataclass
class SweepResult:
    summary: str
    opened: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    suggested: int = 0
    skipped: bool = False  # 幂等跳过(true 时不调模型、不改任何东西)
    # M6-8:下面三个原来只写在 summary 的措辞里。压缩要分"失败 / 没扫完"(M5-30,原来认的是
    # 摘要前缀),夜间那一班还要按状态码分"怪不怪这一天"(E4)——从措辞里认,一个叫「归拢失败」
    # 的话头就能骗过它。
    failed: bool = False  # 模型调用抛了 / 输出解不开:这次没跑成(中途失败时前面几批照样算数)
    status: int | None = None  # 失败时服务商回的 HTTP 状态码;没有就是 None
    unfinished: bool = False  # 没失败,但撞了批数或取数上限,还有没喂进去的


def nominal_window(now: datetime) -> tuple[str, str]:
    """一次触发的名义窗口 `(now-24h, now)`,手动 `/sweep` 和夜间那一班共用。

    **`since` 不决定扫哪一段**(M5-24):下界是光标,since 只落进起居注当由头。
    写成函数只是为了两处不各写一份"24 小时"。
    """
    return (now - timedelta(hours=24)).isoformat(), now.isoformat()


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
        # M6-8:`run` 互斥(见模块 docstring 末段)。锁在实例上,所以组装根只造一个实例。
        self._running = asyncio.Lock()

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

    def _build_prompt(self, opens, events, batch_note: str = "") -> str:
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
        if batch_note:
            parts.append(batch_note)
        convo = self._render_events(events)
        parts.append(convo if convo else "(无)")
        return "\n".join(parts)

    @classmethod
    def _render_events(cls, events) -> str:
        """把这一批渲染成正文。**这里不再截断**——字数由 `_batches` 在分批时管住了,
        截断和分批两套机制并存只会让"到底喂进去了哪些"再次说不清(M5-24)。"""
        return "\n".join(render_event_line(e) for e in events)

    @classmethod
    def _batches(cls, events) -> list[list[Any]]:
        """把待扫事件按渲染后的字数切成批,**从最早的那条开始装**,装不下的留给下一批。

        方向是要害:原来的做法是"超了就只保留最近部分",而光标推到最新那条
        ——最早那几条喂都没喂就被跳过,和 M5-24 的主 bug 是同一个形状。从早往晚装,
        每批喂完把光标推到这一批的最后一条,下一批接着装,一条都不会被越过去。

        单条就超上限的(理论上到不了:入站正文 16KB 封顶)也自成一批照喂——
        撑爆走"归拢失败"可重试,悄悄丢掉不可重试。
        """
        batches: list[list[Any]] = []
        current: list[Any] = []
        total = 0
        for event in events:
            size = len(render_event_line(event)) + 1
            if current and total + size > _PROMPT_CONVO_MAX_CHARS:
                batches.append(current)
                if len(batches) == _SWEEP_MAX_BATCHES:
                    return batches
                current, total = [], 0
            current.append(event)
            total += size
        if current:
            batches.append(current)
        return batches

    async def run(self, since: str, until: str) -> SweepResult:
        """扫「光标之后 ~ until」的全部对话,分批喂,每批喂完推一次光标。

        **`since` 不再决定扫哪一段**(M5-24):它只是这次触发的名义窗口,留进起居注和
        摘要里当由头。下界只能是光标——按时间取下界的那版会把 `光标 < seq < 窗口下界`
        那一段整段跳过,而且光标只增不减,跳过去就再也回不来。

        **同一个实例上的 run 一次只跑一个**(M6-8):后到的等前一个跑完,再读光标——
        读到的已经是推过的那个,喂过的不会再喂。
        """
        async with self._running:
            return await self._run(since, until)

    async def _run(self, since: str, until: str) -> SweepResult:
        cursor = self._cursor()
        pending = self._journal.events_after_seq(cursor, until, limit=_SCAN_LIMIT)
        batches = self._batches(pending)
        if not batches:
            if cursor:
                # P1-1 内容幂等:光标之后没有新内容就是 no-op(不调模型、不改任何东西)。
                return SweepResult(
                    summary=f"起居注 seq {cursor} 之后没有新内容(截至 {until[:16]}),跳过",
                    skipped=True,
                )
            # 从没归拢过、起居注也没有对话:空跑一次无害(且兼容手动测窗口)。
            batches = [[]]
        maybe_more = len(pending) >= _SCAN_LIMIT  # 取满上限,后面可能还有

        opened: list[str] = []
        closed: list[str] = []
        suggested = 0
        stopped = ""
        status: int | None = None
        fed = 0
        for index, batch in enumerate(batches, start=1):
            # 话头每批重取:上一批开/关过的,这一批的模型要看到最新的那份。
            opens = self._threads.all_open_threads()
            note = _batch_note(index, len(batches)) if len(batches) > 1 else ""
            prompt = self._build_prompt(opens, batch, note)
            sweep_id = f"sweep-{uuid.uuid4().hex}"
            # 起居注里记清楚**这一批实际喂了哪一段 seq**——原来只有 since/until,
            # 于是"跳过了什么"在起居注里查不出来,而失效本来就是静默的(M5-24)。
            span = {
                "since": since,
                "until": until,
                "from_seq": batch[0]["seq"] if batch else None,
                "to_seq": batch[-1]["seq"] if batch else None,
                "batch": f"{index}/{len(batches)}",
            }

            # 可见即入账:输入先落,输出后落;模型实收的就是这份 prompt 原文
            self._journal.append(sweep_id, "sweep", {**span, "phase": "input", "content": prompt})
            try:
                output = await self._run_model(prompt)
            except Exception as exc:  # 模型调用失败:不影响主循环,可重试(不推进光标)
                self._journal.append(
                    sweep_id,
                    "sweep",
                    {
                        **span,
                        "phase": "output",
                        "content": f"模型调用失败:{type(exc).__name__}: {exc}",
                    },
                )
                stopped = f"归拢失败(不影响对话):{type(exc).__name__}"
                status = exc.status if isinstance(exc, ModelCallError) else None
                break
            self._journal.append(sweep_id, "sweep", {**span, "phase": "output", "content": output})

            try:
                plan = json.loads(output)
            except Exception:
                stopped = "归拢:模型输出不是 JSON,本次无动作(可重试)"
                break
            if not isinstance(plan, dict):
                stopped = "归拢:模型输出不是对象,本次无动作"
                break

            self._apply(plan, opened, closed)
            suggested += self._propose_all(plan)
            # ★ M5-24 的根:光标绑的是**这一批实际喂进去的最后一条**,不是窗口里最大的那条。
            if batch:
                self._advance_cursor(batch[-1]["seq"])
            fed += len(batch)

        # P1-3:归拢提出提案 → 通知用户(别再让 pending 悄悄压死压缩)。日限由注入的通知器管。
        if suggested:
            self._notify(f"夜间归拢提出 {suggested} 条待审提案(/pending 查看)")
        # 没扫完的要**说出来**(含中途失败剩下的那几批):失效静默是这条 bug 最贵的地方
        # ——少提的事实和"模型觉得不值得提"长得一模一样,谁都看不出来。
        left = len(pending) - fed
        summary = stopped or _summarize(opened, closed, suggested)
        if left > 0:
            amount = f"至少 {left}" if maybe_more else f"{left}"
            summary += f";还有 {amount} 条没扫完,再跑一次 /sweep 接着补"
        elif maybe_more:
            summary += f";这次取满了 {_SCAN_LIMIT} 条上限,可能还有没扫完的,再跑一次 /sweep"
        return SweepResult(
            summary=summary,
            opened=opened,
            closed=closed,
            suggested=suggested,
            failed=bool(stopped),
            status=status,
            unfinished=not stopped and (left > 0 or maybe_more),
        )

    def _apply(self, plan, opened: list[str], closed: list[str]) -> None:
        """只写话头,绝不动账本正文(Gate.settle 是唯一写路径)。"""
        for item in plan.get("open") or []:
            if isinstance(item, dict) and item.get("topic"):
                t = self._threads.open_thread(str(item["topic"]), str(item.get("note") or ""))
                opened.append(t.topic)
        for topic in plan.get("close") or []:
            if isinstance(topic, str) and self._threads.close_thread(topic):
                closed.append(topic)

    def _propose_all(self, plan) -> int:
        suggested = 0
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
        return suggested


def _batch_note(index: int, total: int) -> str:
    """告诉模型这是分批扫的第几批。**不是装饰**:上下文从对话中间开始,不说一声,
    模型会把"前面没头没尾"当成对话本身的样子去归纳。"""
    return f"(对话过长,这次分 {total} 批扫,这是第 {index} 批,按时间从早到晚)"


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


# writing-facts 方法篇里"给归拢也看"的那一半到哪儿为止。**分界线是文件里的标记,
# 不是标题名**:标题会被改写,标记不会——而改写标题的人看不出自己顺手切掉了归拢的判据。
_SWEEP_CUT = "<!-- SWEEP-CUT"


def _fact_rules(registry: Any) -> str:
    """把 memory 的 `writing-facts` 方法篇拼进归拢 prompt(M5-23)。

    **判据只有一份,归拢借的就是那一份。** 以前 sweep.md 自己写了一套,最后一句还说
    「写用户的原话,别加你的解读和推断」——正好和账本要的东西相反,而模型听了那句:
    真机 31 轮跑下来提上来 3 条全是原话(「学校嘛 那天天上课睡觉…」),
    人工翻同样 31 轮找得出的 4 条事实一条都没提。**打架的三份文档里赢的是最差那份。**

    不给归拢开工具是有意的(`build_sweep_runner`:一次 user 消息、`system_prompt=""`、
    no tools)——归拢是批处理,不需要探索能力。所以它读不到方法篇,只能在这里拼。

    读不到就**炸在启动时**,不静默降级:降级的样子和"模型今天状态不好"一模一样,
    而代价是又一晚上的原话(M5-4 那条教训的同一个形状)。
    """
    text = registry.read_skill("memory", "writing-facts")
    head = text.split(_SWEEP_CUT)[0].rstrip()
    if _SWEEP_CUT not in text or not head:
        raise ValueError(
            f"writing-facts 方法篇里找不到 {_SWEEP_CUT} 分界线,归拢就没有判据可拼了"
            f"——那正是 M5-23 修的那个 bug。别删这个标记,要挪就连着上下两半一起想清楚。"
        )
    return f'## 什么算"该记的事实"(memory 的 writing-facts 方法篇原文,别另立一套)\n\n{head}'


def make_sweeper(
    settings: Any,
    journal: Any,
    threads: Any,
    gate: Any,
    registry: Any,
    ledger: Any = None,
    notify: Callable[[str], None] | None = None,
) -> Sweeper:
    """组装根的归拢工厂:prompts/sweep.md + writing-facts 方法篇 + 廉价模型 runner。"""
    instructions = (
        Path("prompts/sweep.md").read_text(encoding="utf-8") + "\n\n" + _fact_rules(registry)
    )
    return Sweeper(
        journal,
        threads,
        gate,
        build_sweep_runner(settings),
        instructions,
        ledger=ledger,
        notify=notify,
    )
