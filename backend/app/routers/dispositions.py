from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.modules.diff_ticket import DispositionError
from app.services import disposition_service as svc

router = APIRouter(tags=["dispositions"])


class CreateBody(BaseModel):
    diff_id: int
    note: str | None = None


class ClaimBody(BaseModel):
    assignee: str


class ReinjectBody(BaseModel):
    side: str
    fields: dict = Field(default_factory=dict)
    simulate_failure: bool = False


class ReviewBody(BaseModel):
    approved: bool
    reason: str | None = None


def _raise(err: DispositionError) -> None:
    raise HTTPException(err.http_status, err.message) from err


@router.get("/dispositions")
def list_dispositions():
    return svc.list_dispositions()


@router.get("/dispositions/evidence")
def evidence():
    return svc.cross_evidence()


@router.get("/side-snapshots")
def side_snapshots():
    return svc.list_side_snapshots()


@router.get("/dispositions/{ticket_id}")
def get_disposition(ticket_id: int):
    try:
        return svc.get_disposition(ticket_id)
    except DispositionError as e:
        _raise(e)


@router.post("/dispositions")
def create_disposition(body: CreateBody):
    try:
        return svc.create_disposition(body.diff_id, body.note)
    except DispositionError as e:
        _raise(e)


@router.post("/dispositions/{ticket_id}/claim")
def claim_disposition(ticket_id: int, body: ClaimBody):
    try:
        return svc.claim_disposition(ticket_id, body.assignee)
    except DispositionError as e:
        _raise(e)


@router.post("/dispositions/{ticket_id}/reinject")
def reinject_disposition(ticket_id: int, body: ReinjectBody):
    try:
        result = svc.reinject_disposition(
            ticket_id,
            side=body.side,
            fields=body.fields,
            simulate_failure=body.simulate_failure,
        )
    except DispositionError as e:
        _raise(e)
    # The failed write is fully recorded server-side; tell the client the
    # ticket stayed actionable instead of silently pretending success.
    if result.get("write_failed"):
        raise HTTPException(422, result.get("fail_reason", "侧写入失败"))
    return result


@router.post("/dispositions/{ticket_id}/submit-review")
def submit_for_review(ticket_id: int):
    try:
        return svc.submit_for_review(ticket_id)
    except DispositionError as e:
        _raise(e)


@router.post("/dispositions/{ticket_id}/review")
def review_disposition(ticket_id: int, body: ReviewBody):
    try:
        return svc.review_disposition(ticket_id, approved=body.approved, reason=body.reason)
    except DispositionError as e:
        _raise(e)
