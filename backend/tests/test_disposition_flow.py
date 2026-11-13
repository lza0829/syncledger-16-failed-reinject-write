import importlib
import tempfile

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    """Fresh isolated database per test."""
    tmp = tempfile.mkdtemp(prefix="syncledger-test-")
    monkeypatch.setenv("DATA_DIR", tmp)
    import app.config as config
    import app.db as db

    importlib.reload(config)
    monkeypatch.setattr(db, "DB_PATH", config.DATA_DIR / config.DB_FILENAME)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    from app.main import app

    with TestClient(app) as c:
        yield c


from app.modules.diff_ticket import STATUS_LABELS  # noqa: E402


def _run_pair(client, pair_id):
    r = client.post("/api/reconcile", json={"pair_id": pair_id, "note": "test"})
    assert r.status_code == 200, r.text
    return r.json()


def _first_field_mismatch(client, pair_id):
    r = client.get(f"/api/diffs?pair_id={pair_id}")
    assert r.status_code == 200
    diffs = r.json()
    return next(d for d in diffs if d["kind"] == "field_mismatch")


def test_full_happy_path_closes_only_after_review(client):
    _run_pair(client, 1)  # inventory pair
    diff = _first_field_mismatch(client, 1)
    assert diff["disposition_state"] == "undisposed"
    assert diff["disposition"] is None

    r = client.post("/api/dispositions", json={"diff_id": diff["id"]})
    assert r.status_code == 200, r.text
    ticket = r.json()
    tid = ticket["id"]
    assert ticket["status"] == "pending_claim"
    assert ticket["status_label"] == STATUS_LABELS["pending_claim"]

    # duplicate disposition on the same diff is refused
    r = client.post("/api/dispositions", json={"diff_id": diff["id"]})
    assert r.status_code == 400

    # illegal jump: review before claim
    r = client.post(f"/api/dispositions/{tid}/review", json={"approved": True})
    assert r.status_code == 400

    r = client.post(f"/api/dispositions/{tid}/claim", json={"assignee": "alice"})
    assert r.status_code == 200
    assert r.json()["status"] == "in_progress"

    # reinject the agreed field (qty) onto the right side, matching the left value
    new_qty = diff["left"]["qty"]
    r = client.post(
        f"/api/dispositions/{tid}/reinject",
        json={"side": "right", "fields": {"qty": new_qty}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    rec = body["reinjects"][-1]
    assert rec["result"] == "success"
    assert rec["old_values"] == {"qty": diff["right"]["qty"]}
    assert rec["new_values"] == {"qty": new_qty}
    # ticket stays in_progress after a successful write — review is a separate step
    assert body["status"] == "in_progress"

    # the side snapshot really carries the new value
    r = client.get("/api/side-snapshots")
    snaps = r.json()
    mine = [s for s in snaps if s["pair_id"] == 1 and s["side"] == "right"]
    assert mine and mine[0]["row"]["qty"] == new_qty

    r = client.post(f"/api/dispositions/{tid}/submit-review")
    assert r.status_code == 200
    assert r.json()["status"] == "pending_review"

    # no direct close: approving works, closing itself is only via review
    r = client.post(f"/api/dispositions/{tid}/review", json={"approved": True})
    assert r.status_code == 200
    closed = r.json()
    assert closed["status"] == "closed"
    assert closed["closed_at"]

    # diff list now distinguishes closed vs undisposed
    diffs = client.get("/api/diffs?pair_id=1").json()
    marked = next(d for d in diffs if d["id"] == diff["id"])
    assert marked["disposition_state"] == "closed"
    assert marked["disposition"]["status_label"] == "已关闭"
    assert any(d["disposition_state"] == "undisposed" for d in diffs)

    # rerun reconcile: the corrected field_mismatch disappears from the new batch
    batch2 = _run_pair(client, 1)
    assert batch2["field_mismatch"] == 0


def test_reinject_requires_claim(client):
    _run_pair(client, 2)  # payment pair
    diff = _first_field_mismatch(client, 2)
    tid = client.post("/api/dispositions", json={"diff_id": diff["id"]}).json()["id"]

    r = client.post(
        f"/api/dispositions/{tid}/reinject",
        json={"side": "right", "fields": {"amount": diff["left"]["amount"]}},
    )
    assert r.status_code == 400
    assert "认领" in r.text


def test_failed_reinject_keeps_ticket_actionable_and_blocks_review(client):
    _run_pair(client, 2)
    diff = _first_field_mismatch(client, 2)
    tid = client.post("/api/dispositions", json={"diff_id": diff["id"]}).json()["id"]
    client.post(f"/api/dispositions/{tid}/claim", json={"assignee": "bob"})

    r = client.post(
        f"/api/dispositions/{tid}/reinject",
        json={"side": "right", "fields": {"amount": 999}, "simulate_failure": True},
    )
    # failure is surfaced, and the failed attempt is still fully recorded
    assert r.status_code == 422
    assert "失败" in r.text

    detail = client.get(f"/api/dispositions/{tid}").json()
    assert detail["status"] == "in_progress"
    failed_rec = next(rr for rr in detail["reinjects"] if rr["result"] == "failed")
    assert failed_rec["new_values"] == {"amount": 999}
    assert failed_rec["fail_reason"]
    assert detail["last_fail_reason"]

    # a failed write must leave the side untouched — no failed value in the
    # snapshot that the next reconcile would overlay as a real correction
    snapped = [
        s for s in client.get("/api/side-snapshots").json()
        if s["pair_id"] == 2 and s["side"] == "right"
        and s["align_key"] == diff["align_key"]
    ]
    assert not snapped, "失败回灌不得写入侧快照"
    assert detail["side_view"]["right"]["amount"] != 999

    # failed records are not evidence: review cannot be submitted
    r = client.post(f"/api/dispositions/{tid}/submit-review")
    assert r.status_code == 400
    assert "成功" in r.text
    assert client.get(f"/api/dispositions/{tid}").json()["status"] == "in_progress"

    # the next reconcile must still see the original mismatch, not the
    # failed attempt's value
    batch = _run_pair(client, 2)
    assert batch["field_mismatch"] >= 1

    # a real successful write then lets the normal chain proceed
    ok = client.post(
        f"/api/dispositions/{tid}/reinject",
        json={"side": "right", "fields": {"amount": diff["left"]["amount"]}},
    )
    assert ok.status_code == 200
    assert client.post(f"/api/dispositions/{tid}/submit-review").status_code == 200
    r = client.post(f"/api/dispositions/{tid}/review", json={"approved": True})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "closed"


def test_failed_reinject_after_success_does_not_count_for_review(client):
    _run_pair(client, 1)
    free = _first_field_mismatch(client, 1)
    tid = client.post("/api/dispositions", json={"diff_id": free["id"]}).json()["id"]
    client.post(f"/api/dispositions/{tid}/claim", json={"assignee": "holly"})

    ok = client.post(
        f"/api/dispositions/{tid}/reinject",
        json={"side": "right", "fields": {"qty": free["left"]["qty"]}},
    )
    assert ok.status_code == 200
    # a later FAILED attempt must neither pollute the side nor become the
    # expected value at review
    bad = client.post(
        f"/api/dispositions/{tid}/reinject",
        json={"side": "right", "fields": {"qty": 555}, "simulate_failure": True},
    )
    assert bad.status_code == 422
    # a failed attempt also must not erase the prior success's evidence
    assert client.post(f"/api/dispositions/{tid}/submit-review").status_code == 200
    r = client.post(f"/api/dispositions/{tid}/review", json={"approved": True})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "closed"

    snapped = next(
        s for s in client.get("/api/side-snapshots").json()
        if s["pair_id"] == 1 and s["side"] == "right" and s["align_key"] == free["align_key"]
    )
    assert snapped["row"]["qty"] == free["left"]["qty"]


def test_review_rejection_returns_to_in_progress_with_reason(client):
    _run_pair(client, 2)
    free = _first_field_mismatch(client, 2)
    tid = client.post("/api/dispositions", json={"diff_id": free["id"]}).json()["id"]
    client.post(f"/api/dispositions/{tid}/claim", json={"assignee": "carol"})
    client.post(
        f"/api/dispositions/{tid}/reinject",
        json={"side": "right", "fields": {"amount": free["left"]["amount"]}},
    )
    client.post(f"/api/dispositions/{tid}/submit-review")

    # reject without reason is refused
    r = client.post(f"/api/dispositions/{tid}/review", json={"approved": False})
    assert r.status_code == 400
    assert "原因" in r.text

    r = client.post(
        f"/api/dispositions/{tid}/review",
        json={"approved": False, "reason": "复核发现金额仍与网关不符"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "in_progress"
    assert body["last_fail_reason"] == "复核发现金额仍与网关不符"

    # after rejection the ticket can be reworked and eventually closed
    client.post(f"/api/dispositions/{tid}/submit-review")
    r = client.post(f"/api/dispositions/{tid}/review", json={"approved": True})
    assert r.status_code == 200
    assert r.json()["status"] == "closed"


def test_review_blocks_when_side_value_drifts_from_reinject_record(client):
    _run_pair(client, 1)
    free = _first_field_mismatch(client, 1)
    pair_id = 1
    field = "qty"
    good = free["left"]["qty"]

    tid = client.post("/api/dispositions", json={"diff_id": free["id"]}).json()["id"]
    client.post(f"/api/dispositions/{tid}/claim", json={"assignee": "dave"})
    client.post(
        f"/api/dispositions/{tid}/reinject",
        json={"side": "right", "fields": {field: good}},
    )
    client.post(f"/api/dispositions/{tid}/submit-review")

    # someone changes only the side data afterwards (drift) — review must fail
    from app.db import connect
    from app.repositories import side_snapshots as snap_repo

    conn = connect()
    snap = snap_repo.get_snapshot_row(
        conn, pair_id=pair_id, side="right", align_key=free["align_key"]
    )
    drifted = dict(snap["row"])
    drifted[field] = free["right"][field] if free.get("right") else 0
    snap_repo.upsert_snapshot_row(
        conn,
        pair_id=pair_id,
        side="right",
        align_key=free["align_key"],
        row=drifted,
        updated_at="2026-11-13T00:00:00Z",
    )
    conn.commit()
    conn.close()

    r = client.post(f"/api/dispositions/{tid}/review", json={"approved": True})
    assert r.status_code == 400
    assert "复核不通过" in r.text
    # ticket remains pending_review — nothing closed on a false claim
    assert client.get(f"/api/dispositions/{tid}").json()["status"] == "pending_review"


def test_reinject_validates_agreed_fields(client):
    _run_pair(client, 3)
    diffs = client.get("/api/diffs?pair_id=3").json()
    free = next(d for d in diffs if d["disposition_state"] == "undisposed" and d["kind"] == "field_mismatch")
    tid = client.post("/api/dispositions", json={"diff_id": free["id"]}).json()["id"]
    client.post(f"/api/dispositions/{tid}/claim", json={"assignee": "erin"})

    r = client.post(
        f"/api/dispositions/{tid}/reinject",
        json={"side": "right", "fields": {"nonexistent_field": "x"}},
    )
    assert r.status_code == 400
    r = client.post(
        f"/api/dispositions/{tid}/reinject",
        json={"side": "bogus_side", "fields": {"category": "x"}},
    )
    assert r.status_code == 400
    r = client.post(
        f"/api/dispositions/{tid}/reinject",
        json={"side": "right", "fields": {}},
    )
    assert r.status_code == 400


def test_review_uses_latest_reinject_when_field_corrected_twice(client):
    _run_pair(client, 1)
    free = _first_field_mismatch(client, 1)
    tid = client.post("/api/dispositions", json={"diff_id": free["id"]}).json()["id"]
    client.post(f"/api/dispositions/{tid}/claim", json={"assignee": "gina"})

    # first reinject with a wrong value, then a second one with the correct one
    r = client.post(
        f"/api/dispositions/{tid}/reinject",
        json={"side": "right", "fields": {"qty": 777}},
    )
    assert r.status_code == 200
    r = client.post(
        f"/api/dispositions/{tid}/reinject",
        json={"side": "right", "fields": {"qty": free["left"]["qty"]}},
    )
    assert r.status_code == 200
    assert len(r.json()["reinjects"]) == 2

    client.post(f"/api/dispositions/{tid}/submit-review")
    # review must compare against the LATEST successful write, not the old one
    r = client.post(f"/api/dispositions/{tid}/review", json={"approved": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "closed"
    # the two old/new pairs are both preserved in the ledger as history
    ledger = client.get(f"/api/dispositions/{tid}").json()["reinjects"]
    values = [x["new_values"]["qty"] for x in ledger if x["result"] == "success"]
    assert values == [777, free["left"]["qty"]]


def test_cross_evidence_mutually_consistent(client):
    # self-contained: one happy-path closure and one failed reinject
    _run_pair(client, 2)
    diffs = client.get("/api/diffs?pair_id=2").json()
    mismatch = next(d for d in diffs if d["kind"] == "field_mismatch")

    tid = client.post("/api/dispositions", json={"diff_id": mismatch["id"]}).json()["id"]
    client.post(f"/api/dispositions/{tid}/claim", json={"assignee": "frank"})
    failed = client.post(
        f"/api/dispositions/{tid}/reinject",
        json={"side": "right", "fields": {"amount": 4242}, "simulate_failure": True},
    )
    assert failed.status_code == 422
    ok = client.post(
        f"/api/dispositions/{tid}/reinject",
        json={"side": "right", "fields": {"amount": mismatch["left"]["amount"]}},
    )
    assert ok.status_code == 200
    client.post(f"/api/dispositions/{tid}/submit-review")
    client.post(f"/api/dispositions/{tid}/review", json={"approved": True})

    ev = client.get("/api/dispositions/evidence").json()
    assert ev["all_pass"] is True
    assert sum(ev["status_counts"].values()) == ev["disposed_total"]
    assert (
        ev["diff_total"]
        == ev["disposed_total"] + ev["undisposed_diffs"]
    )
    assert ev["reinject_counts"]["failed"] >= 1
    assert ev["closed_dispositions"] >= 1
    # every snapshot row is traceable to a real pair/side
    for s in ev["side_snapshots"]:
        assert s["pair_code"] and s["side"] in ("left", "right")
        assert isinstance(s["row"], dict)

    dash = client.get("/api/dashboard").json()
    assert dash["open_dispositions"] + dash["closed_dispositions"] == ev["disposed_total"]


def test_evidence_flags_side_snapshot_polluted_by_failed_attempt(client):
    """Legacy/accidental dirty side data from a failure must fail the 互证."""
    _run_pair(client, 1)
    free = _first_field_mismatch(client, 1)
    tid = client.post("/api/dispositions", json={"diff_id": free["id"]}).json()["id"]
    client.post(f"/api/dispositions/{tid}/claim", json={"assignee": "ivy"})
    failed = client.post(
        f"/api/dispositions/{tid}/reinject",
        json={"side": "right", "fields": {"qty": 31337}, "simulate_failure": True},
    )
    assert failed.status_code == 422
    assert not [
        s for s in client.get("/api/side-snapshots").json()
        if s["pair_id"] == 1 and s["side"] == "right" and s["align_key"] == free["align_key"]
    ]

    # simulate a pre-existing polluted snapshot (e.g. written by the old bug)
    from app.db import connect
    from app.repositories import side_snapshots as snap_repo

    conn = connect()
    snap_repo.upsert_snapshot_row(
        conn,
        pair_id=1,
        side="right",
        align_key=free["align_key"],
        row={**free["right"], "qty": 31337},
        updated_at="2026-11-13T00:00:00Z",
    )
    conn.commit()
    conn.close()

    ev = client.get("/api/dispositions/evidence").json()
    fail_check = next(c for c in ev["checks"] if c["name"] == "失败回灌未写入侧快照")
    assert fail_check["pass"] is False
    assert any(p["ticket_id"] == tid for p in fail_check["detail"])
    assert ev["all_pass"] is False
