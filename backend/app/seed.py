from __future__ import annotations

import json

from app.db import connect


PAIR_SEEDS = [
    {
        "code": "inventory_hq_vs_store",
        "title": "总部库存 ↔ 门店库存",
        "domain": "inventory",
        "left_name": "总部库存夹具",
        "right_name": "门店库存夹具",
        "left_fixture": "inventory_hq.json",
        "right_fixture": "inventory_store.json",
        "align_keys": ["sku_id", "store_id"],
        "compare_fields": ["qty", "unit"],
        "enabled": 1,
    },
    {
        "code": "payment_vs_ledger",
        "title": "支付网关 ↔ 账务流水",
        "domain": "payment",
        "left_name": "支付网关夹具",
        "right_name": "账务流水夹具",
        "left_fixture": "payment_gateway.json",
        "right_fixture": "payment_ledger.json",
        "align_keys": ["txn_id"],
        "compare_fields": ["amount", "currency"],
        "enabled": 1,
    },
    {
        "code": "sku_catalog_a_vs_b",
        "title": "商品目录 A ↔ 目录 B",
        "domain": "catalog",
        "left_name": "目录 A 夹具",
        "right_name": "目录 B 夹具",
        "left_fixture": "sku_catalog_a.json",
        "right_fixture": "sku_catalog_b.json",
        "align_keys": ["sku_code"],
        "compare_fields": ["name", "category"],
        "enabled": 1,
    },
]

CONTRACT_SEEDS = [
    ("inventory_hq_vs_store", "left", ["sku_id", "store_id", "qty", "unit", "updated_at"]),
    ("inventory_hq_vs_store", "right", ["sku_id", "store_id", "qty", "unit", "updated_at"]),
    ("payment_vs_ledger", "left", ["txn_id", "amount", "currency", "paid_at"]),
    ("payment_vs_ledger", "right", ["txn_id", "amount", "currency", "booked_at"]),
    ("sku_catalog_a_vs_b", "left", ["sku_code", "name", "category", "updated_at"]),
    ("sku_catalog_a_vs_b", "right", ["sku_code", "name", "category", "updated_at"]),
]


