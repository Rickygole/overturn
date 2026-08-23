"""Lifecycle agent contracts.

Lifecycle never runs in the request path. It wakes on a schedule, looks at cases
whose payer response window has elapsed, and decides which rung of the appeal
ladder comes next.
"""

from __future__ import annotations

from pydantic import Field

from core.schemas.base import OverturnModel
from core.schemas.enums import ActionType, AppealLevel, CaseStatus


class EscalationDecision(OverturnModel):
    """What to do about one overdue case."""

    case_id: str
    current_level: AppealLevel
    next_level: AppealLevel | None = Field(
        default=None, description="None when the ladder is exhausted."
    )
    next_status: CaseStatus
    action: ActionType

    rationale: str = Field(
        description="Why this rung, in terms of the case history and the payer's window."
    )
    new_deadline_days: int | None = Field(
        default=None, description="Response window for the new level."
    )
    requires_human: bool = Field(
        default=False,
        description="A person has something to do at this rung — scheduling the "
        "peer-to-peer call, for instance. It does NOT stop the clock: the case "
        "still advances and still carries a deadline, so if nobody acts the "
        "ladder keeps climbing on its own.",
    )
    halts_ladder: bool = Field(
        default=False,
        description="No rung remains. This is the only thing that stops a case "
        "advancing, and it is why it is a separate field from requires_human. "
        "Conflating the two meant a case that needed a human at the peer-to-peer "
        "rung stopped dead there and never escalated again — which is the exact "
        "failure the unattended lifecycle exists to prevent.",
    )
    notify_message: str = Field(description="One line the billing clerk will actually read.")


class LadderRung(OverturnModel):
    """A static description of one step of the appeal ladder."""

    level: AppealLevel
    response_window_days: int
    next_level: AppealLevel | None
    action: ActionType
    description: str


# The ladder is deliberately data rather than branching code: Lifecycle proposes
# a rung and this table constrains what is legal, so a confused model cannot
# invent a step that does not exist.
APPEAL_LADDER: dict[AppealLevel, LadderRung] = {
    AppealLevel.FIRST_LEVEL: LadderRung(
        level=AppealLevel.FIRST_LEVEL,
        response_window_days=30,
        next_level=AppealLevel.PEER_TO_PEER,
        action=ActionType.REQUEST_PEER_REVIEW,
        description="Standard written appeal. If the payer is silent past the window, "
        "request a peer-to-peer conversation.",
    ),
    AppealLevel.PEER_TO_PEER: LadderRung(
        level=AppealLevel.PEER_TO_PEER,
        response_window_days=14,
        next_level=AppealLevel.SECOND_LEVEL,
        action=ActionType.ESCALATE,
        description="Clinician-to-clinician review. If unscheduled or unfavourable, "
        "file a second-level appeal.",
    ),
    AppealLevel.SECOND_LEVEL: LadderRung(
        level=AppealLevel.SECOND_LEVEL,
        response_window_days=30,
        next_level=AppealLevel.EXTERNAL_REVIEW,
        action=ActionType.FILE_EXTERNAL_REVIEW,
        description="Second-level internal appeal. On denial, the case becomes eligible "
        "for independent external review.",
    ),
    AppealLevel.EXTERNAL_REVIEW: LadderRung(
        level=AppealLevel.EXTERNAL_REVIEW,
        response_window_days=45,
        next_level=None,
        action=ActionType.CLOSE_CASE,
        description="Independent external review. This is the last rung; the outcome "
        "is binding and the case closes either way.",
    ),
}
