"""Difference disposition workflow service.

Implements the full loop:

  diff -> create disposition -> claim -> reinject into a side snapshot
       -> submit for review -> review against the actual side values
       -> close (approve) / back to in_progress (reject)

Hard rules enforced here, never trusted to the UI:
  * illegal status jumps are rejected;
  * reinject is only possible on a claimed (in_progress) ticket;
  * a failed reinject is recorded with old/new values and leaves the ticket
    actionable — it can never enter pending_review;
  * review re-reads the side snapshot and compares it to every successful
    reinject record, so a ticket cannot close unless the side really changed;
  * close is only reachable via review approval.
"""

from __future__ import annotations

import json
from typing import Any

from app.db import connect
from app.modules.diff_ticket import (
    Action,
    DispositionError,
    DispositionStatus,
    OPEN_STATUSES,
    STATUS_LABELS,
    next_status,
)
from app.repositories import pairs as pair_repo
from app.repositories import side_snapshots as snap_repo
from app.repositories import tickets as ticket_repo
from app.repositories.batches import list_diffs

NUMERIC_FIELDS = {"qty", "amount"}


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

def _load_diff(conn, diff_id: int) -> dict[str, Any]:
    rows = list_diffs(conn, limit=10_000)
    for d in rows:
        if d["id"] == diff_id:
            return d
    raise DispositionError(f"差异 #{diff_id} 不存在", http_status=404)


def list_dispositions() -> list[dict[str, Any]]:
    conn = connect()
    try:
        tickets = ticket_repo.list_tickets(conn)
        for t in tickets:
            t["status_label"] = STATUS_LABELS.get(t["status"], t["status"])
        return tickets
    finally:
        conn.close()


def _effective_side_rows(
    conn, pair: dict[str, Any], side: str
) -> list[dict[str, Any]]:
    """Current effective rows of a side: immutable fixture + snapshot overlay."""
    from app.services.reconcile_service import _load_fixture

    fixture = pair["left_fixture"] if side == "left" else pair["right_fixture"]
    baseline = _load_fixture(fixture)
    return snap_repo.overlay_rows(
        conn,
        pair_id=pair["id"],
        side=side,
        baseline=baseline,
        align_keys=pair["align_keys"],
    )


def _side_view(conn, pair: dict[str, Any], align_key: dict[str, Any]) -> dict[str, Any]:
    view: dict[str, Any] = {}
    for side in ("left", "right"):
        match = None
        for row in _effective_side_rows(conn, pair, side):
            if all(row.get(k) == v for k, v in align_key.items()):
                match = row
                break
        view[side] = match
    return view