def _migrate_legacy_schema(conn) -> None:
    """Idempotent ALTERs for databases created before the disposition cut."""
    ticket_cols = {r["name"] for r in conn.execute("PRAGMA table_info(diff_tickets)")}
    if "last_fail_reason" not in ticket_cols:
        conn.execute("ALTER TABLE diff_tickets ADD COLUMN last_fail_reason TEXT")
    if "closed_at" not in ticket_cols:
        conn.execute("ALTER TABLE diff_tickets ADD COLUMN closed_at TEXT")
    # The base build seeded 'open' as placeholder status; map it onto the
    # new state machine so old rows never sit on an unknown status.
    conn.execute(
        "UPDATE diff_tickets SET status='pending_claim' WHERE status NOT IN "
        "('pending_claim','in_progress','pending_review','closed')"
    )


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS reconcile_pairs(
                id INTEGER PRIMARY KEY,
                code TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                domain TEXT NOT NULL,
                left_name TEXT NOT NULL,
                right_name TEXT NOT NULL,
                left_fixture TEXT NOT NULL,
                right_fixture TEXT NOT NULL,
                align_keys_json TEXT NOT NULL,
                compare_fields_json TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reconcile_batches(
                id INTEGER PRIMARY KEY,
                batch_no TEXT NOT NULL UNIQUE,
                pair_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                left_rows INTEGER NOT NULL,
                right_rows INTEGER NOT NULL,
                matched INTEGER NOT NULL,
                left_only INTEGER NOT NULL,
                right_only INTEGER NOT NULL,
                field_mismatch INTEGER NOT NULL,
                diff_total INTEGER NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(pair_id) REFERENCES reconcile_pairs(id)
            );
            CREATE TABLE IF NOT EXISTS reconcile_diffs(
                id INTEGER PRIMARY KEY,
                batch_id INTEGER NOT NULL,
                pair_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                align_key_json TEXT NOT NULL,
                left_json TEXT,
                right_json TEXT,
                mismatched_fields_json TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'medium',
                created_at TEXT NOT NULL,
                FOREIGN KEY(batch_id) REFERENCES reconcile_batches(id),
                FOREIGN KEY(pair_id) REFERENCES reconcile_pairs(id)
            );
            CREATE TABLE IF NOT EXISTS diff_tickets(
                id INTEGER PRIMARY KEY,
                pair_id INTEGER,
                diff_id INTEGER NOT NULL UNIQUE,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending_claim',
                assignee TEXT,
                note TEXT,
                last_fail_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                closed_at TEXT,
                FOREIGN KEY(pair_id) REFERENCES reconcile_pairs(id),
                FOREIGN KEY(diff_id) REFERENCES reconcile_diffs(id)
            );
            CREATE TABLE IF NOT EXISTS disposition_events(
                id INTEGER PRIMARY KEY,
                ticket_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT,
                actor TEXT,
                detail TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(ticket_id) REFERENCES diff_tickets(id)
            );
            CREATE TABLE IF NOT EXISTS reinject_records(
                id INTEGER PRIMARY KEY,
                ticket_id INTEGER NOT NULL,
                diff_id INTEGER NOT NULL,
                side TEXT NOT NULL,
                target_key_json TEXT NOT NULL,
                old_values_json TEXT NOT NULL,
                new_values_json TEXT NOT NULL,
                result TEXT NOT NULL,
                fail_reason TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(ticket_id) REFERENCES diff_tickets(id),
                FOREIGN KEY(diff_id) REFERENCES reconcile_diffs(id)
            );
            CREATE TABLE IF NOT EXISTS side_snapshots(
                id INTEGER PRIMARY KEY,
                pair_id INTEGER NOT NULL,
                side TEXT NOT NULL,
                align_key_json TEXT NOT NULL,
                row_json TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'reinject',
                updated_at TEXT NOT NULL,
                UNIQUE(pair_id, side, align_key_json),
                FOREIGN KEY(pair_id) REFERENCES reconcile_pairs(id)
            );
            CREATE TABLE IF NOT EXISTS endpoint_contracts(
                id INTEGER PRIMARY KEY,
                pair_code TEXT NOT NULL,
                side TEXT NOT NULL,
                fields_json TEXT NOT NULL,
                version TEXT NOT NULL DEFAULT 'v1',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        _migrate_legacy_schema(conn)
        if conn.execute("SELECT COUNT(*) c FROM reconcile_pairs").fetchone()["c"] == 0:
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            for p in PAIR_SEEDS:
                conn.execute(
                    """
                    INSERT INTO reconcile_pairs(
                        code, title, domain, left_name, right_name,
                        left_fixture, right_fixture, align_keys_json, compare_fields_json,
                        enabled, created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        p["code"],
                        p["title"],
                        p["domain"],
                        p["left_name"],
                        p["right_name"],
                        p["left_fixture"],
                        p["right_fixture"],
                        json.dumps(p["align_keys"], ensure_ascii=False),
                        json.dumps(p["compare_fields"], ensure_ascii=False),
                        p["enabled"],
                        now,
                    ),
                )
        if conn.execute("SELECT COUNT(*) c FROM endpoint_contracts").fetchone()["c"] == 0:
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            for pair_code, side, fields in CONTRACT_SEEDS:
                conn.execute(
                    """
                    INSERT INTO endpoint_contracts(pair_code, side, fields_json, version, updated_at)
                    VALUES (?,?,?,?,?)
                    """,
                    (pair_code, side, json.dumps(fields, ensure_ascii=False), "v1", now),
                )
        if conn.execute("SELECT COUNT(*) c FROM settings").fetchone()["c"] == 0:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?)",
                ("default_policy", "record_only"),
            )
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?)",
                ("auto_overwrite", "false"),
            )
        conn.commit()
    finally:
        conn.close()
