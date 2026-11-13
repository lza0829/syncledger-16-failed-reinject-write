"""Difference disposition state machine and domain rules.

Not a customer-service ticket product — every disposition is bound to one
reconcile diff and drives a write-back (reinject) into a side snapshot that
later reconciliations actually read.

Lifecycle::

    pending_claim --claim------------> in_progress
    in_progress   --submit_review----> pending_review   (only after a successful reinject)
    pending_review --review_approve--> closed
    pending_review --review_reject---> in_progress      (reason mandatory)

Every other transition is rejected. A ticket can never reach `closed`
without passing review, and a failed reinject leaves the ticket in
`in_progress` rather than moving it to `pending_review`.
"""

from __future__ import annotations

from enum import Enum


class DispositionStatus(str, Enum):
    PENDING_CLAIM = "pending_claim"
    IN_PROGRESS = "in_progress"
    PENDING_REVIEW = "pending_review"
    CLOSED = "closed"


# Chinese labels rendered by the UI; the API always exchanges the raw codes.
STATUS_LABELS: dict[str, str] = {
    DispositionStatus.PENDING_CLAIM.value: "待认领",
    DispositionStatus.IN_PROGRESS.value: "处理中",
    DispositionStatus.PENDING_REVIEW.value: "待复核",
    DispositionStatus.CLOSED.value: "已关闭",
}


class Action(str, Enum):
    CREATE = "create"
    CLAIM = "claim"
    REINJECT = "reinject"
    SUBMIT_REVIEW = "submit_review"
    REVIEW_APPROVE = "review_approve"
    REVIEW_REJECT = "review_reject"


# action -> {from_status: to_status}
TRANSITIONS: dict[str, dict[str, str]] = {
    Action.CLAIM.value: {
        DispositionStatus.PENDING_CLAIM.value: DispositionStatus.IN_PROGRESS.value,
    },
    Action.SUBMIT_REVIEW.value: {
        DispositionStatus.IN_PROGRESS.value: DispositionStatus.PENDING_REVIEW.value,
    },
    Action.REVIEW_APPROVE.value: {
        DispositionStatus.PENDING_REVIEW.value: DispositionStatus.CLOSED.value,
    },
    Action.REVIEW_REJECT.value: {
        DispositionStatus.PENDING_REVIEW.value: DispositionStatus.IN_PROGRESS.value,
    },
}


class DispositionError(Exception):
    """Domain violation — mapped to an HTTP error by the router layer."""

    def __init__(self, message: str, *, http_status: int = 400):
        super().__init__(message)
        self.message = message
        self.http_status = http_status


def can_transition(action: str, current_status: str) -> bool:
    return current_status in TRANSITIONS.get(action, {})


def next_status(action: str, current_status: str) -> str:
    """Return the target status for `action` or raise on an illegal jump."""
    table = TRANSITIONS.get(action)
    if table is None:
        raise DispositionError(f"未知处置动作: {action}")
    target = table.get(current_status)
    if target is None:
        raise DispositionError(
            f"非法状态跳转: 动作 {action} 不允许从 {current_status} 发起"
        )
    return target


# Statuses that count as "still actionable / not closed" on the diff list.
OPEN_STATUSES = (
    DispositionStatus.PENDING_CLAIM.value,
    DispositionStatus.IN_PROGRESS.value,
    DispositionStatus.PENDING_REVIEW.value,
)
