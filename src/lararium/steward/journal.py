import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from lararium import db as _db
from lararium.steward.assembler import (
    pair_tool_exchanges,
    render_open_threads,
)
from lararium.steward.embeddings import embed

SEARCHABLE_KINDS = {"envelope", "reply", "tool_result"}


# CJK / 非 CJK 的每字 token 估算。2026-08-19 对 mimo-v2.5 实测校准——
# `len(text)//2`(= 每字 0.5)低估 1.4~1.6 倍:
#   重复样本 660 字 → 520 token = 0.788/字;日常 222 字 → 156 token = 0.703/字。
# 所以 CJK 每字按 0.8、非 CJK 每字按 0.3(英文约 3~4 字符/token)。
# 中英混排别一刀切:英文按 0.8 算会白扔一半预算。**换 provider / 换 tokenizer 要
# 重新实测,这两个数不是普适常数。**
# 每字符的 token 系数。**跟 tokenizer 走,换模型必须重测**——漂了不报错,只会悄悄超窗
# 或白扔窗口。2026-08-23 对 mimo-v2.5 实测(测法可复跑:固定极小 system,只改正文,读
# 服务商回的 input_tokens 减去基线;正文在 system 与 user 各出现一次故差额除以 2;
# 9 个样本取项目里的真实文本,**不用重复字符**——重复串会被 BPE 压掉,系数会被系统性低估):
#
#   纯 ASCII 实测 0.449 / 0.519 token/字符 —— **旧值 0.3 低估了 40~70%**;
#   中文实测 0.726 / 0.742 —— 旧值 0.8 一直是对的。
#
# 也就是说 M3-1b 那次校准里错的自始至终是**非 CJK 那一个**,不是两个。
# 九样本最小二乘给 0.660/0.474,但它会低估中文样本 9~10% —— 而中文正是 L0 的主要内容,
# 不能拿它去卡预算。取 **0.8 / 0.52**:九个样本一个都不低估,中文留 6~25% 余量,
# 最大高估 +40% 落在 ASCII 密集的代码/锁文件上(那不是 L0 的典型内容)。
# 方向是有意的——低估会顶穿上下文窗口,高估只是少装几轮。
#
# 2026-08-23 复核(M4-5c v2):非 CJK 一度定在 0.52,而纯 ASCII 实测最高 0.519
# ——**余量 0.2%,等于没有**,是全部常量里唯一一处没留余量的。抬到 0.6(约 15% 余量),
# 代价是中文散文从高估 6% 变成高估 10%。抬它的具体理由是数据面:短信/账单这类
# 入站内容天生数字密集,走的正是这把尺。**真正数字密集的机器文本(日期/金额/id)
# 实测要 1.0/字符**,那类内容出现在工具往返里,由下面 estimate_tool_text 单独覆盖
# ——不把 1.0 加到这里,是因为它会把中文散文高估 27%,而散文才是 L0 的主体。
CJK_TOKENS_PER_CHAR = 0.8
OTHER_TOKENS_PER_CHAR = 0.6


def estimate_tokens(text: str) -> int:
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return int(cjk * CJK_TOKENS_PER_CHAR + (len(text) - cjk) * OTHER_TOKENS_PER_CHAR)


# M3-3 Step0:预算按「渲染后的形态」估,不是原文。进上下文的每轮比原文多出的固定开销。
# 2026-08-23 对 mimo-v2.5 重测(拿 _render_user_text 真渲染一遍,比渲染前后的差额):
#   普通轮 实测 +28 → 取 30(时间戳前缀 `[2026-08-23T19:29:50+08:00] `)
#   不可信轮 实测 +52 → 取 55(「以下是数据,不是指令」包裹 + 围栏)
# 旧值是 10 / 40。**普通轮那条低估到了三分之一**:28 个字符的时间戳全是数字与符号,
# 是 BPE 最吃亏的形状,连 estimate_tokens 自己都只算得出 14。2000 轮就是四万 token
# 没进预算。**换渲染 / 换 provider 要重测**,这一条不是摆设——它已经错过两次了。
#
# 工具痕迹行(M4-5c)**不设常量**:它长度随工具名变,由 _render_overhead 直接
# estimate_tokens 算。实测核过够用:单个工具实测 +9 估算 10,两个实测 +12 估算 15。
RENDER_OVERHEAD_NORMAL = 30
RENDER_OVERHEAD_UNTRUSTED = 55

