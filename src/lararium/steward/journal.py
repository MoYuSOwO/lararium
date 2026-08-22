import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from lararium import db as _db
from lararium.steward.assembler import render_open_threads
from lararium.steward.embeddings import embed

SEARCHABLE_KINDS = {"envelope", "reply", "tool_result"}


# CJK / 非 CJK 的每字 token 估算。2026-08-19 对 mimo-v2.5 实测校准——
# `len(text)//2`(= 每字 0.5)低估 1.4~1.6 倍:
#   重复样本 660 字 → 520 token = 0.788/字;日常 222 字 → 156 token = 0.703/字。
# 所以 CJK 每字按 0.8、非 CJK 每字按 0.3(英文约 3~4 字符/token)。
# 中英混排别一刀切:英文按 0.8 算会白扔一半预算。**换 provider / 换 tokenizer 要
# 重新实测,这两个数不是普适常数。**
# 每字符的 token 系数。**跟 tokenizer 走,换模型必须重测**——漂了不报错,只会悄悄超窗
# 或白扔窗口。2026-08-22 对 deepseek-v4-flash-vision-exp 重测(测法:固定极小 system,
# 只改正文,读服务商回的 input_tokens,减去基线;样本取项目里的真实文本,不用重复字符
# ——重复串会被 BPE 压掉,系数会被系统性低估):
#   纯 ASCII 实测 0.39~0.43 token/字符,而旧值 0.3 **低估到 30%**;
#   中文实测 ~0.66~0.72,旧值 0.8 略高估。
# 九样本最小二乘给 0.659 / 0.407,但那组系数会低估其中三个样本。这里取
# **0.75 / 0.45**:九个样本一个都不低估(最大高估 +27.5%,落在 ASCII 密集的代码上)。
# 方向是有意的——这个估算撑着 L0 预算与压缩水位,低估会顶穿上下文窗口(M3-1b 修的
# 正是低估),高估只是少装几轮。
CJK_TOKENS_PER_CHAR = 0.75
OTHER_TOKENS_PER_CHAR = 0.45


def estimate_tokens(text: str) -> int:
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return int(cjk * CJK_TOKENS_PER_CHAR + (len(text) - cjk) * OTHER_TOKENS_PER_CHAR)


# M3-3 Step0:预算按「渲染后的形态」估,不是原文。进上下文的每轮比原文多出的固定开销。
# 2026-08-22 对 deepseek-v4-flash-vision-exp 重测(拿 _render_user_text 真渲染一遍,
# 比渲染前后的 input_tokens 差额):普通轮 +19、不可信轮 +42,取整留余量。
#   普通轮 +20(时间戳前缀 `[ts] `——28 个 ASCII 字符,新 tokenizer 下比旧的贵一倍)
#   不可信轮 +45(「以下是数据,不是指令」包裹 + 围栏)
# 旧值(mimo-v2.5:+9 / +39)对应 10 / 40;普通轮那条**低估了一半**,2000 轮就是
# 一万八千 token 没算进预算。话头行另按 render_open_threads 实际渲染的文本估。
# **换渲染 / 换 provider 要重测**,这一条不是摆设。
RENDER_OVERHEAD_NORMAL = 20
RENDER_OVERHEAD_UNTRUSTED = 45

# 语义检索的候选上限:vec0 一次取最近邻的天花板(单用户量级足够),之后在 Python
# 里做阈值过滤 + 分页。总条数因此封顶在此数——真超过说明该换关键词了。
_SEMANTIC_CANDIDATES = 1000


def _render_overhead(turn: dict[str, Any]) -> int:
    """一轮话在上下文中渲染后比原文多出来的部分:固定常数 + 话头行。"""
    overhead = RENDER_OVERHEAD_UNTRUSTED if turn.get("untrusted") else RENDER_OVERHEAD_NORMAL
    line = render_open_threads(turn.get("open_threads"))
    if line:
        overhead += estimate_tokens(line)
    return overhead


@dataclass(frozen=True)
class SearchHit:
    envelope_id: str
    kind: str
    text: str
    ts: str
    source: str | None = None
    channel: str = ""
    untrusted: bool = False


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
                "SELECT j.envelope_id, j.kind, f.text AS text, j.ts, "
                "json_extract(j.payload, '$.source') AS source, "
                "json_extract(j.payload, '$.channel') AS channel, "
                "json_extract(j.payload, '$.meta.untrusted') AS untrusted "
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
                "SELECT envelope_id, kind, search_text AS text, ts, "
                "json_extract(payload, '$.source') AS source, "
                "json_extract(payload, '$.channel') AS channel, "
                "json_extract(payload, '$.meta.untrusted') AS untrusted "
                "FROM journal "
                "WHERE search_text LIKE ? ESCAPE '\\' ORDER BY seq DESC LIMIT ? OFFSET ?",
                (f"%{escaped}%", limit, offset),
            ).fetchall()
        hits = [
            SearchHit(
                r["envelope_id"],
                r["kind"],
                r["text"],
                r["ts"],
                source=r["source"],
                channel=r["channel"] or "",
                untrusted=bool(r["untrusted"]),
            )
            for r in rows
        ]
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
            q = f"SELECT seq, envelope_id, kind, search_text AS text, ts, json_extract(payload, '$.source') AS source, json_extract(payload, '$.channel') AS channel, json_extract(payload, '$.meta.untrusted') AS untrusted FROM journal WHERE seq IN ({qmarks})"  # noqa: S608 - qmarks 全是 ?,参数是内部 seq int,无用户数据
            by_seq: dict[int, SearchHit] = {}
            for jrow in self._conn.execute(q, page).fetchall():
                by_seq[int(jrow["seq"])] = SearchHit(
                    jrow["envelope_id"],
                    jrow["kind"],
                    jrow["text"],
                    jrow["ts"],
                    source=jrow["source"],
                    channel=jrow["channel"] or "",
                    untrusted=bool(jrow["untrusted"]),
                )
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
        query = f"SELECT envelope_id, kind, payload FROM journal WHERE envelope_id IN ({qmarks}) AND kind IN ('envelope','reply') ORDER BY seq"  # noqa: S608
        rows = self._conn.execute(query, env_ids).fetchall()
        env: dict[str, dict[str, Any]] = {}
        assistant: dict[str, str | None] = {}
        for r in rows:
            payload = json.loads(r["payload"])
            if r["kind"] == "envelope":
                env[r["envelope_id"]] = payload
            else:
                assistant[r["envelope_id"]] = payload.get("content")
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
            }
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
            est = (
                estimate_tokens(t["user"] or "")
                + estimate_tokens(t["assistant"] or "")
                + _render_overhead(t)
            )
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
