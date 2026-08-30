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
from datetime import timedelta

from agents.drafting.brief import NothingToArgue, build_brief
from agents.intake.agent import IntakeRequest
from agents.intake.documents import SourceDocument, extract_text_layer
from agents.lifecycle.agent import LifecycleRequest
from agents.mapping.agent import MappingRequest
from agents.mapping.charts import ChartNotFound, load_chart
from agents.mapping.dispute import has_answerable_dispute, primary_disputed_criteria
from agents.orchestrator import effects
from agents.orchestrator.deps import Fleet
from agents.retrieval.agent import RetrievalRequest
from agents.sentinel.agent import ScreeningRequest
from agents.verification.agent import VerificationRequest
from core.audit import content_digest
from core.idempotency import ActionPreviouslyFailed, UnsafeToRetry
from core.schemas.base import utcnow
from core.schemas.case import CaseRecord
from core.schemas.enums import ActionType, AgentName, CaseStatus
from core.schemas.lifecycle import APPEAL_LADDER
from core.telemetry import agent_span

logger = logging.getLogger(__name__)

# Statuses where a person is already the next actor: a clerk hasn't looked at
# the draft yet, or a case landed in front of one because something upstream
# gave up on it. Both sit in a queue a human already knows to check, which is
# what distinguishes them from IN_FLIGHT_STATUSES below.
AWAITING_HUMAN_STATUSES: frozenset[CaseStatus] = frozenset(
    {CaseStatus.AWAITING_APPROVAL, CaseStatus.NEEDS_HUMAN_REVIEW}
)

# Every status that is neither terminal, nor waiting on the payer, nor waiting
# on a person — meaning the only thing that is supposed to move it forward is
# this pipeline (or, for ``approved``/``payer_responded``, a human action this
# pipeline can't manufacture by re-processing a document). This is exactly the
# set the red team's bug report named: none of these are visible to
# ``find_overdue``, an approval queue, or a needs-human queue, so a worker that
# dies while a case sits in one of them leaves it with no path back to a human
# or a machine. ``find_stalled`` below is what makes them visible again, and
# the coverage test in tests/test_resumption.py is what keeps a newly added
# ``CaseStatus`` from silently becoming a tenth one.
IN_FLIGHT_STATUSES: frozenset[CaseStatus] = frozenset(
    {
        CaseStatus.RECEIVED,
        CaseStatus.SCREENING,
        CaseStatus.EXTRACTED,
        CaseStatus.RETRIEVING,
        CaseStatus.MAPPING,
        CaseStatus.DRAFTING,
        CaseStatus.VERIFYING,
        CaseStatus.APPROVED,
        CaseStatus.PAYER_RESPONDED,
    }
)

# A case whose crash is deterministic — a chart the loader will never find, a
# brief that always raises — must not redeliver forever. Three tries is enough
# to rule out a one-off (a preempted worker, a cold-started dependency) without
# turning a poison message into an infinite loop of the same failure.
MAX_RESUME_ATTEMPTS = 3

# Same reasoning, for the payer call specifically: a network blip is worth a
# couple of deliberate retries, a payer endpoint that is genuinely down is not
# something hammering it a fourth time will fix.
MAX_TRANSMISSION_ATTEMPTS = 3


