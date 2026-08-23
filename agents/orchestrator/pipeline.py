"""The orchestrator.

Agents compute. This persists, transitions, and performs every external effect.
That split is not a preference — ``core.gateway.POLICY`` gives Sentinel and
Verification read-only access to cases and gives only this component the right
to claim actions, so the separation is structural.

The orchestrator is also the one component in the fleet that makes no model call
and consumes no untrusted input. It is deterministic Python routing typed
contracts between agents, and that property is what earns it the broadest grant
in the access matrix. There is a test that holds it to that.

The shape of a run is important and easy to misread: ``ingest`` does not wait at
the human gate. It reaches ``awaiting_human_approval`` and returns, and the
Cloud Run instance dies. Weeks may pass. The case is a document, and the only
thing that resumes it is a person opening the approval interface or the
scheduler noticing a deadline.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from agents.drafting.brief import NothingToArgue, build_brief
from agents.intake.agent import IntakeRequest
from agents.intake.documents import SourceDocument, extract_text_layer
from agents.lifecycle.agent import LifecycleRequest
from agents.mapping.agent import MappingRequest
from agents.mapping.charts import ChartNotFound, load_chart
from agents.orchestrator import effects
from agents.orchestrator.deps import Fleet
from agents.retrieval.agent import RetrievalRequest
from agents.sentinel.agent import ScreeningRequest
from agents.verification.agent import VerificationRequest
from core.audit import content_digest
from core.schemas.base import utcnow
from core.schemas.case import CaseRecord
from core.schemas.enums import ActionType, AgentName, CaseStatus
from core.schemas.lifecycle import APPEAL_LADDER
from core.telemetry import agent_span

logger = logging.getLogger(__name__)


class Pipeline:
    """Drives a case from an inbound document to a decision."""

    def __init__(self, fleet: Fleet) -> None:
        self.fleet = fleet
        self.settings = fleet.settings

    # -- entry points -------------------------------------------------------- #

    def ingest(self, document: SourceDocument, case_id: str | None = None) -> CaseRecord:
        """Run a document through to the human gate, or to a terminal state."""
        digest = content_digest(document.data)
        case_id = case_id or self._derive_case_id(document, digest)

        with agent_span("orchestrator", case_id, "ingest"):
            case, created = self.fleet.cases.create_or_get(
                CaseRecord(
                    case_id=case_id,
                    source_document_uri=document.uri,
                    source_sha256=digest,
                )
            )
            if not created and case.status is not CaseStatus.RECEIVED:
                # A redelivery of a document already worked on. Returning the
                # case as-is is the correct response, not an error.
                logger.info("case %s already at %s; nothing to do", case_id, case.status.value)
                return case

            return self._run(case_id, document, digest)

    def escalate_overdue(self, limit: int = 50) -> list[CaseRecord]:
        """What the scheduled job calls. Never runs in the request path."""
        overdue = self.fleet.cases.find_overdue(limit=limit)
        logger.info("scheduler tick: %d overdue case(s)", len(overdue))
        return [self._escalate_one(case) for case in overdue]

    # -- the pipeline -------------------------------------------------------- #

    def _run(self, case_id: str, document: SourceDocument, digest: str) -> CaseRecord:
        # --- Sentinel ------------------------------------------------------- #
        self._advance(case_id, CaseStatus.SCREENING)
        text = extract_text_layer(document)
        screening = self.fleet.sentinel.run(
            case_id,
            ScreeningRequest(
                document_uri=document.uri,
                content=document.data,
                mime_type=document.mime_type,
                extracted_text=text,
            ),
        )

        if screening.quarantine:
            case = self._advance(
                case_id,
                CaseStatus.QUARANTINED,
                attach=lambda c: setattr(c, "screening", screening),
                note=f"{len(screening.findings)} threat finding(s)",
            )
            self._act(
                case_id,
                ActionType.QUARANTINE_DOCUMENT,
                {"sha": digest},
                lambda: effects.quarantine_document(self.fleet, case_id, screening),
            )
            self._notify(case_id, "Inbound document quarantined; it was not processed.")
            return case

        # --- Intake --------------------------------------------------------- #
        denial = self.fleet.intake.run(
            case_id, IntakeRequest(document=document, screening=screening)
        )
        self._advance(
            case_id,
            CaseStatus.EXTRACTED,
            attach=lambda c: (
                setattr(c, "screening", screening),
                setattr(c, "denial", denial),
            ),
        )

        # --- Retrieval ------------------------------------------------------ #
        self._advance(case_id, CaseStatus.RETRIEVING)
        retrieval = self.fleet.retrieval.run(case_id, RetrievalRequest(denial=denial))
        if retrieval.no_applicable_policy:
            case = self._advance(
                case_id,
                CaseStatus.DECLINED_NO_BASIS,
                attach=lambda c: setattr(c, "retrieval", retrieval),
                note="no policy in the corpus governs this denial",
            )
            self._notify(
                case_id,
                "No published policy in the corpus governs this denial, so there is "
                "no criterion to argue against. Declining to appeal.",
            )
            return case

        # --- Mapping -------------------------------------------------------- #
        self._advance(
            case_id, CaseStatus.MAPPING, attach=lambda c: setattr(c, "retrieval", retrieval)
        )
        try:
            chart = load_chart(case_id)
        except ChartNotFound as exc:
            return self._fail(case_id, f"no chart available: {exc}")

        matrix = self.fleet.mapping.run(case_id, MappingRequest(chart=chart, retrieval=retrieval))
        if not matrix.has_appealable_basis:
            case = self._advance(
                case_id,
                CaseStatus.DECLINED_NO_BASIS,
                attach=lambda c: setattr(c, "criteria", matrix),
                note=f"only {matrix.satisfied_count} criteria documented",
            )
            self._notify(
                case_id,
                f"The chart documents {matrix.satisfied_count} of "
                f"{len(matrix.verdicts)} criteria. That is not a basis for an "
                f"appeal; the gap is in the documentation, not the determination.",
            )
            return case

        # --- Drafting and Verification -------------------------------------- #
        self._advance(case_id, CaseStatus.DRAFTING, attach=lambda c: setattr(c, "criteria", matrix))
        return self._draft_verify_loop(case_id)

    def _draft_verify_loop(self, case_id: str) -> CaseRecord:
        """Write, check, and rewrite until it passes or the budget is spent.

        The cap is what turns "an agent that loops" from an outage into a case
        on a human's desk. The failure text from each rejection is fed forward;
        telling a writer only that it failed produces the same draft again.
        """
        instructions: list[str] = []
        max_attempts = self.settings.max_verification_attempts

        with agent_span("orchestrator", case_id, "draft_verify_loop"):
            for attempt in range(1, max_attempts + 1):
                case = self.fleet.cases.load(case_id)
                try:
                    brief = build_brief(case, instructions)
                except NothingToArgue as exc:
                    return self._advance(case_id, CaseStatus.DECLINED_NO_BASIS, note=str(exc))

                draft = self.fleet.drafting.run(case_id, brief, attempt=attempt)
                self._advance(
                    case_id,
                    CaseStatus.VERIFYING,
                    attach=lambda c, d=draft: (
                        c.drafts.append(d)
                        if not any(x.attempt == d.attempt for x in c.drafts)
                        else None
                    ),
                )

                case = self.fleet.cases.load(case_id)
                result = self.fleet.verification.run(
                    case_id,
                    VerificationRequest(
                        draft=draft, retrieval=case.retrieval, matrix=case.criteria
                    ),
                    attempt=attempt,
                )

                if result.passed:
                    case = self._advance(
                        case_id,
                        CaseStatus.AWAITING_APPROVAL,
                        attach=lambda c, r=result: c.verifications.append(r),
                        note=f"verified on attempt {attempt}",
                    )
                    self._notify(
                        case_id,
                        f"Appeal ready for review. {result.citations_checked} citation(s) "
                        f"verified against the policy text"
                        + (f" after {attempt - 1} rejected draft(s)." if attempt > 1 else "."),
                    )
                    return case

                instructions = result.revision_instructions()
                self._advance(
                    case_id,
                    CaseStatus.DRAFTING,
                    attach=lambda c, r=result: c.verifications.append(r),
                    note=f"attempt {attempt} rejected: {len(result.findings)} finding(s)",
                )
                logger.warning(
                    "case %s attempt %d rejected by verification: %s",
                    case_id,
                    attempt,
                    "; ".join(instructions[:2]),
                )

        case = self._advance(
            case_id,
            CaseStatus.NEEDS_HUMAN_REVIEW,
            attach=lambda c: setattr(
                c,
                "needs_human_reason",
                f"verification rejected all {max_attempts} drafting attempts",
            ),
        )
        self._notify(
            case_id,
            f"Verification rejected all {max_attempts} drafts. Nothing was sent. "
            f"This case needs a person.",
        )
        return case

    # -- the human gate ------------------------------------------------------ #

    def try_submit(self, case_id: str) -> CaseRecord:
        """Submit, but only when every required signature is present.

        Called after either signature lands, so the order the clerk and the
        clinician sign in does not matter. ``ready_to_submit`` is read, never
        recomputed here, so there is exactly one definition of what is enough.
        """
        case = self.fleet.cases.load(case_id)
        if case.status is not CaseStatus.APPROVED or not case.ready_to_submit:
            return case

        draft = case.approved_draft()
        if draft is None:
            return self._fail(case_id, "approved case has no identifiable draft")

        outcome = self._act(
            case_id,
            ActionType.SUBMIT_APPEAL,
            {"draft_sha": content_digest(draft.to_firestore())},
            lambda: effects.submit_appeal(self.fleet, case_id, draft),
            attempt=case.escalation_count + 1,
        )

        window = APPEAL_LADDER[case.appeal_level].response_window_days
        accel = self.settings.demo_seconds_per_day if self.settings.demo_time_acceleration else None
        return self._advance(
            case_id,
            CaseStatus.SUBMITTED,
            attach=lambda c: (
                setattr(c, "submitted_at", utcnow()),
                c.set_response_deadline(window, accel),
            ),
            note=f"confirmation {(outcome.result or {}).get('confirmation', 'unknown')}",
        )

    # -- escalation ---------------------------------------------------------- #

    def _escalate_one(self, case: CaseRecord) -> CaseRecord:
        decision = self.fleet.lifecycle.run(
            case.case_id, LifecycleRequest(case=case), attempt=case.escalation_count + 1
        )

        if decision.halts_ladder:
            updated = self._advance(
                case.case_id,
                CaseStatus.NEEDS_HUMAN_REVIEW,
                attach=lambda c: setattr(c, "needs_human_reason", decision.rationale),
                note=decision.action.value,
            )
            self._notify(case.case_id, decision.notify_message)
            return updated

        self._act(
            case.case_id,
            decision.action,
            {"level": decision.next_level.value if decision.next_level else "closed"},
            lambda: effects.escalate(self.fleet, case.case_id, decision),
            attempt=case.escalation_count + 1,
        )

        accel = self.settings.demo_seconds_per_day if self.settings.demo_time_acceleration else None

        # Two scheduler ticks can overlap on one overdue case. The guard above
        # already stops the external effect from happening twice, but a caller
        # arriving after the first has completed gets a *replay* rather than an
        # ActionInFlight — it carries on and would advance the case a second
        # rung for a single escalation.
        #
        # Pinning to the count observed before the action makes the state change
        # idempotent too, so the loser of the race finds the case already moved
        # and leaves it alone. Ordinarily the new deadline hides this, because a
        # freshly escalated case stops being overdue; under demo acceleration
        # the window is seconds and it does not hide it at all.
        observed_count = case.escalation_count

        def apply(current: CaseRecord) -> None:
            if current.escalation_count != observed_count:
                return  # another tick already advanced this case
            current.appeal_level = decision.next_level or current.appeal_level
            current.escalation_count = observed_count + 1
            current.submitted_at = utcnow()
            current.set_response_deadline(decision.new_deadline_days or 30, accel)
            if decision.requires_human:
                # Someone has to schedule the call. The clock keeps running so
                # that not scheduling it is not the same as the case dying.
                current.needs_human_reason = decision.rationale

        updated = self._advance(
            case.case_id,
            decision.next_status,
            attach=apply,
            note=decision.rationale[:200],
        )
        self._notify(case.case_id, decision.notify_message)
        return updated

    # -- helpers ------------------------------------------------------------- #

    def _advance(
        self,
        case_id: str,
        to: CaseStatus,
        attach: Callable[[CaseRecord], object] | None = None,
        note: str | None = None,
    ) -> CaseRecord:
        """Attach an agent's output and transition, in one write.

        ``attach`` may be replayed by the optimistic-locking retry, so it must be
        an idempotent assignment. Appends guard on the item not already being
        present for exactly that reason.
        """

        def change(case: CaseRecord) -> None:
            if attach is not None:
                attach(case)
            case.transition(to, actor=AgentName.ORCHESTRATOR.value, note=note)

        return self.fleet.cases.mutate(case_id, change)

    def _act(
        self,
        case_id: str,
        action: ActionType,
        payload: dict,
        fn: Callable[[], object],
        attempt: int = 1,
    ):
        return self.fleet.guard.execute(case_id, action, payload, fn, attempt=attempt)

    def _notify(self, case_id: str, reason: str) -> None:
        self._act(
            case_id,
            ActionType.NOTIFY_HUMAN,
            {"reason": reason},
            lambda: effects.notify_human(self.fleet, case_id, reason),
            attempt=self._notification_attempt(case_id),
        )

    def _notification_attempt(self, case_id: str) -> int:
        """Distinct notifications about one case are distinct actions.

        Keyed on how many have already been sent, so 'ready for review' and
        'escalated to peer review' do not collide on one action key and get
        silently deduplicated into a single message.
        """
        existing = self.fleet.store.query("actions", where=[("case_id", "==", case_id)])
        return 1 + sum(
            1 for _, row in existing if row.get("action_type") == ActionType.NOTIFY_HUMAN.value
        )

    def _fail(self, case_id: str, reason: str) -> CaseRecord:
        logger.error("case %s failed: %s", case_id, reason)
        return self._advance(
            case_id,
            CaseStatus.FAILED,
            attach=lambda c: (
                setattr(c, "last_error", reason),
                setattr(c, "failure_count", c.failure_count + 1),
            ),
        )

    @staticmethod
    def _derive_case_id(document: SourceDocument, digest: str) -> str:
        """Deterministic from the document, so a redelivery hits the same case."""
        stem = document.uri.rstrip("/").split("/")[-1].split(".")[0]
        return stem if stem.startswith("CASE-") else f"CASE-{digest[:10]}"
