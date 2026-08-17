import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from bundles.memory.ledger import LEDGER_SECTIONS, Ledger

Kind = Literal["add", "amend", "retire"]
Provenance = Literal["user_stated", "untrusted"]


@dataclass(frozen=True)
class Proposal:
    id: str
    kind: str
    section: str | None
    content: str
    old_text: str | None
    provenance: str
    origin: str
    state: str
    note: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_proposal(r: sqlite3.Row) -> Proposal:
    return Proposal(
        r["id"],
        r["kind"],
        r["section"],
        r["content"],
        r["old_text"],
        r["provenance"],
        r["origin"],
        r["state"],
        r["note"],
    )


def _insert_under_section(content: str, section: str, line: str) -> str:
    """把一行插到指定小节末尾。小节不存在就追加到文末。"""
    marker = f"## {section}"
    if marker not in content:
        return content.rstrip("\n") + f"\n\n{marker}\n- {line}\n"
    head, _, tail = content.partition(marker)
    rest_lines = tail.split("\n")
    insert_at = len(rest_lines)
    for i, ln in enumerate(rest_lines[1:], start=1):
        if ln.startswith("## "):
            insert_at = i
            break
    while insert_at > 1 and rest_lines[insert_at - 1].strip() == "":
        insert_at -= 1
    rest_lines.insert(insert_at, f"- {line}")
    return head + marker + "\n".join(rest_lines)


class Gate:
    def __init__(self, ledger: Ledger, conn: sqlite3.Connection) -> None:
        self.ledger = ledger
        self._conn = conn

    def propose(
        self,
        *,
        kind: Kind,
        content: str,
        provenance: Provenance,
        origin: str,
        section: str | None = None,
        old_text: str | None = None,
    ) -> Proposal:
        if kind == "add" and section not in LEDGER_SECTIONS:
            raise ValueError(f"add 必须指定合法小节,收到: {section!r};合法值 {LEDGER_SECTIONS}")
        if kind in ("amend", "retire") and not old_text:
            raise ValueError(f"{kind} 必须提供 old_text 用于定位")
        state = "passed" if provenance == "user_stated" else "pending"
        pid = uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO proposals (id, kind, section, content, old_text, provenance, origin,"
            " state, created_at, resolved_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                pid,
                kind,
                section,
                content,
                old_text,
                provenance,
                origin,
                state,
                _now(),
                _now() if state == "passed" else None,
            ),
        )
        return self.get(pid)

    def get(self, proposal_id: str) -> Proposal:
        row = self._conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        if row is None:
            raise KeyError(f"提案不存在: {proposal_id}")
        return _row_to_proposal(row)

    def pending(self) -> list[Proposal]:
        rows = self._conn.execute(
            "SELECT * FROM proposals WHERE state='pending' ORDER BY created_at"
        ).fetchall()
        return [_row_to_proposal(r) for r in rows]

    def unsettled_count(self) -> int:
        return int(
            self._conn.execute(
                "SELECT COUNT(*) FROM proposals WHERE state='passed' AND settled_at IS NULL"
            ).fetchone()[0]
        )

    def resolve(self, proposal_id: str, *, approved: bool) -> None:
        self._conn.execute(
            "UPDATE proposals SET state=?, resolved_at=? WHERE id=? AND state='pending'",
            ("passed" if approved else "dropped", _now(), proposal_id),
        )

    def settle(self) -> int:
        """把已通过、未落盘的提案一次性写入账本,一次快照。返回落盘条数。"""
        rows = self._conn.execute(
            "SELECT * FROM proposals WHERE state='passed' AND settled_at IS NULL "
            "ORDER BY created_at"
        ).fetchall()
        if not rows:
            return 0

        self.ledger.sync_manual_edit()
        content = self.ledger.read()
        applied: list[str] = []

        for r in rows:
            p = _row_to_proposal(r)
            if p.kind == "add":
                content = _insert_under_section(content, p.section or LEDGER_SECTIONS[0], p.content)
                applied.append(p.id)
            elif p.kind == "amend":
                if p.old_text and p.old_text in content:
                    content = content.replace(p.old_text, p.content, 1)
                    applied.append(p.id)
                else:
                    self._mark_stale(p.id)
            elif p.kind == "retire":
                if p.old_text and p.old_text in content:
                    content = "\n".join(ln for ln in content.split("\n") if p.old_text not in ln)
                    applied.append(p.id)
                else:
                    self._mark_stale(p.id)

        if not applied:
            return 0
        self.ledger.write(content, source="approval_batch", proposal_ids=applied)
        now = _now()
        self._conn.executemany(
            "UPDATE proposals SET settled_at=? WHERE id=?", [(now, pid) for pid in applied]
        )
        return len(applied)

    def _mark_stale(self, proposal_id: str) -> None:
        self._conn.execute(
            "UPDATE proposals SET state='dropped', note='stale: old_text 未匹配到,账本可能已被手编',"
            " resolved_at=? WHERE id=?",
            (_now(), proposal_id),
        )
