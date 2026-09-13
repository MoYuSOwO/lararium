"""隔一阵问一嘴(M6-9)。**这是"助手在场",不是"任务系统戳你"**——晨报、到期提醒、预算推送
(M6-8 砍掉的那批)不从这里复活。

## 形状

    到点 → 造一个 source="nudge" 的信封,内容是**一条指令**(prompts/nudge.md),投进收件箱
         → worker 当普通的一轮跑:前缀 + 档案 + 话头 + L0 + 工具全在
         → 模型的回复就是要推的那句;没什么好说 → 回「不说」→ **不推,不算失败**
         → 信封和回复都落起居注、都进 L0;出件箱只投回复(带保质期),指令一个字都不出去

指令在 L0 里走「(系统触发 · nudge/渠道)」那一支(M4-7 建的)。**伪装成用户的话,历史里就是
"用户说过『主动跟我说一句』",之后每一轮模型都会照做**——这一条是硬的。

**这里只做"什么时候去看一眼"**;说不说、说什么、算不算同一件事,全是模型的。我们给的只有材料
(本来就在前缀、L0、工具里的那些,外加"最近几天你主动说过什么")和"可以不说"的权利。
不新造数据源,也不做"随机挑一条事实寒暄"。

## 什么时候醒(`Nudger.step`)

- **间隔在区间里随机**(默认 45~90 分钟),从**最近一次有人说话**算起:用户的一句,或者上一次
  问一嘴(不管那次说没说)。均匀取秒数,不对齐整点。
- **静默时段里不醒、不发、不攒**:问候过了就没意义。醒来发现落在时段里,就重新从时段结束算一个间隔。
- **上一条主动消息之后,用户没开口,就不发下一条**(硬的)。"开口"= 收件箱里一条 `source="user"`
  的信封:斜杠命令不经收件箱、模型也看不见,算它的话,模型在 L0 里看到的就是自己连着说了两句没人
  接;数据面(短信入账)是银行在说话,不是他。
- **人不在场就不叫**:他最后一句超过 24 小时(微信窗口,官方原话),说了也送不到。
- **聊天优先**:到点时有信封在排队或在处理,这一次让开(和 PDF 转换器、夜间归拢同一个
  `chat_busy`)。用户那句一跑完,"最近一次有人说话"就换成了它,间隔重新算——效果上是跳过。
  信封已经投进去之后用户才发来一句,就按收件箱的先后照常跑:问候先到、回复在后,
  和 L0 里记的顺序一致;中途扔掉问候反而会让模型以为自己说过、用户却没收到。

## 常量(都是拍的)

`REPEAT_DAYS`、`SHELF_LIFE` 是拍的,真机用几天再调;`PRESENCE_WINDOW` 是微信的窗口,不是拍的。
"""

import asyncio
import random
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

from lararium.envelope import Envelope
from lararium.steward.assembler import neutralize_model_text
from lararium.steward.journal import Journal
from lararium.steward.outbox import Outbox
from lararium.timeofday import QuietHours

NUDGE_SOURCE: Final = "nudge"
NUDGE_PROMPT_PATH = Path("prompts/nudge.md")

# 模型说"没什么好说"的那个词。**不用真的空回复**:OpenAI 兼容接口回一个空 content,pydantic-ai
# 收到的是"没有文本也没有工具调用",会自己追一句"请回复"重试——模型被追着就会硬说一句,
# 正好把"可以不说"的权利拿走了。所以约定一个词,空回复照样也算不说。
SILENT_WORD = "不说"

# 同一件事几天内不再拿来开口(拍的)。
REPEAT_DAYS = 3
# 一句问候的保质期(拍的):两小时还没送到,就是过时的"在忙什么"了。
SHELF_LIFE = timedelta(hours=2)
# 微信 ClawBot 只收用户最后一条消息之后 24 小时内的推送(官方原话,M5-1 定性)。
PRESENCE_WINDOW = timedelta(hours=24)

# 包在「不说」外面也认的那些符号:模型常会顺手加个引号或句号。
_SILENT_WRAPPING = " \t\r\n\"'`「」『』“”‘’()()[]【】。.!!"  # noqa: RUF001 - 全角标点就是要认的