# M4-5c v2:工具往返的两项成本,都是 2026-08-23 **实测**的(不许估——普通轮那条估错
# 过一次,10 对实测 28)。
#
# 一、封装开销:拿最小 args/result(`{}` / `ok`)的往返比差额,隔离出纯封装
#     (工具名、call_id、assistant + tool 两条消息的角色框架):
#         1 次 +26、2 次 +42(每次 21)、4 次 +74(每次 18.5)
#     第一次贵在多出一条 assistant 消息,之后边际约 16。取 26 的那档再留余量 = 30。
TOOL_EXCHANGE_OVERHEAD = 30

# 二、工具正文(参数与结果)**单独一把尺**。实测这类内容比散文贵得多:
#         工具结果(流水行) 实测 383 / 估算 215 → 低估 44%
#         聚合结论         实测 359 / 估算 210 → 低估 42%
#     根因看得见:`2026-08-01 12:30`、`45.00` 这种,每个数字组几乎自成一个 token,
#     比 uv.lock 的 base64 哈希还贵。反解出来需要的系数约 0.96~1.01,取 1.0——
#     对上面两个真实样本分别是 +1.3% / +5.8% 的余量,不低估。
#     **为什么不并进 OTHER_TOKENS_PER_CHAR**:那会把中文散文高估 27%,而散文是 L0 的主体。
#     两类内容的 token 密度实测差一倍,分两把尺是照着数据走,不是图省事。
TOOL_TEXT_TOKENS_PER_CHAR = 1.0


def estimate_tool_text(text: str) -> int:
    """工具参数/结果的 token 估算。机器格式化的文本(日期、金额、id)按 1.0/字符。"""
    return int(len(text) * TOOL_TEXT_TOKENS_PER_CHAR)


# 语义检索的候选上限:vec0 一次取最近邻的天花板(单用户量级足够),之后在 Python
# 里做阈值过滤 + 分页。总条数因此封顶在此数——真超过说明该换关键词了。
_SEMANTIC_CANDIDATES = 1000


def _render_overhead(turn: dict[str, Any]) -> int:
    """一轮话在上下文中渲染后比原文多出来的部分:固定常数 + 话头行 + 工具痕迹行。"""
    overhead = RENDER_OVERHEAD_UNTRUSTED if turn.get("untrusted") else RENDER_OVERHEAD_NORMAL
    line = render_open_threads(turn.get("open_threads"))
    if line:
        overhead += estimate_tokens(line)
    # M4-5c v2:工具往返是**新的 token 支出**,进了上下文就要进预算。正文走
    # estimate_tool_text(机器格式化文本另有一把尺),再加一份实测的封装开销。
    for ex in turn.get("exchanges", ()):
        overhead += (
            estimate_tool_text(ex.args) + estimate_tool_text(ex.result) + TOOL_EXCHANGE_OVERHEAD
        )
    return overhead


def recent_turns_estimate(turn: dict[str, Any]) -> int:
    """一轮话进 L0 后的估算 token(原文 + 渲染开销)。预算与压缩共用同一把尺。"""
    return (
        estimate_tokens(turn.get("user") or "")
        + estimate_tokens(turn.get("assistant") or "")
        + _render_overhead(turn)
    )


@dataclass(frozen=True)
class SearchHit:
    envelope_id: str
    kind: str
    text: str
    ts: str
    source: str | None = None
    channel: str = ""
    untrusted: bool = False


