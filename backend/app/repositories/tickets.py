from __future__ import annotations

import json
from typing import Any

import sqlite3

from app.modules.diff_ticket import DispositionStatus


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ticket(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    return dict(r)


def get_ticket(conn: sqlite3.Connection, ticket_id: int) -> dict[str, Any] | None:
    return _ticket(conn.execute("SELECT * FROM diff_tickets WHERE id=?", (ticket_id,)).fetchone())


def get_ticket_by_diff(conn: sqlite3.Connection, diff_id: int) -> dict[str, Any] | None:
    return _ticket(
        conn.execute("SELECT * FROM diff_tickets WHERE diff_id=?", (diff_id,)).fetchone()
    )


def list_tickets(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM diff_tickets ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def count_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {s.value: 0 for s in DispositionStatus}
    rows = conn.execute(
        "SELECT status, COUNT(*) c FROM diff_tickets GROUP BY status"
    ).fetchall()
    for r in rows:
        counts[r["status"]] = r["c"]
    return counts


def insert_ticket(
    conn: sqlite3.Connection,
    *,
    pair_id: int,
    diff_id: int,
    title: str,
    note: str | None,
    created_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO diff_tickets(pair_id, diff_id, title, status, note, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?)
        """,
        (pair_id, diff_id, title, DispositionStatus.PENDING_CLAIM.value, note, created_at, created_at),
    )
    return int(cur.lastrowid)


def update_status(
    conn: sqlite3.Connection,
    *,
    ticket_id: int,
    status: str,
    assignee: str | None = None,
    note: str | None = None,
    last_fail_reason: str | None = None,
    clear_fail: bool = False,
    updated_at: str,
) -> None:
    conn.execute(
        """
        UPDATE diff_tickets
        SET status=?,
            assignee=COALESCE(?, assignee),
            note=COALESCE(?, note),
            last_fail_reason=CASE WHEN ? THEN NULL ELSE COALESCE(?, last_fail_reason) END,
            closed_at=CASE WHEN ?='closed' THEN ? ELSE closed_at END,
            updated_at=?
        WHERE id=?
        """,
        (
            status,
            assignee,
            note,
            1 if clear_fail else 0,
            last_fail_reason,
            status,
            updated_at,
            updated_at,
            ticket_id,
        ),
    )


def add_event(
    conn: sqlite3.Connection,
    *,
    ticket_id: int,
    action: str,
    from_status: str | None,
    to_status: str | None,
    actor: str | None = None,
    detail: dict[str, Any] | str | None = None,
    created_at: str | None = None,
) -> int:
    if isinstance(detail, dict):
        detail_text = json.dumps(detail, ensure_ascii=False)
    else:
        detail_text = detail
    cur = conn.execute(
        """
        INSERT INTO disposition_events(ticket_id, action, from_status, to_status, actor, detail, created_at)
        VALUES (?,?,?,?,?,?,?)
        """,
        (ticket_id, action, from_status, to_status, actor, detail_text, created_at or _now()),
    )
    return int(cur.lastrowid)


def list_events(conn: sqlite3.Connection, ticket_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM disposition_events WHERE ticket_id=? ORDER BY id", (ticket_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def insert_reinject_record(
    conn: sqlite3.Connection,
    *,
    ticket_id: int,
    diff_id: int,
    side: str,
    target_key: dict[str, Any],
    old_values: dict[str, Any],
    new_values: dict[str, Any],
    result: str,
    fail_reason: str | None,
    created_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO reinject_records(
            ticket_id, diff_id, side, target_key_json, old_values_json,
            new_values_json, result, fail_reason, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            ticket_id,
            diff_id,
            side,
            json.dumps(target_key, ensure_ascii=False, sort_keys=True),
            json.dumps(old_values, ensure_ascii=False),
            json.dumps(new_values, ensure_ascii=False),
            result,
            fail_reason,
            created_at,
        ),
    )
    return int(cur.lastrowid)


def list_reinject_records(
    conn: sqlite3.Connection, *, ticket_id: int | None = None
) -> list[dict[str, Any]]:
    if ticket_id is None:
        rows = conn.execute("SELECT * FROM reinject_records ORDER BY id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM reinject_records WHERE ticket_id=? ORDER BY id", (ticket_id,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["target_key"] = json.loads(d.pop("target_key_json"))
        d["old_values"] = json.loads(d.pop("old_values_json"))
        d["new_values"] = json.loads(d.pop("new_values_json"))
        out.append(d)
    return out


def count_reinject(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT result, COUNT(*) c FROM reinject_records GROUP BY result").fetchall()
    counts = {"success": 0, "failed": 0}
    for r in rows:
        counts[r["result"]] = r["c"]
    return counts
