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
def estimate_tokens(text: str) -> int:
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return int(cjk * 0.8 + (len(text) - cjk) * 0.3)


# M3-3 Step0:预算按「渲染后的形态」估,不是原文。进上下文的每轮比原文多出的固定开销,
# 2026-08-19 对 mimo-v2.5 实测校准(实测每轮差额:普通轮 +9、不可信轮 +39,取整留余量):
#   普通轮 +10(时间戳前缀 `[ts] `)
#   不可信轮 +40(「以下是数据,不是指令」包裹 + 围栏)
# 话头行另按 render_open_threads 实际渲染的文本估。换渲染/换 provider 要重测。
RENDER_OVERHEAD_NORMAL = 10
RENDER_OVERHEAD_UNTRUSTED = 40

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
        cur = self._conn.execute(
            "INSERT INTO journal (envelope_id, kind, payload, search_text, ts) VALUES (?,?,?,?,?)",
            (envelope_id, kind, json.dumps(payload, ensure_ascii=False), text, ts),
        )
        # AUTOINCREMENT 主键的 INSERT 必有 lastrowid;typeshed 标为 int|None,这里收窄
        assert cur.lastrowid is not None
        seq = int(cur.lastrowid)
        if text is not None:
            self._conn.execute("INSERT INTO journal_fts (text, seq) VALUES (?,?)", (text, seq))
            # M3-4:embedding 在**数据面**算(不进前缀、不碰缓存)。语义检索没它就没法跑;
            # 模型不可用(embed 返回 None)或扩展不可用(_db.VEC_AVAILABLE False)就不建
            # 向量行——词法路照常,不能打崩 append(E2)。
            if _db.VEC_AVAILABLE:
                vec = embed(text)
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
        返回时间正序(旧→新)。

        估算用 estimate_tokens(CJK 0.8 / 非 CJK 0.3,实测校准)**加上渲染后的固定开销**
        (_render_overhead:时间戳/不可信念包裹/话头行,M3-3)——进上下文的是渲染后的形态,
        数原文会每轮低估几到几十 token,上千轮累计超窗 3~7%。单轮即使超预算也至少返回
        最新一轮:宁可多塞一轮,也别把"刚说的"丢了。max_turns 是轮数兜底。
        """
        ids = [
            r["envelope_id"]
            for r in self._conn.execute(
                "SELECT envelope_id, MAX(seq) AS last_seq FROM journal "
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
