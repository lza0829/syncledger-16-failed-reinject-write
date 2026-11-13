"""Side snapshot store.

The fixture JSON files stay immutable (they are the baseline extract). Every
reinject write lands here instead; reconcile runs overlay these rows on top
of the fixtures so a corrected side actually changes the next comparison.
"""

from __future__ import annotations

import json
from typing import Any

import sqlite3


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    d["align_key"] = json.loads(d.pop("align_key_json"))
    d["row"] = json.loads(d.pop("row_json"))
    return d


def get_snapshot_row(
    conn: sqlite3.Connection, *, pair_id: int, side: str, align_key: dict[str, Any]
) -> dict[str, Any] | None:
    key_json = json.dumps(align_key, ensure_ascii=False, sort_keys=True)
    r = conn.execute(
        "SELECT * FROM side_snapshots WHERE pair_id=? AND side=? AND align_key_json=?",
        (pair_id, side, key_json),
    ).fetchone()
    return _row(r)


def list_snapshot_rows(
    conn: sqlite3.Connection, *, pair_id: int, side: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM side_snapshots WHERE pair_id=? AND side=? ORDER BY id",
        (pair_id, side),
    ).fetchall()
    return [_row(r) for r in rows]  # type: ignore[misc]


def upsert_snapshot_row(
    conn: sqlite3.Connection,
    *,
    pair_id: int,
    side: str,
    align_key: dict[str, Any],
    row: dict[str, Any],
    updated_at: str,
    source: str = "reinject",
) -> None:
    key_json = json.dumps(align_key, ensure_ascii=False, sort_keys=True)
    conn.execute(
        """
        INSERT INTO side_snapshots(pair_id, side, align_key_json, row_json, source, updated_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(pair_id, side, align_key_json) DO UPDATE SET
            row_json=excluded.row_json,
            source=excluded.source,
            updated_at=excluded.updated_at
        """,
        (pair_id, side, key_json, json.dumps(row, ensure_ascii=False), source, updated_at),
    )


def overlay_rows(
    conn: sqlite3.Connection, *, pair_id: int, side: str, baseline: list[dict[str, Any]],
    align_keys: list[str],
) -> list[dict[str, Any]]:
    """Baseline fixture rows with snapshot corrections/inserts overlaid."""
    merged: dict[tuple, dict[str, Any]] = {
        tuple(r.get(k) for k in align_keys): dict(r) for r in baseline
    }
    for snap in list_snapshot_rows(conn, pair_id=pair_id, side=side):
        key = tuple(snap["align_key"].get(k) for k in align_keys)
        merged[key] = dict(snap["row"])
    return list(merged.values())
