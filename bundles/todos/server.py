"""todos bundle —— 待办:一次性的事、作业、考试(M6-7)。

**和话头的边界**(论证在 REVIEW M6-7 最前面):待办是**要去做的一件事**,做了就能勾掉
(交了、考完了、取回来了);话头是**还没聊出结论的一段话**(在考虑要不要换手机、那笔钱
回头再说、在等谁回复),有结论了就关掉。判据不是"有没有到期日"——「买洗衣液」没有日子,
也是一件做完能勾掉的事。**同一件事只记一边**,这句写在 `todos__add_todo` 的 docstring 上。

**到期日是查询字段,不是闹钟**:用户砍掉了整个推送方向(PLAN M6-8),「这周要交什么」
是用户问了才答。docstring 里写着,免得下一个人看到到期日就加提醒。

**课程表不在这里**:它是周期性的,M6-6 已经定了是学习 bundle 里的一份笔记。「哪门课」是
自由文本,**不校验**它在不在学习 bundle 里(bundle 之间不许 import,课程管理在 courses)。

**不做**:优先级、子任务、标签、重复任务。

S2:本文件过了 300 行。存储已经拆在 `store.py`(严格档);这里是五个工具的 docstring 与人话
+ FastMCP 适配。五个工具共用同一套渲染(一行怎么写、日期怎么说、文本怎么中和),拆开就是
两份口径(P1-1 的形状)。
"""

import re
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastmcp import FastMCP

from bundles.runtime import BundleRuntime
from bundles.todos.store import Listing, Todo, TodoStore

# 一页最多几条。limit 不是参数(模型只能翻页),但 page 是——不封顶就是整张表倒进 L0,
# 而压缩是全系统仅有的两个缓存重建点之一。同 list_threads 的 MAX_THREAD_ROWS。
MAX_TODO_ROWS = 20
# 写入时的上限,**超了拒绝,不悄悄截**:截掉的那半句模型不知道没了。
# 一页最坏 20 条 *(60 + 24 + 200 + 标记)≈ 6000 字,和一次检索同量级。
MAX_TITLE_CHARS = 60
MAX_COURSE_CHARS = 24
MAX_NOTE_CHARS = 200

# 围栏分隔符,**从 lararium.steward.assembler 抄来的一份**(bundle 不许 import steward)。
# 抄了会漂,`test_fence_markers_match_the_stewards` 把两边钉在一起(同 finance)。
FENCE_OPEN = "<<<"
FENCE_CLOSE = ">>>"
_OPEN = "「"
_CLOSE = "」"

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _one_line(text: str) -> str:
    """模型写的文本(标题、课名、备注、删除理由)进一行之前的两刀:折行 + 中和界符。

    折行:这一行里的换行没有任何上游替我们折(工具结果当轮原样进模型,M6-5 探针),
    一个标题就能凭换行伪造出下一条待办、连 `[已完成]` 都能写上(P1-2)。
    中和:`>>>` 能提前闭合围栏(P1-3),「」能在同一行伪造出第二个字段。
    """
    folded = re.sub(r"\s+", " ", text).strip()
    return (
        folded.replace(FENCE_OPEN, "＜＜＜")  # noqa: RUF001 - 换成全角形近字是目的不是笔误
        .replace(FENCE_CLOSE, "＞＞＞")  # noqa: RUF001 - 同上
        .replace(_OPEN, "﹁")
        .replace(_CLOSE, "﹂")
    )


def _quoted(text: str) -> str:
    return f"{_OPEN}{_one_line(text)}{_CLOSE}"


def _parse_due(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw.strip())
    except (ValueError, AttributeError):
        return None


def _day(d: date) -> str:
    return f"{d.isoformat()} {_WEEKDAYS[d.weekday()]}"


