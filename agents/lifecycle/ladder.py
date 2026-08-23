"""The appeal ladder as enforced structure.

Lifecycle proposes; this module disposes. A model is asked for a rationale and a
sentence a clerk will read, and every structural field of its answer is
overwritten from the static table in ``core.schemas.lifecycle``.

The reason is narrow: a confused model can produce bad prose, which a human
notices and ignores. A confused model that invents a rung of the appeal ladder
produces a case in a state that does not exist, weeks after anyone was watching.
"""

from __future__ import annotations

from core.schemas.case import CaseRecord
from core.schemas.enums import ActionType, AppealLevel, CaseStatus
from core.schemas.lifecycle import APPEAL_LADDER, EscalationDecision, LadderRung


def current_rung(case: CaseRecord) -> LadderRung:
    return APPEAL_LADDER[case.appeal_level]


def next_rung(case: CaseRecord) -> LadderRung | None:
    """The rung after this one, or ``None`` when the ladder is exhausted."""
    following = current_rung(case).next_level
    return APPEAL_LADDER[following] if following else None


def constrain(proposal: EscalationDecision, case: CaseRecord) -> EscalationDecision:
    """Force a proposal onto the ladder, keeping only its prose.

    Two fields survive from the model: ``rationale`` and ``notify_message``.
    Everything else is derived.
    """
    rung = next_rung(case)

    if rung is None:
        return EscalationDecision(
            case_id=case.case_id,
            current_level=case.appeal_level,
            next_level=None,
            next_status=CaseStatus.NEEDS_HUMAN_REVIEW,
            action=ActionType.CLOSE_CASE,
            rationale=(
                proposal.rationale
                or "The appeal ladder is exhausted; independent external review "
                "was the final step available."
            ),
            new_deadline_days=None,
            requires_human=True,
            notify_message=(
                proposal.notify_message
                or f"Case {case.case_id} has exhausted every level of appeal and "
                f"needs a decision from a person."
            ),
        )

    return EscalationDecision(
        case_id=case.case_id,
        current_level=case.appeal_level,
        next_level=rung.level,
        next_status=CaseStatus.ESCALATED,
        action=current_rung(case).action,
        rationale=proposal.rationale or rung.description,
        new_deadline_days=rung.response_window_days,
        # A rung that needs a clinician on a call is not something to do
        # unattended, whatever the model proposed.
        requires_human=rung.level == AppealLevel.PEER_TO_PEER,
        notify_message=(
            proposal.notify_message
            or f"Case {case.case_id}: no response from the payer within the "
            f"{current_rung(case).response_window_days}-day window. "
            f"Escalating to {rung.level.value.replace('_', ' ')}."
        ),
    )