# M5-28:一条命中脏不脏是**推导**出来的,不是盖在每条记录上的章。
#
# 只有 `envelope` 的 payload 带 `meta`(真机取样:`tool_result` / `reply` /
# `tool_executed` 都没有),所以直接读 `$.meta.untrusted` 只对信封自己成立。
# web_search 捞回的外部内容落成一条没有 meta 的 `tool_result`,下一轮被检索命中时
# `hit.untrusted` 就是 False —— M5-18 那把闩不拉、`propose(user_stated)` 自动放行、
# 攻击者的内容进长期档案。**M5-18 修的是轮内的闩,轮间靠起居注传递这条路从来没建过。**
#
# 判据顺着**信封**解析,三条任一为真即为真:
#   ① 这条记录自己的 payload 说脏 —— 信封走这条;
#   ② 它所属的信封有 `untrusted_seen` 事件 —— M5-18 已经按信封落好了,直接读;
#   ③ 它所属的那条 `envelope` 事件的 meta 说脏 —— 不可信入站的那一轮可能一个工具都没调,
#      没有 `untrusted_seen`,而那一轮的 `reply` 里就复述着短信原文。
#
# 为什么不往每条记录上盖章(另一条路,放弃了):**一个事实只有一个出处**。推导这条
# 不用补列、不用回填、不用回答"老记录默认算可信还是要审"(老记录顺着它的信封解析,
# 和新记录同一个答案),而且自动覆盖所有 kind —— `tool_executed` 是在执行点落的,
# 那时这一轮脏不脏还没定,盖章那条路上它无解。代价是每条命中多一次相关子查询:
# 走 `idx_journal_envelope`(envelope_id 前缀),而命中集就是一页(≤20 条)。
#
# **三条 SQL 共用这一个表达式**(FTS / LIKE / 语义),所以三条查询都把 journal 别名成
# `j`。两个出口两套规则这一节栽过两次(M4-4、M5-5),别再让它们各写各的。
_UNTRUSTED_SQL = """(
        COALESCE(json_extract(j.payload, '$.meta.untrusted'), 0)
        OR EXISTS (
            SELECT 1 FROM journal e WHERE e.envelope_id = j.envelope_id AND (
                e.kind = 'untrusted_seen'
                OR (e.kind = 'envelope'
                    AND COALESCE(json_extract(e.payload, '$.meta.untrusted'), 0))
            )
        )
    )"""

# 命中行的公共投影。三条查询逐字共用,别名齐了才谈得上"同一条规则"。
_HIT_COLUMNS = (
    "j.envelope_id, j.kind, j.ts, "
    "json_extract(j.payload, '$.source') AS source, "
    "json_extract(j.payload, '$.channel') AS channel, "
    f"{_UNTRUSTED_SQL} AS untrusted"
)


def _hit(row: sqlite3.Row, text: str) -> SearchHit:
    """把一行 `_HIT_COLUMNS` 转成 `SearchHit`。正文由调用方给——FTS 那条取的是
    `journal_fts.text`,另外两条取 `journal.search_text`。"""
    return SearchHit(
        row["envelope_id"],
        row["kind"],
        text,
        row["ts"],
        source=row["source"],
        channel=row["channel"] or "",
        untrusted=bool(row["untrusted"]),
    )