def _tags(t: Todo, today: date) -> list[str]:
    """行首的方括号标记。**过期只标没做、没删的**:做完了不是过期,删掉的不在名单上。"""
    tags = []
    if t.done_at is not None:
        tags.append(f"[已完成 {t.done_at[:10]}]")
    if t.deleted_at is not None:
        reason = f" · 原因{_quoted(t.deleted_reason)}" if t.deleted_reason else ""
        tags.append(f"[已删除{reason}]")
    if t.done_at is None and t.deleted_at is None and t.due is not None:
        due = date.fromisoformat(t.due)
        if due < today:
            tags.append(f"[已过期 {(today - due).days} 天]")
        elif due == today:
            tags.append("[今天到期]")
    return tags


def _line(t: Todo, today: date) -> str:
    """一条待办一行,#id 在最前(喂给勾掉 / 改 / 删),状态标记紧跟着——扫一眼行首就够。"""
    parts = [f"- #{t.id}", *_tags(t, today), _quoted(t.title)]
    fields = []
    if t.course:
        fields.append(f"课{_quoted(t.course)}")
    if t.due is not None:
        fields.append(f"到期 {_day(date.fromisoformat(t.due))}")
    if t.note:
        fields.append(f"备注{_quoted(t.note)}")
    return " ".join(parts) + "".join(f" · {f}" for f in fields)


def _summary(t: Todo) -> str:
    """回话里说"是哪一条"用的短形状:标题 + 课 + 到期日。"""
    bits = [_quoted(t.title)]
    if t.course:
        bits.append(f"课{_quoted(t.course)}")
    if t.due is not None:
        bits.append(f"到期 {_day(date.fromisoformat(t.due))}")
    return " · ".join(bits)


def _missing(todo_id: object) -> str:
    return f"没有 #{todo_id} 这条待办,什么都没动。先用 todos__list_todos 看一眼,#id 在每行开头。"


def _clean_title(raw: str) -> str | None:
    title = re.sub(r"\s+", " ", raw or "").strip()
    return title if 0 < len(title) <= MAX_TITLE_CHARS else None


def _clean_course(raw: str) -> str | None:
    """空串 → None(不属于哪门课);超长由调用方判。"""
    course = re.sub(r"\s+", " ", raw or "").strip()
    return course or None


def _clean_note(raw: str) -> str | None:
    note = (raw or "").strip()
    return note or None


def _past_warning(due: str | None, today: date) -> str:
    if due is not None and date.fromisoformat(due) < today:
        return "(注意:这个日子已经过了——要是日期算错了,用 todos__update_todo 改)"
    return ""


def _header(
    listing: Listing,
    *,
    today: date,
    due_before: str | None,
    course: str | None,
    include_done: bool,
    include_deleted: bool,
    page: int,
    pages: int,
) -> str:
    scope = "待办" if include_done else "没完成的待办"
    extras = [w for w, on in (("含已完成", include_done), ("含已删除", include_deleted)) if on]
    if extras:
        scope += f"({'、'.join(extras)})"
    filters = []
    if due_before is not None:
        filters.append(f"到期在 {due_before} 及以前的")
    if course is not None:
        filters.append(f"课{_quoted(course)}的")
    overdue = f",其中 {listing.overdue} 条已过期" if listing.overdue else ""
    return (
        f"{''.join(filters)}{scope}一共 {listing.total} 条{overdue},第 {page}/{pages} 页。"
        f"今天是 {_day(today)}:"
    )