def get_disposition(ticket_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        ticket = ticket_repo.get_ticket(conn, ticket_id)
        if ticket is None:
            raise DispositionError(f"处置单 #{ticket_id} 不存在", http_status=404)
        ticket["status_label"] = STATUS_LABELS.get(ticket["status"], ticket["status"])
        ticket["events"] = ticket_repo.list_events(conn, ticket_id)
        ticket["reinjects"] = ticket_repo.list_reinject_records(conn, ticket_id=ticket_id)
        diff = next(
            (d for d in list_diffs(conn, limit=10_000) if d["id"] == ticket["diff_id"]), None
        )
        ticket["diff"] = diff
        pair = pair_repo.get_pair(conn, ticket["pair_id"])
        ticket["pair"] = pair
        if diff is not None and pair is not None:
            ticket["side_view"] = _side_view(conn, pair, diff["align_key"])
        else:
            ticket["side_view"] = {"left": None, "right": None}
        return ticket
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _validate_fields(pair: dict[str, Any], fields: dict[str, Any]) -> None:
    allowed = set(pair["compare_fields"])
    unknown = set(fields) - allowed
    if unknown:
        raise DispositionError(
            f"字段 {sorted(unknown)} 不在对账对约定的比对字段 {pair['compare_fields']} 内"
        )
    if not fields:
        raise DispositionError("回灌必须至少包含一个约定字段")
    for name, value in fields.items():
        if name in NUMERIC_FIELDS:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise DispositionError(f"字段 {name} 必须是数值，收到 {value!r}")
        elif not isinstance(value, str):
            raise DispositionError(f"字段 {name} 必须是字符串，收到 {value!r}")


def _validate_side(side: str) -> None:
    if side not in ("left", "right"):
        raise DispositionError("回灌侧必须是 left 或 right")


def _record_reinject_failure(
    conn, *, ticket, side, target_key, old_values, fields, reason, now
) -> None:
    """Persist the failed attempt and keep the ticket actionable."""
    ticket_repo.insert_reinject_record(
        conn,
        ticket_id=ticket["id"],
        diff_id=ticket["diff_id"],
        side=side,
        target_key=target_key,
        old_values=old_values,
        new_values=fields,
        result="failed",
        fail_reason=reason,
        created_at=now,
    )
    ticket_repo.update_status(
        conn,
        ticket_id=ticket["id"],
        status=DispositionStatus.IN_PROGRESS.value,
        last_fail_reason=reason,
        updated_at=now,
    )
    ticket_repo.add_event(
        conn,
        ticket_id=ticket["id"],
        action=Action.REINJECT.value,
        from_status=DispositionStatus.IN_PROGRESS.value,
        to_status=DispositionStatus.IN_PROGRESS.value,
        actor=ticket["assignee"],
        detail={"result": "failed", "side": side, "reason": reason, "new_values": fields},
        created_at=now,
    )


# --------------------------------------------------------------------------- #
# Workflow actions
# --------------------------------------------------------------------------- #

def create_disposition(diff_id: int, note: str | None = None) -> dict[str, Any]:
    conn = connect()
    try:
        diff = _load_diff(conn, diff_id)
        existing = ticket_repo.get_ticket_by_diff(conn, diff_id)
        if existing is not None:
            raise DispositionError(
                f"差异 #{diff_id} 已存在处置单 #{existing['id']}，不得重复开单"
            )
        now = _now()
        pair = pair_repo.get_pair(conn, diff["pair_id"])
        assert pair is not None
        title = f"{pair['code']} / {diff['kind']} / {diff['align_key']}"
        ticket_id = ticket_repo.insert_ticket(
            conn,
            pair_id=diff["pair_id"],
            diff_id=diff_id,
            title=title,
            note=note,
            created_at=now,
        )
        ticket_repo.add_event(
            conn,
            ticket_id=ticket_id,
            action=Action.CREATE.value,
            from_status=None,
            to_status=DispositionStatus.PENDING_CLAIM.value,
            detail={"diff_id": diff_id, "note": note},
            created_at=now,
        )
        conn.commit()
        return get_disposition(ticket_id)
    finally:
        conn.close()


def claim_disposition(ticket_id: int, assignee: str) -> dict[str, Any]:
    conn = connect()
    try:
        ticket = ticket_repo.get_ticket(conn, ticket_id)
        if ticket is None:
            raise DispositionError(f"处置单 #{ticket_id} 不存在", http_status=404)
        if not assignee or not assignee.strip():
            raise DispositionError("认领必须指定处理人")
        target = next_status(Action.CLAIM.value, ticket["status"])
        now = _now()
        ticket_repo.update_status(
            conn, ticket_id=ticket_id, status=target, assignee=assignee.strip(), updated_at=now
        )
        ticket_repo.add_event(
            conn,
            ticket_id=ticket_id,
            action=Action.CLAIM.value,
            from_status=ticket["status"],
            to_status=target,
            actor=assignee.strip(),
            created_at=now,
        )
        conn.commit()
        return get_disposition(ticket_id)
    finally:
        conn.close()


def reinject_disposition(
    ticket_id: int,
    *,
    side: str,
    fields: dict[str, Any],
    simulate_failure: bool = False,
) -> dict[str, Any]:
    """Write agreed fields into the target side snapshot, old/new recorded."""
    conn = connect()
    try:
        ticket = ticket_repo.get_ticket(conn, ticket_id)
        if ticket is None:
            raise DispositionError(f"处置单 #{ticket_id} 不存在", http_status=404)
        # Gate: only a claimed ticket (in_progress) may be reinjected.
        if ticket["status"] != DispositionStatus.IN_PROGRESS.value:
            raise DispositionError(
                f"当前状态 {STATUS_LABELS[ticket['status']]} 不可回灌；"
                "只有认领后的「处理中」处置单允许回灌"
            )
        _validate_side(side)
        diff = _load_diff(conn, ticket["diff_id"])
        pair = pair_repo.get_pair(conn, ticket["pair_id"])
        assert pair is not None
        _validate_fields(pair, dict(fields))

        target_key = diff["align_key"]
        snap = snap_repo.get_snapshot_row(
            conn, pair_id=pair["id"], side=side, align_key=target_key
        )
        current_row = snap["row"] if snap else (diff.get(side) or {})
        old_values = {f: current_row.get(f) for f in fields}
        now = _now()

        merged_row = dict(current_row)
        merged_row.update(fields)
        for k, v in target_key.items():
            merged_row.setdefault(k, v)

        snap_repo.upsert_snapshot_row(
            conn,
            pair_id=pair["id"],
            side=side,
            align_key=target_key,
            row=merged_row,
            updated_at=now,
        )

        if simulate_failure:
            reason = "模拟侧写入失败：侧夹具/侧快照拒绝本次写入（故障注入）"
            _record_reinject_failure(
                conn,
                ticket=ticket,
                side=side,
                target_key=target_key,
                old_values=old_values,
                fields=dict(fields),
                reason=reason,
                now=now,
            )
            conn.commit()
            failed = get_disposition(ticket_id)
            failed["write_failed"] = True
            failed["fail_reason"] = reason
            return failed

        ticket_repo.insert_reinject_record(
            conn,
            ticket_id=ticket_id,
            diff_id=ticket["diff_id"],
            side=side,
            target_key=target_key,
            old_values=old_values,
            new_values=dict(fields),
            result="success",
            fail_reason=None,
            created_at=now,
        )
        ticket_repo.update_status(
            conn,
            ticket_id=ticket_id,
            status=DispositionStatus.IN_PROGRESS.value,
            clear_fail=True,
            updated_at=now,
        )
        ticket_repo.add_event(
            conn,
            ticket_id=ticket_id,
            action=Action.REINJECT.value,
            from_status=DispositionStatus.IN_PROGRESS.value,
            to_status=DispositionStatus.IN_PROGRESS.value,
            actor=ticket["assignee"],
            detail={"result": "success", "side": side, "old": old_values, "new": dict(fields)},
            created_at=now,
        )
        conn.commit()
        return get_disposition(ticket_id)
    finally:
        conn.close()


def submit_for_review(ticket_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        ticket = ticket_repo.get_ticket(conn, ticket_id)
        if ticket is None:
            raise DispositionError(f"处置单 #{ticket_id} 不存在", http_status=404)
        if ticket["status"] != DispositionStatus.IN_PROGRESS.value:
            raise DispositionError(
                f"当前状态 {STATUS_LABELS[ticket['status']]} 不可提交复核"
            )
        records = ticket_repo.list_reinject_records(conn, ticket_id=ticket_id)
        if not records:
            raise DispositionError("尚无回灌记录，不得提交复核（禁止只改单据状态）")
        target = next_status(Action.SUBMIT_REVIEW.value, ticket["status"])
        now = _now()
        ticket_repo.update_status(conn, ticket_id=ticket_id, status=target, updated_at=now)
        ticket_repo.add_event(
            conn,
            ticket_id=ticket_id,
            action=Action.SUBMIT_REVIEW.value,
            from_status=ticket["status"],
            to_status=target,
            actor=ticket["assignee"],
            created_at=now,
        )
        conn.commit()
        return get_disposition(ticket_id)
    finally:
        conn.close()


def review_disposition(ticket_id: int, *, approved: bool, reason: str | None = None) -> dict[str, Any]:
    conn = connect()
    try:
        ticket = ticket_repo.get_ticket(conn, ticket_id)
        if ticket is None:
            raise DispositionError(f"处置单 #{ticket_id} 不存在", http_status=404)
        if ticket["status"] != DispositionStatus.PENDING_REVIEW.value:
            raise DispositionError(
                f"当前状态 {STATUS_LABELS[ticket['status']]} 不在待复核，无法复核"
            )
        now = _now()

        if not approved:
            if not reason or not reason.strip():
                raise DispositionError("驳回必须填写原因")
            target = next_status(Action.REVIEW_REJECT.value, ticket["status"])
            ticket_repo.update_status(
                conn,
                ticket_id=ticket_id,
                status=target,
                last_fail_reason=reason.strip(),
                updated_at=now,
            )
            ticket_repo.add_event(
                conn,
                ticket_id=ticket_id,
                action=Action.REVIEW_REJECT.value,
                from_status=ticket["status"],
                to_status=target,
                actor=ticket["assignee"],
                detail={"reason": reason.strip()},
                created_at=now,
            )
            conn.commit()
            return get_disposition(ticket_id)

        # Approval gate: re-read the actual side values and compare against
        # every successful reinject record. Mismatch means the side was never
        # really corrected (or drifted) — approval is refused.
        checks = _check_side_values(conn, ticket)
        mismatches = [c for c in checks if not c["matches"]]
        if mismatches:
            detail = "; ".join(
                f"{c['side']}.{c['field']} 侧值={c['side_value']!r} 回灌值={c['expected_value']!r}"
                for c in mismatches
            )
            raise DispositionError(f"复核不通过：侧实际值与回灌记录不符（{detail}）")

        target = next_status(Action.REVIEW_APPROVE.value, ticket["status"])
        ticket_repo.update_status(
            conn, ticket_id=ticket_id, status=target, clear_fail=True, updated_at=now
        )
        ticket_repo.add_event(
            conn,
            ticket_id=ticket_id,
            action=Action.REVIEW_APPROVE.value,
            from_status=ticket["status"],
            to_status=target,
            actor=ticket["assignee"],
            detail={"checks": checks},
            created_at=now,
        )
        conn.commit()
        return get_disposition(ticket_id)
    finally:
        conn.close()


def _check_side_values(conn, ticket: dict[str, Any]) -> list[dict[str, Any]]:
    """Compare the side's current values against the reinject ledger.

    When a field was reinjected more than once, only the LATEST successful
    write defines the intended final value; earlier records are the audit
    trail and must not be expected to still match the live side.
    """
    records = ticket_repo.list_reinject_records(conn, ticket_id=ticket["id"])
    latest: dict[tuple, dict[str, Any]] = {}
    for rec in records:
        for field, expected in rec["new_values"].items():
            latest[(rec["side"], json.dumps(rec["target_key"], sort_keys=True), field)] = {
                "reinject_id": rec["id"],
                "side": rec["side"],
                "field": field,
                "expected_value": expected,
                "target_key": rec["target_key"],
            }

    # Cache current side rows per (side, key).
    side_row_cache: dict[tuple, dict[str, Any]] = {}

    def _current_row(side: str, target_key: dict[str, Any]) -> dict[str, Any]:
        cache_key = (side, json.dumps(target_key, sort_keys=True))
        if cache_key not in side_row_cache:
            snap = snap_repo.get_snapshot_row(
                conn,
                pair_id=ticket["pair_id"],
                side=side,
                align_key=target_key,
            )
            side_row_cache[cache_key] = snap["row"] if snap else {}
        return side_row_cache[cache_key]

    checks: list[dict[str, Any]] = []
    for item in latest.values():
        side_row = _current_row(item["side"], item["target_key"])
        actual = side_row.get(item["field"])
        checks.append(
            {
                "reinject_id": item["reinject_id"],
                "side": item["side"],
                "field": item["field"],
                "expected_value": item["expected_value"],
                "side_value": actual,
                "matches": actual == item["expected_value"],
            }
        )
    return checks


def list_side_snapshots() -> list[dict[str, Any]]:
    conn = connect()
    try:
        out: list[dict[str, Any]] = []
        for pair in pair_repo.list_pairs(conn):
            for side in ("left", "right"):
                for snap in snap_repo.list_snapshot_rows(conn, pair_id=pair["id"], side=side):
                    snap["pair_id"] = pair["id"]
                    snap["pair_code"] = pair["code"]
                    snap["side"] = side
                    out.append(snap)
        return out
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Cross-evidence (互证)
# --------------------------------------------------------------------------- #

def cross_evidence() -> dict[str, Any]:
    """Status counts, reinject ledger, side data and diff list must agree."""
    conn = connect()
    try:
        diffs = list_diffs(conn, limit=10_000)
        tickets = ticket_repo.list_tickets(conn)
        status_counts = ticket_repo.count_by_status(conn)
        reinject_counts = ticket_repo.count_reinject(conn)
        open_status = set(OPEN_STATUSES)

        diff_ids_with_ticket = {t["diff_id"] for t in tickets}
        undisposed = sum(1 for d in diffs if d["id"] not in diff_ids_with_ticket)
        closed = status_counts.get(DispositionStatus.CLOSED.value, 0)
        open_tickets = sum(status_counts.get(s, 0) for s in open_status)

        checks: list[dict[str, Any]] = []

        # 1. Every closed ticket's side values really match its reinject ledger.
        for t in tickets:
            if t["status"] != DispositionStatus.CLOSED.value:
                continue
            bad = [c for c in _check_side_values(conn, t) if not c["matches"]]
            checks.append(
                {
                    "name": f"处置单 #{t['id']} 侧值与回灌记录一致",
                    "pass": not bad,
                    "detail": bad or "全部字段一致",
                }
            )

        # 2. Every reinject record points at an existing diff.
        orphan = [
            r["id"]
            for r in ticket_repo.list_reinject_records(conn)
            if not any(d["id"] == r["diff_id"] for d in diffs)
        ]
        checks.append(
            {
                "name": "回灌记录均挂在真实差异上",
                "pass": not orphan,
                "detail": orphan or "无悬挂回灌记录",
            }
        )

        # 3. Tickets in review/closed must have at least one success reinject.
        no_ledger = []
        for t in tickets:
            if t["status"] in (
                DispositionStatus.PENDING_REVIEW.value,
                DispositionStatus.CLOSED.value,
            ):
                ok = any(
                    r["result"] == "success"
                    for r in ticket_repo.list_reinject_records(conn, ticket_id=t["id"])
                )
                if not ok:
                    no_ledger.append(t["id"])
        checks.append(
            {
                "name": "待复核/已关闭处置单均有成功回灌记录",
                "pass": not no_ledger,
                "detail": no_ledger or "均有回灌台账",
            }
        )

        # 4. Status-count sum equals ticket total.
        checks.append(
            {
                "name": "各状态数量之和等于处置单总数",
                "pass": sum(status_counts.values()) == len(tickets),
                "detail": {"sum": sum(status_counts.values()), "total": len(tickets)},
            }
        )

        return {
            "status_counts": status_counts,
            "status_labels": STATUS_LABELS,
            "reinject_counts": reinject_counts,
            "side_snapshots": list_side_snapshots(),
            "diff_total": len(diffs),
            "disposed_total": len(tickets),
            "undisposed_diffs": undisposed,
            "closed_dispositions": closed,
            "open_dispositions": open_tickets,
            "checks": checks,
            "all_pass": all(c["pass"] for c in checks),
        }
    finally:
        conn.close()