class Pipeline:
    """Drives a case from an inbound document to a decision."""

    def __init__(self, fleet: Fleet) -> None:
        self.fleet = fleet
        self.settings = fleet.settings

    # -- entry points -------------------------------------------------------- #

    def ingest(self, document: SourceDocument, case_id: str | None = None) -> CaseRecord:
        """Run a document through to the human gate, or to a terminal state.

        Pub/Sub delivers at least once, so this has to be safe to call twice for
        one document. Historically "safe" meant "return the existing case
        untouched" for anything past ``received`` — correct for a case that
        finished a stage cleanly, and silently wrong for one whose worker died
        mid-stage. There is no separate status to tell those two apart, so
        redelivery is also the mechanism that resumes a crashed case: see
        ``_RESUME_STAGES``.
        """
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
            if created or case.status is CaseStatus.RECEIVED:
                return self._attempt(case_id, case, lambda: self._run(case_id, document, digest))

            resume = _RESUME_STAGES.get(case.status)
            if resume is None:
                # Either genuinely finished (terminal, awaiting the payer),
                # waiting on a person (the approval queue, needs-human review),
                # or one of the two statuses — approved, payer_responded — this
                # pipeline has no document-processing work to redo for. Those
                # two are still visible; see IN_FLIGHT_STATUSES and
                # find_stalled, which is what notices them instead.
                logger.info("case %s already at %s; nothing to do", case_id, case.status.value)
                return case

            return self._attempt(case_id, case, lambda: resume(self, case_id, document, case))

    def escalate_overdue(self, limit: int = 50) -> list[CaseRecord]:
        """What the scheduled job calls. Never runs in the request path."""
        overdue = self.fleet.cases.find_overdue(limit=limit)
        logger.info("scheduler tick: %d overdue case(s)", len(overdue))
        return [self._escalate_one(case) for case in overdue]

    def find_stalled(self, older_than_minutes: int, limit: int | None = None) -> list[CaseRecord]:
        """Cases parked in an in-flight status well past when a worker would be on them.

        Redelivery resumes a crashed case, but nothing redelivers a message
        whose subscription has already acked — and the ack for a stage happens
        before the next one starts, not after the whole case finishes, so the
        message for the stage that was running when the worker died is gone
        for good. Only a sweep over how long a case has sat untouched finds
        that one. This is the query the scheduled job runs to do it.
        """
        cutoff = utcnow() - timedelta(minutes=older_than_minutes)
        stalled = [
            case
            for status in IN_FLIGHT_STATUSES
            for case in self.fleet.cases.find_by_status(status)
            if case.updated_at <= cutoff
        ]
        stalled.sort(key=lambda c: c.updated_at)
        return stalled[:limit] if limit else stalled

    # -- resuming a crashed case ----------------------------------------------- #

    def _attempt(
        self, case_id: str, case: CaseRecord, stage: Callable[[], CaseRecord]
    ) -> CaseRecord:
        """Run one stage (fresh or resumed), and stop a deterministic crash from looping.

        ``stage`` may raise — a preempted worker, a flaky dependency, a chart
        that will never load. The first ``MAX_RESUME_ATTEMPTS`` times, the
        exception is left to propagate, which is what tells the caller (Pub/Sub,
        a test) that this delivery failed and should be retried. Past that, the
        failure is deterministic enough that retrying will not help, and the
        case is routed to a person instead of redelivering forever.
        """
        try:
            return stage()
        except Exception as exc:
            failed = self.fleet.cases.mutate(
                case_id, lambda c: setattr(c, "failure_count", c.failure_count + 1)
            )
            if failed.failure_count < MAX_RESUME_ATTEMPTS:
                logger.warning(
                    "case %s failed at %s (attempt %d/%d): %s",
                    case_id,
                    case.status.value,
                    failed.failure_count,
                    MAX_RESUME_ATTEMPTS,
                    exc,
                )
                raise
            logger.error(
                "case %s failed %d times at %s; routing to a human: %s",
                case_id,
                failed.failure_count,
                case.status.value,
                exc,
            )
            reason = (
                f"crashed {failed.failure_count} times while resuming from "
                f"{case.status.value}: {exc}"
            )
            return self._advance(
                case_id,
                CaseStatus.NEEDS_HUMAN_REVIEW,
                attach=lambda c, reason=reason: setattr(c, "needs_human_reason", reason),
                note="poison message: resume attempts exhausted",
            )

    def _resume_screening(
        self, case_id: str, document: SourceDocument, case: CaseRecord
    ) -> CaseRecord:
        """Sentinel's result is never committed before ``extracted``, so there is
        nothing to skip — this is exactly the top of a fresh run."""
        return self._run_screening(case_id, document, content_digest(document.data))

    def _resume_retrieval(
        self, case_id: str, document: SourceDocument, case: CaseRecord
    ) -> CaseRecord:
        """Screening and Intake already committed their output at ``extracted``.
        Re-running Intake here would burn a model call to reproduce it."""
        return self._run_retrieval(case_id, case.denial)

    def _resume_mapping(
        self, case_id: str, document: SourceDocument, case: CaseRecord
    ) -> CaseRecord:
        """Retrieval's result is attached at the ``mapping`` transition itself,
        so it survives the crash even though Mapping's own output does not."""
        return self._run_mapping(case_id, case.denial, case.retrieval)

    def _resume_drafting(
        self, case_id: str, document: SourceDocument, case: CaseRecord
    ) -> CaseRecord:
        """Continue the loop at the next attempt, not attempt one.

        A crash at ``drafting`` means either no draft has been written yet, or
        the most recent one was just rejected and the loop looped back here.
        ``len(case.drafts) + 1`` is the right next attempt either way, and the
        rejected verification's feedback is fed forward exactly as it would
        have been without the crash — otherwise the resumed attempt repeats the
        mistake the crash happened to interrupt.
        """
        start_attempt = len(case.drafts) + 1
        instructions = self._pending_instructions(case, start_attempt)
        return self._draft_verify_loop(
            case_id, start_attempt=start_attempt, instructions=instructions
        )

    def _resume_verifying(
        self, case_id: str, document: SourceDocument, case: CaseRecord
    ) -> CaseRecord:
        """The draft for this attempt is already attached; only Verification's
        call was lost. Re-running Drafting would append a duplicate attempt."""
        draft = case.latest_draft
        if draft is None:
            # Should not happen — ``verifying`` is only reached with a draft
            # attached — but a missing draft is not a reason to raise here.
            # Falling back to a fresh drafting attempt is still correct.
            return self._resume_drafting(case_id, document, case)
        instructions = self._pending_instructions(case, draft.attempt)
        return self._draft_verify_loop(
            case_id,
            start_attempt=draft.attempt,
            instructions=instructions,
            pending_draft=draft,
        )

    @staticmethod
    def _pending_instructions(case: CaseRecord, attempt: int) -> list[str]:
        """Revision feedback for ``attempt``, if the previous one earned any.

        Only applies when the last verification on record is for the attempt
        immediately before this one — i.e. the loop rejected it and looped
        back. A fresh case, or one resuming its very first attempt, has none.
        """
        verification = case.latest_verification
        if verification is not None and len(case.verifications) == attempt - 1:
            return verification.revision_instructions()
        return []

    # -- the pipeline ---------------------------------------------------------- #

    def _run(self, case_id: str, document: SourceDocument, digest: str) -> CaseRecord:
        return self._run_screening(case_id, document, digest)

    def _run_screening(self, case_id: str, document: SourceDocument, digest: str) -> CaseRecord:
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

        return self._run_intake(case_id, document, screening)

    def _run_intake(self, case_id: str, document: SourceDocument, screening) -> CaseRecord:
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
        return self._run_retrieval(case_id, denial)

    def _run_retrieval(self, case_id: str, denial) -> CaseRecord:
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
        return self._run_mapping(case_id, denial, retrieval)

    def _run_mapping(self, case_id: str, denial, retrieval) -> CaseRecord:
        # --- Mapping -------------------------------------------------------- #
        self._advance(
            case_id, CaseStatus.MAPPING, attach=lambda c: setattr(c, "retrieval", retrieval)
        )
        try:
            chart = load_chart(case_id)
        except ChartNotFound as exc:
            return self._fail(case_id, f"no chart available: {exc}")

        matrix = self.fleet.mapping.run(
            case_id, MappingRequest(chart=chart, retrieval=retrieval, denial=denial)
        )
        # Whether there is an appeal here is not a question of how many criteria
        # are documented. It is whether the chart answers the one the payer
        # actually asked. A case can document seven criteria beautifully and
        # still be hopeless if the eighth is the one that was denied on.
        disputed = primary_disputed_criteria(denial, retrieval)
        answerable, explanation = has_answerable_dispute(matrix, disputed)
        if not answerable:
            case = self._advance(
                case_id,
                CaseStatus.DECLINED_NO_BASIS,
                attach=lambda c: (
                    setattr(c, "criteria", matrix),
                    setattr(c, "needs_human_reason", explanation),
                ),
                note=f"payer's reason turns on {', '.join(disputed[:2]) or 'unclear criteria'}",
            )
            self._notify(case_id, explanation)
            return case

        # --- Drafting and Verification -------------------------------------- #
        self._advance(case_id, CaseStatus.DRAFTING, attach=lambda c: setattr(c, "criteria", matrix))
        return self._draft_verify_loop(case_id)

    def _draft_verify_loop(
        self,
        case_id: str,
        start_attempt: int = 1,
        instructions: list[str] | None = None,
        pending_draft=None,
    ) -> CaseRecord:
        """Write, check, and rewrite until it passes or the budget is spent.

        The cap is what turns "an agent that loops" from an outage into a case
        on a human's desk. The failure text from each rejection is fed forward;
        telling a writer only that it failed produces the same draft again.

        ``start_attempt``, ``instructions`` and ``pending_draft`` exist for
        resumption. A fresh case always calls this with the defaults, which
        reproduce the original attempt-one-through-the-cap loop exactly.
        """
        instructions = list(instructions or [])
        max_attempts = self.settings.max_verification_attempts

        with agent_span("orchestrator", case_id, "draft_verify_loop"):
            for attempt in range(start_attempt, max_attempts + 1):
                case = self.fleet.cases.load(case_id)
                if pending_draft is not None and pending_draft.attempt == attempt:
                    # A crash lost the verification of this draft, not the
                    # draft itself. Re-drafting would append a second attempt
                    # under the same number for no reason — reuse it.
                    draft = pending_draft
                else:
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
                pending_draft = None

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

        # A dedicated counter, not `escalation_count`. See the field's
        # docstring in core/schemas/case.py: escalation only advances through
        # the scheduled ladder, which never looks at `approved`, so a
        # transient send failure had no path to a fresh idempotency key and
        # every retry replayed the same permanently-failed action. Bumping
        # this here -- on the attempt actually about to call the payer -- is
        # what gives ``retry_transmission`` somewhere new to go.
        case = self.fleet.cases.mutate(
            case_id, lambda c: setattr(c, "transmission_attempts", c.transmission_attempts + 1)
        )

        try:
            outcome = self._act(
                case_id,
                ActionType.SUBMIT_APPEAL,
                {"draft_sha": content_digest(draft.to_firestore())},
                lambda: effects.submit_appeal(self.fleet, case_id, draft),
                attempt=case.transmission_attempts,
            )
        except (ActionPreviouslyFailed, UnsafeToRetry) as exc:
            # Transmission did not happen, or may have half-happened. Either way
            # the case must not sit at `approved` looking finished — that status
            # is in no queue and nothing would ever look at it again.
            self._notify(
                case_id,
                f"The appeal for {case_id} was approved but not transmitted. {exc}",
            )
            return self._transmission_failed(
                case_id, case, str(exc), unsafe=isinstance(exc, UnsafeToRetry)
            )
        except Exception as exc:
            # First failure. The action record already carries the error; the
            # case follows it so a person sees it rather than a stalled status.
            self._notify(case_id, f"Transmitting the appeal for {case_id} failed: {exc}")
            self._transmission_failed(case_id, case, str(exc), unsafe=False)
            raise

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

    def retry_transmission(self, case_id: str) -> CaseRecord:
        """The explicit way back for a case ``try_submit`` bounced to a person.

        ``try_submit`` cannot get here on its own: it early-returns the moment
        ``status`` is not ``approved``, and a failed send is exactly what moves
        a case away from ``approved``. Nothing about redelivery or the
        scheduler reaches this either -- it is a deliberate action, made by
        something (a person, an operator tool) that has decided the earlier
        failure is safe to try again. That is also why it does not run
        automatically from ``find_stalled``: minting a fresh attempt on a
        payer call is not a decision to make unattended.
        """
        case = self.fleet.cases.load(case_id)
        if case.status is not CaseStatus.NEEDS_HUMAN_REVIEW:
            return case
        if case.transmission_unsafe:
            # The previous claim died without recording whether the payer got
            # it. Retrying blind risks a second real appeal; only a person who
            # has checked with the payer directly may clear this, and there is
            # no automatic path that does that.
            return case
        if not case.ready_to_submit:
            # Not something a resend can fix -- a signature is missing or was
            # invalidated by a later draft. Pushing this to `approved` would
            # just bounce it straight back here without ever calling the payer.
            return case
        if case.transmission_attempts >= MAX_TRANSMISSION_ATTEMPTS:
            return case

        case = self._advance(case_id, CaseStatus.APPROVED, note="retrying transmission")
        return self.try_submit(case_id)

    def _transmission_failed(
        self, case_id: str, case: CaseRecord, error: str, unsafe: bool
    ) -> CaseRecord:
        """Record one failed transmission attempt and route the case to a person.

        The reason accumulates across attempts rather than overwriting itself,
        so a person opening the needs-human queue after several retries sees
        what happened on every one of them, not just the most recent.
        """

        def attach(c: CaseRecord) -> None:
            c.transmission_errors.append(error[:500])
            c.transmission_unsafe = unsafe
            attempts = c.transmission_attempts
            history = "; ".join(
                f"attempt {i + 1}: {e}" for i, e in enumerate(c.transmission_errors)
            )
            if unsafe:
                tail = (
                    " The previous attempt's outcome is unknown -- it may or may not "
                    "have reached the payer. Retrying automatically risks a second "
                    "real appeal; a person has to check with the payer directly "
                    "before this case can move again."
                )
            elif attempts >= MAX_TRANSMISSION_ATTEMPTS:
                tail = (
                    f" That is the limit ({MAX_TRANSMISSION_ATTEMPTS} attempts); this "
                    f"needs a person, not another automatic retry."
                )
            else:
                tail = (
                    " Retrying is a decision for a person, because a failure that "
                    "happened partway through cannot be told apart from one that "
                    "never started."
                )
            c.needs_human_reason = (
                f"The appeal was approved but transmission failed. Nothing reached "
                f"the payer. Tried {attempts} time(s) so far -- {history}."
            ) + tail
            c.last_error = error[:500]

        return self._advance(
            case_id,
            CaseStatus.NEEDS_HUMAN_REVIEW,
            attach=attach,
            note="transmission failed",
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


# Map from a mid-pipeline status to the method that resumes it. Deliberately a
# lookup rather than a chain of ``if case.status is ...`` branches bolted onto
# ``ingest`` — a status added to this pipeline later either gets an entry here
# or falls through to "nothing to do", which is a visible gap (caught by the
# IN_FLIGHT_STATUSES coverage test) rather than a silent one.
#
# Every value is a plain function of ``(Pipeline, case_id, document, case)``,
# not a bound method reference, because it is built here at module scope after
# the class body has already produced the underlying functions.
_RESUME_STAGES: dict[
    CaseStatus, Callable[[Pipeline, str, SourceDocument, CaseRecord], CaseRecord]
] = {
    CaseStatus.SCREENING: Pipeline._resume_screening,
    CaseStatus.EXTRACTED: Pipeline._resume_retrieval,
    CaseStatus.RETRIEVING: Pipeline._resume_retrieval,
    CaseStatus.MAPPING: Pipeline._resume_mapping,
    CaseStatus.DRAFTING: Pipeline._resume_drafting,
    CaseStatus.VERIFYING: Pipeline._resume_verifying,
}
