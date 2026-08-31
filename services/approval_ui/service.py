"""The approval gate as a service object, independent of HTTP.

This is the moment the whole system is built around: a human reads a drafted
appeal and decides whether it may be transmitted to an insurer. Everything here
exists to make that decision recorded, single, and attributable.

Three rules shape the file:

  * **The approved draft is pinned by attempt number.** The reviewer's form
    carries the attempt they actually read, and that number is what lands in
    ``HumanDecision.draft_attempt_approved``. Approving "the latest draft" would
    mean approving whatever happened to be in the record at write time, which is
    not what the human agreed to.
  * **Approval routes through the idempotency guard.** A double-clicked button
    and a retried request are the same event; the second one must replay rather
    than record a second decision. The action type is ``RECORD_APPROVAL``: this is
    the human-gate action, and deliberately *not* ``SUBMIT_APPEAL``, which
    belongs to Lifecycle. Claiming that key here would block the real submission.
  * **Rejection does not route through the guard.** It has no effect outside
    this system; it moves the case into an internal review queue. Its protection
    against a double write is the status check, which is sufficient because a
    rejected case is no longer in ``awaiting_human_approval``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from core.audit import AuditLog
from core.gateway import GatewayHandle
from core.idempotency import IdempotencyGuard
from core.memory import MemoryBank, PayerObservation
from core.schemas.case import CaseRecord, ClinicianCosign, HumanDecision
from core.schemas.enums import ActionType, AgentName, CaseStatus
from core.state import CaseRepository
from core.store import DocumentStore
from services.approval_ui.view import CLOSED_STATUSES, WITH_PAYER_STATUSES

# The status a case must be in before a human may sign it off.
REVIEWABLE_STATUS = CaseStatus.AWAITING_APPROVAL


class ApprovalError(Exception):
    """A refusal this interface reports back to the reviewer.

    Carries its own HTTP status so the routing layer does not have to maintain a
    parallel table mapping error classes to codes.
    """

    status_code = 409
    heading = "This decision was not recorded"
    field: str | None = None  # the form control at fault, when there is one


class ChecksNotConfirmed(ValueError):
    """The clerk submitted an approval without confirming all three checks."""


class NotApprovable(ApprovalError):
    """The case is not in a state where a human decision means anything."""


class StaleReview(ApprovalError):
    """The draft on screen is not the draft currently under review."""

    heading = "The draft changed while you were reading it"


class UnknownDraftAttempt(ApprovalError):
    """The submitted attempt number does not exist on this case."""

    status_code = 400


class MissingReason(ApprovalError):
    """A rejection with no stated reason is not a rejection, it is a shrug."""

    status_code = 400
    heading = "A rejection needs a reason"
    field = "reason"


class MissingReviewer(ApprovalError):
    """Nothing is recorded anonymously."""

    status_code = 400
    heading = "Say who is deciding"
    field = "decided_by"


@dataclass(frozen=True)
class DecisionOutcome:
    """What happened when a human pressed the button.

    ``recorded`` is ``False`` when the idempotency guard replayed an earlier
    identical decision. The interface says so rather than pretending it wrote
    something, because a reviewer who is told "recorded" twice will reasonably
    believe two things were recorded.
    """

    case: CaseRecord
    recorded: bool
    action_key: str | None = None
    delivery_count: int = 1


class ApprovalService:
    """Queue, review and decide. One instance per process, holding no state."""

    def __init__(self, store: DocumentStore, agent: AgentName = AgentName.ORCHESTRATOR) -> None:
        self._store = store
        self.gateway = GatewayHandle(agent)
        self.cases = CaseRepository(store, self.gateway)
        self.audit = AuditLog(store, self.gateway)
        self.guard = IdempotencyGuard(store, self.gateway)
        self.memory = MemoryBank(store, self.gateway)

    # -- reads ---------------------------------------------------------------- #

    def load(self, case_id: str) -> CaseRecord:
        """The case, or ``CaseNotFound``."""
        return self.cases.load(case_id)

    def awaiting_approval(self) -> list[CaseRecord]:
        return sorted(self.cases.find_by_status(CaseStatus.AWAITING_APPROVAL), key=_by_deadline)

    def needs_human_review(self) -> list[CaseRecord]:
        return sorted(self.cases.find_by_status(CaseStatus.NEEDS_HUMAN_REVIEW), key=_by_deadline)

    def open_cases(self) -> list[CaseRecord]:
        """Every case a person can still act on, soonest deadline first.

        One list rather than three, because the queue is one table now. Who a
        case is waiting on was never a property of which query returned it; it
        is a property of the case, and it reads as a column.
        """
        held = [
            *self.cases.find_by_status(CaseStatus.AWAITING_APPROVAL),
            *self.cases.find_by_status(CaseStatus.APPROVED),
            *self.cases.find_by_status(CaseStatus.NEEDS_HUMAN_REVIEW),
        ]
        return sorted(held, key=_by_deadline)

    def with_payer_cases(self) -> list[CaseRecord]:
        """Every case currently with the payer, soonest deadline first.

        Mirrors `open_cases()`'s shape for the one state nobody but Lifecycle
        can act on. Without a way to fetch these, a case in this status --
        including one that escalated itself unattended, the strongest
        evidence this project has for its own defining claim -- was
        reachable from the queue only by already knowing its id. Reads the
        same `WITH_PAYER_STATUSES` `view.py`'s dashboard counts against, so
        the count and the list behind it cannot drift apart.
        """
        held = [c for status in WITH_PAYER_STATUSES for c in self.cases.find_by_status(status)]
        return sorted(held, key=_by_deadline)

    def closed_cases(self) -> list[CaseRecord]:
        """Every case that has finished, one way or another.

        Same shape, same reasoning. A quarantined case -- the one behind the
        Model Armor negative result -- sits here, and was equally
        unreachable before this existed. Reads `CLOSED_STATUSES`, the same
        table `view.py` renders the "What each state means" disclosure from.
        """
        held = [c for status, _, _ in CLOSED_STATUSES for c in self.cases.find_by_status(status)]
        return sorted(held, key=_by_deadline)

    def all_cases(self) -> list[CaseRecord]:
        """Every case, for the dashboard counts.

        One pass rather than a query per status: at this size the read is
        cheaper than fifteen round trips, and it is the only way to show the
        statuses nothing else surfaces. At ten thousand cases this becomes
        counters maintained on write.
        """
        return list(self.cases.iter_all())

    def trail(self, case_id: str) -> list[Any]:
        """Audit events for one case, oldest first."""
        from core.audit import read_case_trail

        return read_case_trail(self._store, self.gateway, case_id)

    def payer_observation(self, case: CaseRecord) -> PayerObservation | None:
        """What cross-case memory holds for this case's payer/policy/reason.

        ``None`` for a case with no denial to key on, or genuinely no prior
        observations -- the case page renders both honestly rather than
        hiding the section.
        """
        return self.memory.recall_for_case(case)

    # -- decisions ------------------------------------------------------------ #

    def approve(
        self,
        case_id: str,
        decided_by: str,
        draft_attempt: int | None,
        citations_checked: bool = False,
        quotes_checked: bool = False,
        assertions_checked: bool = False,
    ) -> DecisionOutcome:
        """Record the clerk's approval of one specific drafting attempt.

        The clerk is asked about the paper trail, not the medicine: that every
        cited section resolves, that the quoted policy text matches its source,
        and that nothing is asserted the criteria matrix does not support.
        Verification has already computed all three; the clerk is confirming
        them, which is a thing a non-clinician can competently do.

        Whether the care was appropriate is a different question and it is put
        to a clinician in :meth:`cosign`.

        Called twice with the same arguments, this writes once. The second call
        is replayed by the guard and reported with ``recorded=False``.
        """
        reviewer = _require_reviewer(decided_by)
        if not (citations_checked and quotes_checked and assertions_checked):
            raise ChecksNotConfirmed(
                "All three verification checks must be confirmed before approving. "
                "The clerk is attesting that the citations resolve, that the quoted "
                "policy text matches, and that nothing is asserted without support."
            )
        case = self.cases.load(case_id)
        attempt = self._pin_attempt(case, draft_attempt)

        if case.status != REVIEWABLE_STATUS and not _already_approved(case, attempt):
            raise NotApprovable(
                f"Case {case_id} is {case.status.value!r}. Only a case in "
                f"{REVIEWABLE_STATUS.value!r} can be approved for transmission."
            )

        # Built once, outside the mutation closure: CaseRepository.mutate may run
        # the closure more than once under contention, and the decision timestamp
        # must not shift between attempts.
        decision = HumanDecision(
            decided_by=reviewer,
            approved=True,
            draft_attempt_approved=attempt,
            note=f"Approved drafting attempt {attempt} for transmission.",
            citations_checked=citations_checked,
            quotes_checked=quotes_checked,
            assertions_checked=assertions_checked,
        )
        payload = {
            "case_id": case_id,
            "decision": "approve",
            "draft_attempt": attempt,
            "decided_by": reviewer,
        }

        def record() -> dict[str, Any]:
            with self.audit.record(case_id, "human_approval", payload) as recording:
                updated = self.cases.mutate(case_id, lambda c: _apply_approval(c, decision))
                recording.input_summary = (
                    f"reviewer approved drafting attempt {attempt} of case {case_id}"
                )
                recording.decision = f"approved: attempt {attempt} may be transmitted to the payer"
                recording.output = {
                    "approved": True,
                    "draft_attempt_approved": attempt,
                    "status": updated.status.value,
                    "decided_by": reviewer,
                }
            return {
                "approved": True,
                "case_id": case_id,
                "draft_attempt_approved": attempt,
                "status": CaseStatus.APPROVED.value,
            }

        outcome = self.guard.execute(
            case_id=case_id,
            action_type=ActionType.RECORD_APPROVAL,
            payload=payload,
            fn=record,
            attempt=attempt,
        )
        return DecisionOutcome(
            case=self.cases.load(case_id),
            recorded=outcome.executed,
            action_key=outcome.action_key,
            delivery_count=outcome.delivery_count,
        )

    def cosign(
        self,
        case_id: str,
        clinician_name: str,
        credential: str,
        attests_clinical_accuracy: bool,
        npi: str | None = None,
        note: str | None = None,
        draft_attempt: int | None = None,
    ) -> DecisionOutcome:
        """Record the ordering clinician's signature on the clinical argument.

        Medical-necessity appeals are signed by the clinician who ordered the
        care. Modelling that is not ceremony: it is the difference between a
        system that drafts a letter for a qualified signatory and one that
        quietly asks an administrator to vouch for a clinical claim they are not
        in a position to evaluate.

        The signature is pinned to a specific drafting attempt, so a co-sign
        cannot authorise a draft written after it was given.
        """
        if not clinician_name.strip():
            raise MissingReviewer("A clinician co-sign requires a name.")
        if not credential.strip():
            raise MissingReviewer("A clinician co-sign requires a credential.")
        if not attests_clinical_accuracy:
            raise ChecksNotConfirmed(
                "A co-sign without an attestation records nothing. Reject the draft "
                "instead if the clinical argument is wrong."
            )

        case = self.cases.load(case_id)
        attempt = self._pin_attempt(case, draft_attempt)

        signature = ClinicianCosign(
            clinician_name=clinician_name.strip(),
            credential=credential.strip(),
            npi=(npi or "").strip() or None,
            attests_clinical_accuracy=True,
            note=note,
            draft_attempt_signed=attempt,
        )
        payload = {
            "case_id": case_id,
            "decision": "cosign",
            "draft_attempt": attempt,
            "clinician": signature.clinician_name,
        }

        def record() -> dict[str, Any]:
            with self.audit.record(case_id, "clinician_cosign", payload) as recording:

                def apply(current: CaseRecord) -> None:
                    current.clinician_cosign = signature
                    current.revision += 1

                self.cases.mutate(case_id, apply)
                recording.input_summary = (
                    f"clinician co-signed drafting attempt {attempt} of case {case_id}"
                )
                recording.decision = f"co-signed: attempt {attempt} clinical argument attested"
                recording.output = {"draft_attempt_signed": attempt}
            return {"cosigned": True, "draft_attempt_signed": attempt}

        outcome = self.guard.execute(
            case_id=case_id,
            action_type=ActionType.RECORD_COSIGN,
            payload=payload,
            fn=record,
            attempt=attempt,
        )
        return DecisionOutcome(
            case=self.cases.load(case_id),
            recorded=outcome.executed,
            action_key=outcome.action_key,
            delivery_count=outcome.delivery_count,
        )

    def submit_if_ready(self, case_id: str, pipeline: Any | None = None) -> CaseRecord:
        """Transmit, but only once every required signature is present.

        Called after either signature lands, so the order the clerk and the
        clinician sign in does not matter — whichever is second triggers the
        submission. ``CaseRecord.ready_to_submit`` is read and never recomputed
        here, so there is exactly one definition of what counts as enough.

        Without this call the approval interface is a dead end: a case reaches
        ``approved`` and nothing on earth moves it to ``submitted``.
        """
        case = self.cases.load(case_id)
        if not case.ready_to_submit:
            return case

        if pipeline is None:
            from agents.orchestrator.deps import build_fleet
            from agents.orchestrator.pipeline import Pipeline

            pipeline = Pipeline(build_fleet(store=self._store))
        return pipeline.try_submit(case_id)

    def reject(self, case_id: str, decided_by: str, reason: str) -> DecisionOutcome:
        """Send a draft back, with the reason attached to the case."""
        reviewer = _require_reviewer(decided_by)
        stated = (reason or "").strip()
        if not stated:
            raise MissingReason(
                "Say what is wrong with this draft. The reason is stored on the case "
                "and is what the next attempt has to answer."
            )

        case = self.cases.load(case_id)
        if case.status != REVIEWABLE_STATUS:
            raise NotApprovable(
                f"Case {case_id} is {case.status.value!r}. Only a case in "
                f"{REVIEWABLE_STATUS.value!r} can be rejected at this gate."
            )

        attempt = case.latest_draft.attempt if case.latest_draft else None
        decision = HumanDecision(
            decided_by=reviewer,
            approved=False,
            draft_attempt_approved=None,
            note=stated,
        )
        payload = {
            "case_id": case_id,
            "decision": "reject",
            "draft_attempt": attempt,
            "decided_by": reviewer,
        }

        with self.audit.record(case_id, "human_rejection", payload) as recording:
            updated = self.cases.mutate(case_id, lambda c: _apply_rejection(c, decision, attempt))
            recording.input_summary = (
                f"reviewer rejected drafting attempt {attempt} of case {case_id}"
            )
            recording.decision = f"rejected: {stated}"[:500]
            recording.output = {
                "approved": False,
                "draft_attempt_rejected": attempt,
                "status": updated.status.value,
                "decided_by": reviewer,
            }

        return DecisionOutcome(case=updated, recorded=True)

    # -- internals ------------------------------------------------------------ #

    def _pin_attempt(self, case: CaseRecord, requested: int | None) -> int:
        """Resolve which drafting attempt this approval applies to."""
        if not case.drafts:
            raise NotApprovable(
                f"Case {case.case_id} has no drafted appeal. There is nothing to approve."
            )

        latest = case.drafts[-1].attempt
        if requested is None:
            return latest

        known = {d.attempt for d in case.drafts}
        if requested not in known:
            raise UnknownDraftAttempt(
                f"Case {case.case_id} has no drafting attempt {requested}. "
                f"Attempts on file: {sorted(known)}."
            )

        if requested != latest and case.status == REVIEWABLE_STATUS:
            raise StaleReview(
                f"You reviewed attempt {requested}, but attempt {latest} is now the "
                f"draft on file. Re-read the current draft before approving it."
            )
        return requested


def _apply_approval(case: CaseRecord, decision: HumanDecision) -> None:
    """Pure mutation. Safe to run more than once on a freshly loaded record."""
    case.human_decision = decision
    if case.status != CaseStatus.APPROVED:
        case.transition(
            CaseStatus.APPROVED,
            actor=decision.decided_by,
            note=f"Human approved drafting attempt {decision.draft_attempt_approved}.",
        )


def _apply_rejection(case: CaseRecord, decision: HumanDecision, attempt: int | None) -> None:
    case.human_decision = decision
    case.needs_human_reason = (
        f"Appeal draft attempt {attempt} rejected by {decision.decided_by}: {decision.note}"
    )
    if case.status != CaseStatus.NEEDS_HUMAN_REVIEW:
        case.transition(
            CaseStatus.NEEDS_HUMAN_REVIEW,
            actor=decision.decided_by,
            note=decision.note,
        )


def _already_approved(case: CaseRecord, attempt: int) -> bool:
    """Whether this exact approval is already on the record.

    Used to let a duplicate submission fall through to the guard, which replays
    it, rather than being turned away by the status check with an error. A
    double-click is not an error; it is the same decision arriving twice.
    """
    decision = case.human_decision
    return (
        case.status == CaseStatus.APPROVED
        and decision is not None
        and decision.approved
        and decision.draft_attempt_approved == attempt
    )


def _require_reviewer(decided_by: str | None) -> str:
    name = (decided_by or "").strip()
    if not name:
        raise MissingReviewer(
            "Enter your name or email. Every decision at this gate is attributed, "
            "and an unattributed approval is worse than no approval."
        )
    return name[:200]


def _by_deadline(case: CaseRecord) -> tuple[bool, date, str]:
    """Soonest appeal deadline first; cases with no stated deadline last."""
    deadline = case.denial.appeal_deadline if case.denial else None
    return (deadline is None, deadline or date.max, case.case_id)