def _tool_functions(store: TodoStore, tz: ZoneInfo) -> list[Callable]:
    """工具顺序即冻结顺序(前缀第 0 层),由 manifest.yaml 与测试钉死。工具边界不抛异常(E2)。"""

    def today() -> date:
        # 「今天」按配置时区(M1 Task 9 那个 8 小时时差):date.today() 是机器本地日期。
        return datetime.now(tz).date()

    def stamp() -> str:
        return datetime.now(tz).replace(tzinfo=None).isoformat(timespec="seconds")

    def add_todo(
        title: str,
        due: str | None = None,
        course: str | None = None,
        note: str | None = None,
    ) -> str:
        """记一条待办:一件要去做、做完能勾掉的事——交材料、交作业、考试、去取体检报告。
        title 写这件事本身(60 字以内,细节放 note);course 是「哪门课」,自由文本、不校验,
        作业和考试带上,别的事不用填。
        due 是到期日,只认 YYYY-MM-DD:「下周五」这类相对说法**先调 current_time 拿到今天
        是几号、自己换算好再传**,这里不猜。
        **到期日是拿来查的,不是闹钟**:到了日子什么都不会响,「这周要交什么」是用户问了,
        你再用 todos__list_todos 查。
        **聊到一半、还没下结论的事不记在这里,用 open_thread**(在考虑要不要换手机、那笔钱
        回头再说、在等谁回复)——待办是"要去做的一件事",话头是"还没聊出结论的一段话",
        同一件事只记一边。"""
        clean = _clean_title(title)
        if clean is None:
            return f"标题要有字、不超过 {MAX_TITLE_CHARS} 字(细节放 note),这条没记。"
        due_day = None
        if due is not None and due.strip():
            parsed = _parse_due(due)
            if parsed is None:
                return (
                    f"看不懂到期日「{due}」:要 YYYY-MM-DD。"
                    f"相对说法先调 current_time 换算成日期再传。这条没记。"
                )
            due_day = parsed.isoformat()
        clean_course = _clean_course(course or "")
        if clean_course is not None and len(clean_course) > MAX_COURSE_CHARS:
            return f"课名不超过 {MAX_COURSE_CHARS} 字,这条没记。"
        clean_note = _clean_note(note or "")
        if clean_note is not None and len(clean_note) > MAX_NOTE_CHARS:
            return f"备注不超过 {MAX_NOTE_CHARS} 字,这条没记。"

        same = store.open_with_title(clean)
        todo_id = store.insert(
            title=clean, due=due_day, course=clean_course, note=clean_note, created_at=stamp()
        )
        saved = store.get(todo_id)
        if saved is None:  # 刚插进去的读不回来 = 库出了事,别跟用户说记好了
            return "这条没记进去(写完读不回来)。"
        note_part = f" · 备注{_quoted(clean_note)}" if clean_note else ""
        out = f"记下了 #{todo_id}:{_summary(saved)}{note_part}。{_past_warning(due_day, today())}"
        if same is not None:
            # 重复是删除的头号来由(用户说过「加错了、重复了」)。不拦——两份作业同名是正常的——
            # 但说出来,模型才有机会问一句。
            out += f"\n另有一条同名的 #{same.id} 也没完成;是同一件事的话,删掉其中一条。"
        return out

    def list_todos(
        due_before: str | None = None,
        course: str | None = None,
        include_done: bool = False,
        include_deleted: bool = False,
        page: int = 1,
    ) -> str:
        """列待办。默认只列没完成的;include_done=True 连已完成的一起列(标「已完成」),
        include_deleted=True 连删掉的一起列(标「已删除」)——两个开关各管一件事。
        按到期日从早到晚排,**没有到期日的排在最后**;过期还没完成的行首标「已过期 N 天」,
        当天到期的标「今天到期」;第一行给总数、其中几条过期、今天是几号。
        due_before=YYYY-MM-DD 只列那天及以前到期的(含过期没做完的,不含没有到期日的),
        「这周要交什么」就传这周日;course 按课筛,写法要和记的时候一致。
        每页最多 20 条,翻页换 page(越界会钳到有效页)。
        每行开头的 #id 喂给 todos__complete_todo / todos__update_todo / todos__delete_todo。"""
        before = None
        if due_before is not None:
            parsed = _parse_due(due_before)
            if parsed is None:
                return f"看不懂日期「{due_before}」:要 YYYY-MM-DD。相对说法先调 current_time 换算。"
            before = parsed.isoformat()
        wanted_course = _clean_course(course or "") if course is not None else None
        now = today()

        def fetch(offset: int) -> Listing:
            return store.listing(
                today=now.isoformat(),
                due_before=before,
                course=wanted_course,
                include_done=include_done,
                include_deleted=include_deleted,
                limit=MAX_TODO_ROWS,
                offset=offset,
            )

        # 页码钳到 [1, 总页数](0 / 负数 / 超大都不报错),同 search_history / list_threads。
        first = fetch(0)
        pages = max(1, -(-first.total // MAX_TODO_ROWS))
        page = min(max(1, page if isinstance(page, int) else 1), pages)
        listing = first if page == 1 else fetch((page - 1) * MAX_TODO_ROWS)

        if listing.total == 0:
            # "筛出来没有" ≠ "一条都没有" ≠ "都做完了":混成一句模型会以为待办是空的。
            hidden = [
                w for w, on in (("完成的", not include_done), ("删掉的", not include_deleted)) if on
            ]
            tail = ""
            if hidden:
                flags = " / ".join(
                    f
                    for f, on in (
                        ("include_done", not include_done),
                        ("include_deleted", not include_deleted),
                    )
                    if on
                )
                tail = f"{'、'.join(hidden)}不在这份名单里({flags} 才列)。"
            if wanted_course is not None:
                tail += "课名要和记的时候写的一字不差,不带 course 列一遍看看记成了什么。"
            return f"没有符合的待办。{tail}"
        head = _header(
            listing,
            today=now,
            due_before=before,
            course=wanted_course,
            include_done=include_done,
            include_deleted=include_deleted,
            page=page,
            pages=pages,
        )
        return "\n".join([head, *(_line(t, now) for t in listing.rows)])

    def complete_todo(todo_id: int, undo: bool = False) -> str:
        """勾掉一条待办:这件事做完了(交了、考完了、取回来了)。
        todo_id 是 todos__list_todos 每行开头的 #id。
        **勾错了能撤回**:同一个 id 再调一次、带 undo=True,回到没完成的名单里,别的一个字不变。
        **做完不是删掉**:本来就不该记的(加错了、重复了、不用做了)用 todos__delete_todo。"""
        with store.atomically():
            t = store.get(todo_id)
            if t is None:
                return _missing(todo_id)
            if t.deleted_at is not None:
                return (
                    f"#{todo_id}({_summary(t)})已经删了,不在名单上,勾不了。"
                    f"要拿回来先调 todos__delete_todo、带 undo=True。"
                )
            if undo:
                if t.done_at is None:
                    return f"#{todo_id}({_summary(t)})没勾过,本来就在没完成的名单里。"
                store.set_done(todo_id, None)
                return f"撤回了:#{todo_id}({_summary(t)})又回到没完成的名单里。"
            if t.done_at is not None:
                # 再勾一次不刷新完成时间:那个时间是"什么时候做完的",不是"最后一次说做完"。
                return f"#{todo_id}({_summary(t)})已经勾过了({t.done_at[:10]}),什么都没动。"
            store.set_done(todo_id, stamp())
        return (
            f"勾掉了 #{todo_id}:{_summary(t)}。"
            f"勾错了再调一次 todos__complete_todo、带 undo=True 就能撤回。"
        )

    def update_todo(
        todo_id: int,
        title: str | None = None,
        due: str | None = None,
        course: str | None = None,
        note: str | None = None,
    ) -> str:
        """改一条待办——「改到下周五」「其实是概率论的作业」。只传要改的字段,没传的原样保留;
        due / course / note 传空字符串 "" 是清掉(不定日子了、不属于哪门课了、备注不要了)。
        due 同样只认 YYYY-MM-DD,相对说法先调 current_time 换算。
        **就地改,#id 不变**;完成状态不受影响;删掉的改不了(先用 todos__delete_todo 带
        undo=True 拿回来)。"""
        if title is None and due is None and course is None and note is None:
            return "什么都没传,这条没改。只传要改的那几个字段。"
        new_title = None
        if title is not None:
            new_title = _clean_title(title)
            if new_title is None:
                return f"标题要有字、不超过 {MAX_TITLE_CHARS} 字(细节放 note),这条没改。"
        new_due = None
        if due is not None and due.strip():
            parsed = _parse_due(due)
            if parsed is None:
                return (
                    f"看不懂到期日「{due}」:要 YYYY-MM-DD。"
                    f"相对说法先调 current_time 换算成日期再传。这条没改。"
                )
            new_due = parsed.isoformat()
        new_course = _clean_course(course) if course is not None else None
        if new_course is not None and len(new_course) > MAX_COURSE_CHARS:
            return f"课名不超过 {MAX_COURSE_CHARS} 字,这条没改。"
        new_note = _clean_note(note) if note is not None else None
        if new_note is not None and len(new_note) > MAX_NOTE_CHARS:
            return f"备注不超过 {MAX_NOTE_CHARS} 字,这条没改。"

        # 校验全在动手之前(M5-15 栽过反过来的);读-判-写在一个事务里(M5-31)。
        with store.atomically():
            t = store.get(todo_id)
            if t is None:
                return _missing(todo_id)
            if t.deleted_at is not None:
                return (
                    f"#{todo_id}({_summary(t)})已经删了,改不了。"
                    f"要拿回来先调 todos__delete_todo、带 undo=True。"
                )
            store.overwrite_fields(
                todo_id,
                title=t.title if new_title is None else new_title,
                due=t.due if due is None else new_due,
                course=t.course if course is None else new_course,
                note=t.note if note is None else new_note,
            )
            after = store.get(todo_id)
        if after is None:
            return "这条没改成(写完读不回来)。"
        note_part = f" · 备注{_quoted(after.note)}" if after.note else ""
        return (
            f"改了 #{todo_id}:{_summary(t)} → {_summary(after)}{note_part}。"
            f"还是 #{todo_id},号没变。{_past_warning(after.due if due else None, today())}"
        )

    def delete_todo(todo_id: int, reason: str = "", undo: bool = False) -> str:
        """删掉一条本来就不该在的待办——加错了、重复了、不用做了。**删的时候 reason 必填**
        (不写就不删,会让你补一句)。
        **不是真删**:todos__list_todos(include_deleted=True) 还看得到。删错了同一个 id
        再调一次、带 undo=True,原样回来(连它完没完成都照旧)——**撤回不用给 reason**。
        **做完了不是删**:做完用 todos__complete_todo。"""
        with store.atomically():
            t = store.get(todo_id)
            if t is None:
                return _missing(todo_id)
            if undo:
                if t.deleted_at is None:
                    return f"#{todo_id}({_summary(t)})没删,现在就在名单上,不用恢复。"
                store.set_deleted(todo_id, None, None)
                return f"恢复了 #{todo_id}:{_summary(t)},原样回到名单上。"
            if t.deleted_at is not None:
                # 再删一次不许盖掉第一次的理由,更不该让模型以为"这次才生效"。
                return f"#{todo_id}({_summary(t)})已经删过了。要拿回来就带 undo=True。"
            if not (reason or "").strip():
                return (
                    f"删 #{todo_id}({_summary(t)})得说清为什么(reason),所以没删。"
                    f"「记重了」和「不用做了」不是一回事,回头看得出差别。"
                )
            store.set_deleted(todo_id, stamp(), reason.strip())
        return (
            f"删了 #{todo_id}:{_summary(t)} · 原因{_quoted(reason)}。"
            f"删错了再调一次 todos__delete_todo、带 undo=True 就能拿回来。"
        )

    return [add_todo, list_todos, complete_todo, update_todo, delete_todo]


def build(data_dir: Path, *, timezone: str) -> BundleRuntime:
    """统一构造入口(bundle 契约)。timezone 由组装根注入、不在这里兜默认值:
    「今天」和完成时间都按它算,默认值会和 `Settings.timezone` 各走各的(M1 Task 9)。"""
    return BundleRuntime(
        tools=_tool_functions(TodoStore(Path(data_dir) / "todos"), ZoneInfo(timezone))
    )


def create_server(data_dir: Path, *, timezone: str) -> FastMCP:
    """MCP 服务入口,同 finance;注册裸名(命名空间是 MCP 客户端的事,M6-6d)。"""
    mcp = FastMCP("todos")
    for fn in build(data_dir, timezone=timezone).tools:
        mcp.tool()(fn)
    return mcp


if __name__ == "__main__":
    import os

    # 两个默认值必须和 `Settings` 里同名变量的默认值一致(同 finance)。
    create_server(
        Path(os.environ.get("LARARIUM_DATA_DIR", "./data")),
        timezone=os.environ.get("LARARIUM_TIMEZONE", "Asia/Shanghai"),
    ).run()