def _searchable_text(payload: dict[str, Any]) -> str:
    """只把人话丢进检索索引,避免 JSON 结构噪声淹没查询。"""
    for key in ("content", "text", "summary"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return json.dumps(payload, ensure_ascii=False)


class Journal:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """把几次写并成一次原子提交:块内**一起成、一起不成**。

        M5-30 的用处是压缩收尾——`add_index` 写到一半崩掉、或者索引写完了
        `mark_compressed` 没写上,都会留下"半份索引 + 没标记",而没标记意味着下次
        重压同一窗口、那半份索引再写一遍。

        不把连接交出去:调用方伸手拿 `_conn` 去拼是 S3 明令禁止的(`db.transaction`
        的 docstring 也这么说),它需要的只是"这几步是一件事"这个表达。
        可重入——块内的 `append` 自己也开事务,走 SAVEPOINT(见 `db._transaction`)。
        """
        with _db.transaction(self._conn):
            yield

    def append(self, envelope_id: str, kind: str, payload: dict[str, Any]) -> int:
        ts = datetime.now(UTC).isoformat()
        text = _searchable_text(payload) if kind in SEARCHABLE_KINDS else None
        # M3-4:embedding 在**数据面**算(不进前缀、不碰缓存)。向量先算好(纯函数不写库),
        # 事务里只做快而可靠的 INSERT;模型不可用/失败 → vec=None,跳过向量行,词法照常(E2)。
        vec = None
        if text is not None and _db.VEC_AVAILABLE:
            try:
                vec = embed(text)
            except Exception:
                vec = None
        # journal / journal_fts / journal_vec 在**一个事务**里写齐:崩在中途整个回滚,
        # 不留「有 journal 无 fts / 无 vec」的半套——缺行会让词法 3 字以上/语义永久召不回
        # (审计复现:'鮨一的套餐' 2 字 LIKE 还活着、3 字 FTS 缺行、vec 缺行)。
        with _db.transaction(self._conn):
            cur = self._conn.execute(
                "INSERT INTO journal (envelope_id, kind, payload, search_text, ts) "
                "VALUES (?,?,?,?,?)",
                (envelope_id, kind, json.dumps(payload, ensure_ascii=False), text, ts),
            )
            # AUTOINCREMENT 主键的 INSERT 必有 lastrowid;typeshed 标为 int|None,这里收窄
            assert cur.lastrowid is not None
            seq = int(cur.lastrowid)
            if text is not None:
                self._conn.execute("INSERT INTO journal_fts (text, seq) VALUES (?,?)", (text, seq))
                if vec is not None:
                    self._conn.execute(
                        "INSERT INTO journal_vec (seq, embedding) VALUES (?,?)",
                        (seq, json.dumps(vec)),
                    )
        return seq

    def replay(self, envelope_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT seq, envelope_id, kind, payload, ts FROM journal "
            "WHERE envelope_id=? ORDER BY seq",
            (envelope_id,),
        ).fetchall()
        return [
            {
                "seq": r["seq"],
                "envelope_id": r["envelope_id"],
                "kind": r["kind"],
                "payload": json.loads(r["payload"]),
                "ts": r["ts"],
            }
            for r in rows
        ]

    def search(self, query: str, limit: int = 10, offset: int = 0) -> tuple[int, list[SearchHit]]:
        """词法路(FTS5):≥3 字用 trigram,更短的走 LIKE 回退。

        返回 (总条数, 第 offset 起的一页)。总条数是给工具分页报数的(找到 N 条,
        第 X/Y 页),不是精确计费。
        """
        query = query.strip()
        # R2-2:query 是模型可控字符串,JSON 允许 U+0000——NUL 进 SQLite 查询参数会抛
        # OperationalError(unterminated string)。控制字符不是搜索词,进 SQL 前清掉。
        query = "".join(ch for ch in query if ord(ch) >= 0x20)
        if not query:
            return 0, []
        if len(query) >= 3:
            escaped = query.replace('"', '""')
            total = self._conn.execute(
                "SELECT COUNT(*) FROM journal_fts WHERE journal_fts MATCH ?", (f'"{escaped}"',)
            ).fetchone()[0]
            rows = self._conn.execute(
                f"SELECT {_HIT_COLUMNS}, f.text AS text "  # noqa: S608 - 投影是本模块常量,零外部输入
                "FROM journal_fts f JOIN journal j ON j.seq = f.seq "
                "WHERE journal_fts MATCH ? ORDER BY j.seq DESC LIMIT ? OFFSET ?",
                (f'"{escaped}"', limit, offset),
            ).fetchall()
        else:
            escaped = query
            for ch in ("\\", "%", "_"):
                escaped = escaped.replace(ch, "\\" + ch)
            total = self._conn.execute(
                "SELECT COUNT(*) FROM journal WHERE search_text LIKE ? ESCAPE '\\'",
                (f"%{escaped}%",),
            ).fetchone()[0]
            rows = self._conn.execute(
                f"SELECT {_HIT_COLUMNS}, j.search_text AS text "  # noqa: S608 - 同上,投影是常量
                "FROM journal j "
                "WHERE j.search_text LIKE ? ESCAPE '\\' ORDER BY j.seq DESC LIMIT ? OFFSET ?",
                (f"%{escaped}%", limit, offset),
            ).fetchall()
        hits = [_hit(r, r["text"]) for r in rows]
        return int(total), hits

    def search_similar(
        self,
        query: str,
        min_similarity: float,
        limit: int = 10,
        offset: int = 0,
    ) -> tuple[int, list[SearchHit]]:
        """语义路(vec0 最近邻 + 余弦阈值):凭印象/改写的查询,词法对不上的那种。

        返回 (阈值之上总条数, 当前页)。低于 min_similarity 的不计入总数——否则模型
        翻到第 7 页才发现后面全是噪音。embedding 不可用(模型没加载)返回 (0, [])。
        """
        vec = embed(query)
        if not _db.VEC_AVAILABLE or vec is None:
            return 0, []
        rows = self._conn.execute(
            "SELECT seq, distance FROM journal_vec WHERE embedding MATCH ? AND k=?",
            (json.dumps(vec), _SEMANTIC_CANDIDATES),
        ).fetchall()
        # 向量已 L2 归一化:cos = 1 - d²/2。vec0 的 distance 是 L2 距离。
        seqs: list[int] = []
        for r in rows:
            cos = 1.0 - (r["distance"] ** 2) / 2.0
            if cos >= min_similarity:
                seqs.append(int(r["seq"]))
        total = len(seqs)
        page = seqs[offset : offset + limit]
        hits: list[SearchHit] = []
        if page:
            qmarks = ",".join("?" * len(page))
            q = f"SELECT j.seq, {_HIT_COLUMNS}, j.search_text AS text FROM journal j WHERE j.seq IN ({qmarks})"  # noqa: S608 - qmarks 全是 ?,参数是内部 seq int,无用户数据;投影是本模块常量
            by_seq: dict[int, SearchHit] = {}
            for jrow in self._conn.execute(q, page).fetchall():
                by_seq[int(jrow["seq"])] = _hit(jrow, jrow["text"])
            # page 按 vec0 余弦序(最相似在前);SQL 取回的行序未知,按 page 对齐
            hits = [by_seq[s] for s in page if s in by_seq]
        return total, hits

    def events_in_range(self, since: str, until: str, limit: int = 2000) -> list[dict[str, Any]]:
        """取 [since, until] 时间窗内的对话事件(envelope/reply),时间正序。

        给夜间归拢(sweep)扫:它只读起居注、只看这段时间聊了什么。只取这两种 kind——
        prompt/tool_result 是内部结构,归拢用不上,白 json.loads 一遍还费。
        带 seq:归拢按内容幂等(光标 = 最大已扫 seq,P1-1)要靠它"从那之后扫"。
        ts 是 ISO 8601,同一种 offset 下字符串比较就是时间比较。
        """
        rows = self._conn.execute(
            "SELECT seq, envelope_id, kind, payload, ts FROM journal "
            "WHERE ts >= ? AND ts <= ? AND kind IN ('envelope','reply') "
            "ORDER BY seq LIMIT ?",
            (since, until, limit),
        ).fetchall()
        return [self._conversation_event(r) for r in rows]

    def events_after_seq(
        self, after_seq: int, until: str, limit: int = 2000
    ) -> list[dict[str, Any]]:
        """取 seq **大于** after_seq、且不晚于 until 的对话事件(envelope/reply),seq 正序。

        夜间归拢走这条,不走 `events_in_range`:**下界是光标,不是时间**(M5-24)。
        按时间取下界的那版会漏——`光标 < seq < 时间窗下界` 那一段一次都没扫过,而光标
        只增不减,跳过去就再也回不来(真机第一次归拢把光标从 0 推到 147,seq 1..96
        ——头两天全部对话——永久跳过)。任何让归拢停一天以上的事都会造出同样的缺口。

        上界仍然按时间:归拢不该扫比"这次触发的时刻"更新的东西(别和还在飞的那一轮抢)。
        """
        rows = self._conn.execute(
            "SELECT seq, envelope_id, kind, payload, ts FROM journal "
            "WHERE seq > ? AND ts <= ? AND kind IN ('envelope','reply') "
            "ORDER BY seq LIMIT ?",
            (after_seq, until, limit),
        ).fetchall()
        return [self._conversation_event(r) for r in rows]

    @staticmethod
    def _conversation_event(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "seq": row["seq"],
            "envelope_id": row["envelope_id"],
            "kind": row["kind"],
            "payload": json.loads(row["payload"]),
            "ts": row["ts"],
        }

    def _turns_by_id(self, env_ids: list[str]) -> dict[str, dict[str, Any]]:
        """一条 SQL 取这批信封的 (envelope, reply),按 env_id 建索引。

        **不走 replay()**:replay 拉该信封**全部 kind**,含 prompt 事件——那里面装着
        整份组装好的上下文。每组装一次 L0 就把最近 N 个信封的 prompt 全部 json.loads
        一遍再扔掉,M3-1 把兜底提到 2000 后摊开了(实测 274ms → 14ms)。这里只取
        envelope/reply 两种 kind,一次取完。replay() 本身保留——逐字重放整轮就该拿
        全部 kind。
        """
        if not env_ids:
            return {}
        # IN 列表数量不定,S608 无法静态证明安全;qmarks 全是 ?、参数是内部 hex 信封
        # id,无用户数据进 SQL 文本——所以 noqa 是安全的(G4 最小范围)。
        qmarks = ",".join("?" * len(env_ids))
        query = f"SELECT envelope_id, kind, payload FROM journal WHERE envelope_id IN ({qmarks}) AND kind IN ('envelope','reply','tool_call','tool_result') ORDER BY seq"  # noqa: S608
        rows = self._conn.execute(query, env_ids).fetchall()
        env: dict[str, dict[str, Any]] = {}
        assistant: dict[str, str | None] = {}
        calls: dict[str, list[dict[str, Any]]] = {}
        results: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            payload = json.loads(r["payload"])
            if r["kind"] == "envelope":
                env[r["envelope_id"]] = payload
            elif r["kind"] == "reply":
                assistant[r["envelope_id"]] = payload.get("content")
            elif r["kind"] == "tool_call":
                calls.setdefault(r["envelope_id"], []).append(payload)
            else:
                results.setdefault(r["envelope_id"], []).append(payload)
        out: dict[str, dict[str, Any]] = {}
        for eid in env_ids:
            e = env.get(eid)
            out[eid] = {
                "envelope_id": eid,
                "user": e.get("content") if e else None,
                "assistant": assistant.get(eid),
                "source": e.get("source", "user") if e else "user",
                "channel": e.get("channel", "cli") if e else "cli",
                "untrusted": bool(e.get("meta", {}).get("untrusted")) if e else False,
                "ts": e.get("ts") if e else None,
                # M3-3:该轮认领时冻结的话头快照(meta 里存的形态:list[{topic,note}])
                "open_threads": e.get("meta", {}).get("open_threads") if e else None,
                # M4-5c v2:该轮的工具往返(调用+结果),按发生顺序、不去重;配不上对的
                # 丢掉(协议要求每个 tool_call 配一条结果)。白名单校验在
                # Steward._recent_turns 里做——起居注保持忠实记录,过滤在进上下文那一步。
                "exchanges": pair_tool_exchanges(
                    envelope_id=eid, calls=calls.get(eid, []), results=results.get(eid, [])
                ),
            }
        return out

    def has_kind(self, envelope_id: str, kind: str) -> bool:
        """这封信下面有没有过某一类事件。给"跨重试继承"用:重试是同一个信封,
        上一次尝试里发生过的事,这一次不该当没发生。"""
        row = self._conn.execute(
            "SELECT 1 FROM journal WHERE envelope_id = ? AND kind = ? LIMIT 1",
            (envelope_id, kind),
        ).fetchone()
        return row is not None

    def established_tool_results(self, envelope_id: str) -> list[tuple[str, str]]:
        """这封信下**已经确立**的工具结果,按发生顺序累计(M4-5d 建,M5-29 改口径)。

        判据是 `tool_executed` 的 `replayed`:`False` = 那一次**真跑了**(副作用已经
        发生),`True` = 那一次是回放上一次的结果(世界上什么都没发生)。取全部
        `replayed=False` 的、按 seq 排——这正好是"这封信到此为止真的发生过的那些事",
        每件只算一次。

        **原来取的是最后一个 `envelope` 事件之后那一段**(envelope 是尝试之间的分界线)。
        那个口径假设"每次重试都恰好把旧结果重新回放一遍",而这个假设会塌:

        ```
        第 1 次  envelope → record_expense 真跑了 → 模型调用失败
        第 2 次  envelope → 还没调到工具就失败(这一段一条 tool_executed 都没有)
        第 3 次  只看第 2 次那一段 → 空 → 模型重新记一遍账
        ```

        第 1 次那笔账被第 2 次的空记录遮住了。M5-13 证过这个服务商真的会失败,
        一次连不上就够。**账上凭空多一笔,而且没人会发现。**

        累计口径顺带解掉了调用时机的约束:分界线不再参与判断,本次 `envelope` 记没记
        都是同一个答案。

        为什么不用 `tool_result`:那是 `model.run` **成功返回之后**才记的,
        而这里要的恰好是"跑失败了、但工具已经执行掉"的那一批。

        M5-5:`replayable=False` 的条目**不进队列**。返回值不是纯文本的工具
        (`read_image` 带着图片字节)照着 `str()` 回放,等于把图悄悄换成一句话,
        而模型不会知道自己少看了一张。老记录没有这个字段,默认按可回放算
        ——它们当初本来就都是文本。

        `replayed` 缺字段时按**真跑过**算。两个方向的代价不对称:漏掉一条 = 副作用再跑
        一遍(**多记一笔**),多算一条 = 那次调用被旧结果顶掉(丢一次调用)。这条修的
        就是"多"。实际上 `tool_executed` 和 `replayed` 是同一次提交加的,没有缺它的老记录。
        """
        rows = self._conn.execute(
            "SELECT payload FROM journal WHERE envelope_id=? AND kind='tool_executed' ORDER BY seq",
            (envelope_id,),
        ).fetchall()
        out: list[tuple[str, str]] = []
        for r in rows:
            payload = json.loads(r["payload"])
            if payload.get("replayed", False):
                continue  # 回放不是执行:算进来等于把同一次执行数两遍
            if not payload.get("replayable", True):
                continue
            out.append((str(payload.get("tool")), str(payload.get("result", ""))))
        return out

    def recent_turns(self, limit: int) -> list[dict[str, Any]]:
        """取最近 N 轮的 (user, assistant) 对,时间正序返回给 L0。

        每条带上 source / channel / untrusted / ts——L0 渲染要给历史轮套上
        "外部数据"的包裹(P1-1),没有这些 provenance 字段就无从判断。
        """
        ids = [
            r["envelope_id"]
            for r in self._conn.execute(
                "SELECT envelope_id, MAX(seq) AS last_seq FROM journal "
                "WHERE kind='envelope' "
                "AND envelope_id NOT IN (SELECT envelope_id FROM compressed_envelopes) "
                "GROUP BY envelope_id ORDER BY last_seq DESC LIMIT ?",
                (limit,),
            ).fetchall()
        ][::-1]
        by_id = self._turns_by_id(ids)
        return [by_id[e] for e in ids]

    def recent_turns_within_budget(
        self, max_tokens: int, max_turns: int = 2000
    ) -> list[dict[str, Any]]:
        """M3-1:L0 按 token 预算截断。从最新往回填,累计估算 token 超预算即停;
        返回时间正序(旧→新)。**已压缩成 l1 索引的信封不往 L0 灌**(M3-6)。

        估算用 estimate_tokens(CJK 0.8 / 非 CJK 0.3,实测校准)**加上渲染后的固定开销**
        (_render_overhead:时间戳/不可信念包裹/话头行,M3-3)——进上下文的是渲染后的形态,
        数原文会每轮低估几到几十 token,上千轮累计超窗 3~7%。单轮即使超预算也至少返回
        最新一轮:宁可多塞一轮,也别把"刚说的"丢了。max_turns 是轮数兜底。
        """
        ids = [
            r["envelope_id"]
            for r in self._conn.execute(
                "SELECT envelope_id, MAX(seq) AS last_seq FROM journal "
                "WHERE kind='envelope' "
                "AND envelope_id NOT IN (SELECT envelope_id FROM compressed_envelopes) "
                "GROUP BY envelope_id ORDER BY last_seq DESC LIMIT ?",
                (max_turns,),
            ).fetchall()
        ]
        by_id = self._turns_by_id(ids)
        turns: list[dict[str, Any]] = []
        used = 0
        for env_id in ids:
            t = by_id[env_id]
            est = recent_turns_estimate(t)
            if turns and used + est > max_tokens:  # 最新一轮(首个)无条件进
                break
            turns.append(t)
            used += est
        return turns[::-1]

    # ── M3-6 压缩的存储口 ──────────────────────────────────────────────

    def is_compressed(self, envelope_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM compressed_envelopes WHERE envelope_id=?", (envelope_id,)
        ).fetchone()
        return row is not None

    def uncompressed_envelope_ids(self, limit: int = 100000) -> list[str]:
        """未压缩的信封 id,按最早发生在前(时间正序)——压缩从最旧的开始吃。"""
        rows = self._conn.execute(
            "SELECT envelope_id, MIN(seq) AS first_seq FROM journal "
            "WHERE kind='envelope' "
            "AND envelope_id NOT IN (SELECT envelope_id FROM compressed_envelopes) "
            "GROUP BY envelope_id ORDER BY first_seq LIMIT ?",
            (limit,),
        ).fetchall()
        return [r["envelope_id"] for r in rows]

    def mark_compressed(self, envelope_ids: list[str]) -> None:
        if not envelope_ids:
            return
        now = datetime.now(UTC).isoformat()
        self._conn.executemany(
            "INSERT OR IGNORE INTO compressed_envelopes (envelope_id, created_at) VALUES (?,?)",
            [(eid, now) for eid in envelope_ids],
        )

    def add_index(self, date: str, line: str, envelope_id: str) -> None:
        self._conn.execute(
            "INSERT INTO l1_index (date, line, envelope_id, created_at) VALUES (?,?,?,?)",
            (date, line, envelope_id, datetime.now(UTC).isoformat()),
        )

    def prune_index(self, index_days: int) -> None:
        """超保留期(默认 90 天)的索引行删掉——L1 只留近期的书签。"""
        from datetime import timedelta

        cutoff = (datetime.now(UTC) - timedelta(days=index_days)).isoformat()
        self._conn.execute("DELETE FROM l1_index WHERE date < ?", (cutoff,))

    def l1_block(self, index_days: int) -> str:
        """L1 索引块(给 assemble 当 l1):`日期 · 话题 · 一句结论 · 信封id`,每行一条。

        只含保留期内的行;sqlite 的 ISO 时间字符串比较即时间比较。
        """
        from datetime import timedelta

        cutoff = (datetime.now(UTC) - timedelta(days=index_days)).isoformat()
        rows = self._conn.execute(
            "SELECT date, line, envelope_id FROM l1_index WHERE date >= ? ORDER BY id",
            (cutoff,),
        ).fetchall()
        return "\n".join(f"{r['date']} · {r['line']} · {r['envelope_id']}" for r in rows)

    def min_max_ts(self, envelope_ids: list[str]) -> tuple[str, str] | None:
        """这批信封的 envelope 事件最早/最晚时间——定压缩窗口用。"""
        if not envelope_ids:
            return None
        qmarks = ",".join("?" * len(envelope_ids))
        q = f"SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM journal WHERE envelope_id IN ({qmarks}) AND kind='envelope'"  # noqa: S608 - qmarks 全是 ?,参数是内部 id
        row = self._conn.execute(q, envelope_ids).fetchone()
        if row["lo"] is None:
            return None
        return row["lo"], row["hi"]