def is_silent(text: str | None) -> bool:
    """模型这次选了不说。**不说是正常出口,不是失败**。"""
    return (text or "").strip(_SILENT_WRAPPING) in ("", SILENT_WORD)


def load_nudge_prompt(path: Path = NUDGE_PROMPT_PATH) -> str:
    """那条指令的模板。**住在 `prompts/`**(CONVENTIONS L1:给模型读的文字进文件),组装根读一次。"""
    return path.read_text(encoding="utf-8").rstrip("\n")


@dataclass(frozen=True)
class Spoken:
    """一次真的推出去的主动消息:什么时候、说了什么。"""

    ts: datetime
    content: str


def render_instruction(template: str, *, recent: list[Spoken], timezone: str) -> str:
    """把"最近几天主动说过的"填进模板。说过的是**模型自己写的**文字,重新喂给模型前照样过刀。"""
    tz = ZoneInfo(timezone)
    lines = [
        f"- [{s.ts.astimezone(tz):%m-%d %H:%M}] {neutralize_model_text(s.content)}" for s in recent
    ]
    return template.format(
        silent=SILENT_WORD, days=REPEAT_DAYS, recent="\n".join(lines) or "(没有)"
    )


def expiry_of(envelope: Envelope) -> datetime | None:
    """这个信封的回复该带多长的保质期。只有问一嘴带,别的回复一直等(None)。"""
    if envelope.source != NUDGE_SOURCE or "expires_at" not in envelope.meta:
        return None
    return datetime.fromisoformat(str(envelope.meta["expires_at"]))


def discard_expired(*, outbox: Outbox, journal: Journal, now: datetime) -> int:
    """扔掉过了保质期还没交出去的,**每扔一条记一笔起居注**(`outbox_expired`)。

    不记的话,"她怎么不说话了"没处查:被扔的那句模型以为说过,用户从没收到。
    查法:`SELECT count(*) FROM journal WHERE kind='outbox_expired'`。这个 kind 不进 L0、不进检索。
    """
    dropped = outbox.drop_expired(now)
    for item in dropped:
        journal.append(
            item.envelope_id,
            "outbox_expired",
            {"seq": item.seq, "channel": item.channel, "content": item.content},
        )
    return len(dropped)


class NudgeState:
    """问一嘴要问库的那几件事。全是查已有的表(收件箱、出件箱),除了那个开关。F4:查和改分开。"""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def is_off(self) -> bool:
        return self._conn.execute("SELECT 1 FROM nudge_off WHERE id=1").fetchone() is not None

    def turn_off(self, now: datetime) -> bool:
        """关掉。返回这次是不是新关的(已经关着就是 False)。**没有打开的方法**。"""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO nudge_off (id, off_at) VALUES (1, ?)",
            (now.astimezone(UTC).isoformat(),),
        )
        return bool(cur.rowcount)

    def last_activity(self) -> datetime | None:
        """最近一次有人说话:用户的一句,或者上一次问一嘴(说没说都算)。间隔从这里起算。"""
        return self._latest("SELECT MAX(ts) FROM inbox WHERE source IN ('user', 'nudge')")

    def last_user_message(self) -> datetime | None:
        return self._latest("SELECT MAX(ts) FROM inbox WHERE source='user'")

    def last_spoken(self) -> datetime | None:
        """上一条**真的投进出件箱**的主动消息是什么时候问的(选了不说的不算:用户没东西可回)。"""
        return self._latest(
            "SELECT MAX(i.ts) FROM outbox o JOIN inbox i ON i.id = o.envelope_id "
            "WHERE i.source='nudge'"
        )

    def spoken_since(self, since: datetime) -> list[Spoken]:
        """`since` 之后主动说过的,时间正序。出件箱里那一份就是用户收到的原文(和起居注里的回复同一份)。"""
        rows = self._conn.execute(
            "SELECT i.ts, o.content FROM outbox o JOIN inbox i ON i.id = o.envelope_id "
            "WHERE i.source='nudge' ORDER BY i.ts DESC LIMIT 50"
        ).fetchall()
        spoken = [Spoken(datetime.fromisoformat(r[0]), str(r[1])) for r in rows]
        return [s for s in reversed(spoken) if s.ts >= since]

    def _latest(self, sql: str) -> datetime | None:
        row = self._conn.execute(sql).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None


