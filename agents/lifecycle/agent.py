"""The Lifecycle agent.

Never runs in the request path. A scheduled job wakes it, hands it a case whose
payer response window has elapsed, and it says what happens next.

This is where the multi-week claim is actually cashed. Between an appeal going
out and this agent running, no process exists. The case is a document. The only
reason this works is that ``CaseRecord.is_overdue`` is a pure function of stored
state, so a worker that has never seen the case can evaluate it correctly.
"""

from __future__ import annotations

from dataclasses import dataclass

from agents.base import OverturnAgent
from agents.lifecycle.ladder import constrain, current_rung, next_rung
from core.audit import Recording
from core.schemas.case import CaseRecord
from core.schemas.enums import AgentName
from core.schemas.lifecycle import EscalationDecision

from .prompts import LIFECYCLE_SYSTEM


@dataclass(frozen=True)
class LifecycleRequest:
    """One overdue case."""

    case: CaseRecord

    def to_firestore(self) -> dict:
        return {
            "case_id": self.case.case_id,
            "appeal_level": self.case.appeal_level.value,
            "escalation_count": self.case.escalation_count,
            "deadline": self.case.response_deadline.isoformat()
            if self.case.response_deadline
            else None,
        }


class LifecycleAgent(OverturnAgent[LifecycleRequest, EscalationDecision]):
    """Decides what happens to an appeal the payer has not answered."""

    name = AgentName.LIFECYCLE
    operation = "decide"

    def _summarise(self, request: LifecycleRequest) -> str:
        return (
            f"level {request.case.appeal_level.value}, "
            f"escalation {request.case.escalation_count}, "
            f"overdue since {request.case.response_deadline}"
        )

    def _execute(
        self,
        case_id: str,
        request: LifecycleRequest,
        rec: Recording,
        attempt: int,
    ) -> EscalationDecision:
        case = request.case
        rung = next_rung(case)

        proposal: EscalationDecision | None = None
        try:
            proposal, response = self.llm.structured(
                agent=self.name.value,
                operation=self.operation,
                system=LIFECYCLE_SYSTEM,
                prompt=self._render_prompt(case),
                schema=EscalationDecision,
                model=self.settings.model_flash,
            )
            rec.model = response.model
            rec.input_tokens = response.input_tokens
            rec.output_tokens = response.output_tokens
        except Exception as exc:
            # The ladder is a table. Losing the prose is a degraded notification,
            # not a stalled case, and a case that stalls silently for weeks is
            # much worse than one that escalates with a terse message.
            rec.extra["model_unavailable"] = f"{type(exc).__name__}: {exc}"[:200]
            proposal = EscalationDecision(
                case_id=case.case_id,
                current_level=case.appeal_level,
                next_status=case.status,
                action=current_rung(case).action,
                rationale="",
                notify_message="",
            )

        decision = constrain(proposal, case)
        rec.decision = (
            f"{case.appeal_level.value} -> "
            f"{decision.next_level.value if decision.next_level else 'ladder exhausted'}"
            f" via {decision.action.value}"
            + (f"; new window {decision.new_deadline_days} days" if rung else "")
            + ("; needs a person" if decision.requires_human else "")
        )
        return decision

    @staticmethod
    def _render_prompt(case: CaseRecord) -> str:
        rung = current_rung(case)
        following = next_rung(case)
        history = "\n".join(
            f"  {h.at.date()}  {h.to_status.value:26} by {h.actor}" for h in case.history[-8:]
        )
        responses = (
            "\n".join(f"  {r.received_at.date()}  {r.outcome}" for r in case.payer_responses)
            or "  (none — the payer has not responded at any level)"
        )
        return (
            f"CASE {case.case_id}\n"
            f"Current stage: {rung.level.value} ({rung.description})\n"
            f"Submitted: {case.submitted_at.date() if case.submitted_at else 'unknown'}\n"
            f"Response window: {rung.response_window_days} days, "
            f"closed {case.response_deadline.date() if case.response_deadline else 'unknown'}\n"
            f"Prior escalations: {case.escalation_count}\n\n"
            f"Case history:\n{history}\n\n"
            f"Payer responses:\n{responses}\n\n"
            f"Next stage, already determined: "
            f"{following.level.value if following else 'none — the ladder is exhausted'}\n\n"
            f"Write the rationale and the notification."
        )