class Nudger:
    """和 worker、PDF 转换器、夜间归拢并排跑的那一圈。一圈 = `step()` 判一次、该叫就投一个信封,
    然后睡到它说的时候。它自己**从不调模型**:投进收件箱之后,那就是 worker 的普通一轮。"""

    # 聊天那一轮还没完时,隔多久再看一眼。
    CHAT_POLL = 5.0
    # 最长睡多久再自己看一眼:兜住系统时钟被调、机器睡眠。
    IDLE_POLL = 3600.0

    def __init__(
        self,
        *,
        state: NudgeState,
        submit: Callable[[Envelope], None],
        chat_busy: Callable[[], bool],
        quiet: QuietHours,
        timezone: str,
        channel: str,
        instructions: str,
        min_interval: timedelta,
        max_interval: timedelta,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._state = state
        self._submit = submit
        self._chat_busy = chat_busy
        self._quiet = quiet
        self._tz = ZoneInfo(timezone)
        self._timezone = timezone
        self._channel = channel
        self._instructions = instructions
        self._min = min_interval
        self._max = max_interval
        # 可注入的时钟、sleep、随机数:测试走假时钟和种子,不真等。
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep or asyncio.sleep
        self._rng = rng or random.Random()  # noqa: S311 - 定间隔用的,不是密码学用途
        # 这一次该醒的时刻,和它是从哪次说话算起的。**放内存**:重启重新抽一次,本来就是随机的。
        self._anchor: datetime | None = None
        self._due = datetime.min.replace(tzinfo=UTC)

    async def run(self) -> None:
        while True:
            await self._sleep(self.step())

    def step(self) -> float:
        """判一次,该叫就投一个信封。返回离下次该醒还有几秒。"""
        now = self._clock()
        if self._state.is_off():
            return self.IDLE_POLL
        anchor = self._state.last_activity()
        if anchor is None:
            return self.IDLE_POLL  # 从没人说过话:没人可问
        if anchor != self._anchor:
            self._anchor = anchor
            # 从那次说话起抽一个间隔;已经过了(刚起来、上次说话在昨天)就从现在起抽,别一起来就发。
            self._due = anchor + self._draw()
            if self._due <= now:
                self._due = now + self._draw()
        if self._quiet.contains(now, self._tz):
            end = self._quiet.ends_at(now, self._tz)
            if self._due < end:
                self._due = end + self._draw()  # 不攒:时段结束后隔一阵再说,而且不是整点
            return self._until(now)
        if now < self._due:
            return self._until(now)
        last_user = self._state.last_user_message()
        if last_user is None or now - last_user > PRESENCE_WINDOW:
            return self._min.total_seconds()  # 人不在场
        last_spoken = self._state.last_spoken()
        if last_spoken is not None and last_user <= last_spoken:
            return self._min.total_seconds()  # 上一句还没人接
        if self._chat_busy():
            return self.CHAT_POLL
        self._submit(self._envelope(now, last_user))
        return self.CHAT_POLL

    def _draw(self) -> timedelta:
        return timedelta(
            seconds=self._rng.uniform(self._min.total_seconds(), self._max.total_seconds())
        )

    def _until(self, now: datetime) -> float:
        return max(0.0, min((self._due - now).total_seconds(), self.IDLE_POLL))

    def _envelope(self, now: datetime, last_user: datetime) -> Envelope:
        recent = self._state.spoken_since(now - timedelta(days=REPEAT_DAYS))
        # 保质期取两者先到的:两小时,或者他那条消息的微信窗口关上的那一刻。
        expires = min(now + SHELF_LIFE, last_user + PRESENCE_WINDOW)
        return Envelope(
            id=uuid.uuid4().hex,
            source=NUDGE_SOURCE,
            channel=self._channel,
            content=render_instruction(self._instructions, recent=recent, timezone=self._timezone),
            meta={"expires_at": expires.astimezone(UTC).isoformat()},
            ts=now.astimezone(UTC),
        )
