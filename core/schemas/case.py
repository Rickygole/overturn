"""The durable case record.

This is the product. No process runs between a submitted appeal and a payer's
response weeks later; the entire state of the work lives in this document in
Firestore, and any worker that picks the case up can resume from it.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from pydantic import Field, computed_field

from core.schemas.base import OverturnModel, utcnow
from core.schemas.criteria import CriteriaMatrix
from core.schemas.denial import DenialExtraction
from core.schemas.draft import AppealDraft
from core.schemas.enums import TERMINAL_STATUSES, AppealLevel, CaseStatus

# Statuses in which the ball is in the payer's court and a deadline is running.
AWAITING_PAYER_STATUSES: frozenset[CaseStatus] = frozenset(
    {CaseStatus.SUBMITTED, CaseStatus.ESCALATED}
)
from core.schemas.policy import RetrievalResult
from core.schemas.sentinel import ScreeningResult
from core.schemas.verification import VerificationResult


class StatusTransition(OverturnModel):
    """One entry in the case history. Append-only."""

    at: datetime = Field(default_factory=utcnow)
    from_status: CaseStatus | None = None
    to_status: CaseStatus
    actor: str = Field(description="Agent name, 'scheduler', or a human identifier.")
    note: str | None = None


class HumanDecision(OverturnModel):
    """The clerk's gate: is the paper trail sound?

    A billing clerk is the right person to approve this and the wrong person to
    approve medicine, so the question put to them is deliberately narrow. They
    are asked to confirm three things a non-clinician can actually check, all
    three of which Verification has already computed:

      * every cited section exists in the retrieved policy set
      * the quoted policy text says what the letter says it says
      * nothing is asserted that has no row in the criteria matrix

    They are not asked whether the care was appropriate. Where a draft makes a
    clinical argument, :class:`ClinicianCosign` is required as well, and
    ``CaseRecord.ready_to_submit`` will not return true without it.
    """

    decided_at: datetime = Field(default_factory=utcnow)
    decided_by: str
    approved: bool
    note: str | None = None
    draft_attempt_approved: int | None = None

    citations_checked: bool = Field(
        default=False, description="Clerk confirmed each cited identifier resolves."
    )
    quotes_checked: bool = Field(
        default=False, description="Clerk confirmed quoted policy text matches the source."
    )
    assertions_checked: bool = Field(
        default=False,
        description="Clerk confirmed no claim appears that the matrix does not support.",
    )


class ClinicianCosign(OverturnModel):
    """The ordering clinician's signature on the clinical argument.

    Medical-necessity appeals are signed by the clinician who ordered the care.
    Modelling that is not ceremony: it is the difference between a system that
    drafts a letter for a qualified signatory and one that quietly asks an
    administrator to vouch for a clinical claim they cannot evaluate.
    """

    signed_at: datetime = Field(default_factory=utcnow)
    clinician_name: str
    credential: str = Field(description="MD, DO, NP, PA, and so on.")
    npi: str | None = None
    attests_clinical_accuracy: bool
    note: str | None = None
    draft_attempt_signed: int | None = None


class PayerResponse(OverturnModel):
    """What came back, if anything."""

    received_at: datetime = Field(default_factory=utcnow)
    outcome: str = Field(description="'overturned', 'upheld', 'partial', or 'no_response'.")
    rationale: str | None = None
    next_level_available: AppealLevel | None = None
    raw_document_uri: str | None = None


class CaseRecord(OverturnModel):
    """Everything known about one denied claim."""

    case_id: str
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    status: CaseStatus = CaseStatus.RECEIVED
    history: list[StatusTransition] = Field(default_factory=list)

    # --- Source document --------------------------------------------------------
    source_document_uri: str
    source_sha256: str | None = None

    # --- Agent outputs, attached as the pipeline advances -----------------------
    screening: ScreeningResult | None = None
    denial: DenialExtraction | None = None
    retrieval: RetrievalResult | None = None
    criteria: CriteriaMatrix | None = None
    drafts: list[AppealDraft] = Field(default_factory=list)
    verifications: list[VerificationResult] = Field(default_factory=list)

    # --- Human gate -------------------------------------------------------------
    human_decision: HumanDecision | None = None
    clinician_cosign: ClinicianCosign | None = None
    requires_clinician_cosign: bool = Field(
        default=True,
        description="Set false only where the draft argues documentation alone and "
        "makes no clinical claim. Defaults to true because that is the safe default.",
    )

    # --- Lifecycle --------------------------------------------------------------
    appeal_level: AppealLevel = AppealLevel.FIRST_LEVEL
    submitted_at: datetime | None = None
    response_deadline: datetime | None = Field(
        default=None,
        description="When the payer's response window closes. The scheduler polls on this.",
    )
    escalation_count: int = 0
    payer_responses: list[PayerResponse] = Field(default_factory=list)

    # --- Transmission -------------------------------------------------------------
    # Deliberately its own counter, not `escalation_count`. Escalation only
    # advances through the scheduled ladder, which only ever looks at
    # `submitted` and `escalated` cases -- a case sitting at `approved` (or
    # bounced from it to `needs_human_review` after a failed send) is invisible
    # to that path. Coupling transmission retries to it meant a transient
    # network error on `try_submit` produced a case with no idempotency key
    # that could ever change again: `find_overdue` would never see it, so
    # `escalation_count` would never increment, so every retry replayed the
    # same failed action forever.
    transmission_attempts: int = Field(
        default=0,
        description="How many times SUBMIT_APPEAL has actually been attempted. "
        "Bumped by `Pipeline.try_submit` immediately before each call to the "
        "payer, so a deliberate retry gets a genuinely new idempotency key.",
    )
    transmission_errors: list[str] = Field(
        default_factory=list,
        description="One entry per failed transmission attempt, oldest first, "
        "so a person reading `needs_human_reason` sees what happened on every "
        "try rather than just the most recent one.",
    )
    transmission_unsafe: bool = Field(
        default=False,
        description="Set when a transmission claim expired without the guard "
        "recording an outcome -- whether the payer received that attempt is "
        "genuinely unknown. `retry_transmission` refuses to mint a new attempt "
        "while this is true, because guessing wrong risks a second real appeal.",
    )

    # --- Failure handling -------------------------------------------------------
    last_error: str | None = None
    failure_count: int = 0
    needs_human_reason: str | None = None

    # --- Concurrency ------------------------------------------------------------
    revision: int = Field(
        default=0, description="Incremented on every write; used for optimistic locking."
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @computed_field  # type: ignore[prop-decorator]
    @property
    def draft_attempts(self) -> int:
        return len(self.drafts)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_overdue(self) -> bool:
        """Whether the payer's response window has elapsed with no answer.

        This single property is what the scheduled job queries on. It is
        deliberately a pure function of stored state, so a worker that has never
        seen this case before can evaluate it correctly.

        Both statuses count, and the reason is the whole product: a case that
        has been escalated to the second level is waiting on the payer in
        exactly the way a freshly submitted one is. Checking only ``submitted``
        let the ladder advance one rung and then stall silently — the case sat
        in ``escalated`` past a deadline that nothing was watching, which is the
        precise failure this system exists to prevent.
        """
        if self.status not in AWAITING_PAYER_STATUSES or self.response_deadline is None:
            return False
        return utcnow() >= self.response_deadline

    @property
    def latest_draft(self) -> AppealDraft | None:
        return self.drafts[-1] if self.drafts else None

    @property
    def latest_verification(self) -> VerificationResult | None:
        return self.verifications[-1] if self.verifications else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ready_to_submit(self) -> bool:
        """Whether every signature this case needs is actually present.

        Checked immediately before transmission, not at approval time, so a case
        that gains a clinical argument on a later drafting attempt cannot ride
        an earlier clerk approval out the door.
        """
        if not self.human_decision or not self.human_decision.approved:
            return False
        if self.requires_clinician_cosign:
            cosign = self.clinician_cosign
            if cosign is None or not cosign.attests_clinical_accuracy:
                return False
            # Both attempts must be pinned, and they must match. Skipping the
            # check when either is unset meant an unpinned co-sign authorised
            # any draft — including one written after the signature was given,
            # since `approved_draft()` falls back to the most recent draft when
            # the approval is not pinned either. A signature that cannot say
            # what it signed is not a signature.
            approved_attempt = self.human_decision.draft_attempt_approved
            signed_attempt = cosign.draft_attempt_signed
            if approved_attempt is None or signed_attempt is None:
                return False
            if signed_attempt != approved_attempt:
                return False
        return True

    def unfilled_gaps(self) -> list[str]:
        """Bracketed placeholders still standing in the letter about to be sent.

        **This does not block transmission, and that is a deliberate,
        uncomfortable choice.** Every letter carries six or seven of these,
        because a clinic's letterhead, the payer's appeals address and an
        ordering provider's NPI are not facts about a case and were never
        captured at intake. Refusing to send while any remain would mean the
        product can never send anything at all, which is not a safety property,
        it is a broken product.

        So the gate stays open and the gaps are shown to the person signing
        instead. The right fix is to capture the four fields that are printed
        on every determination notice (requesting provider, provider NPI,
        appeals address, allowed amount) at intake, and to put the clinic's own
        letterhead in configuration where it belongs -- at which point the
        remaining gaps are few enough that refusing to send on them is
        reasonable. That is the next piece of work, and it is named here rather
        than in a backlog nobody reads.

        Drafting composes the letter's face -- date line, addressee, reference
        block, signature -- from the case record, and emits a bracketed gap for
        anything the record does not hold: the practice letterhead, the payer's
        appeals address, the ordering provider's NPI. That is the correct
        behaviour; inventing an NPI would be far worse.

        But `effects.submit_appeal` transmits `body` verbatim, so without this
        an approved letter would post "[NPI -- not in the case record; add
        before sending]" to the payer. A letter that still says what is missing
        from it is not a letter anyone signed off on sending, whatever the
        signatures say. The gate fails closed on it, which is the same posture
        the attempt cap takes: not sending is a designed outcome.
        """
        draft = self.approved_draft()
        if draft is None:
            return []
        return sorted(set(re.findall(r"\[[^\[\]]{8,120}\]", draft.body)))

    def approved_draft(self) -> AppealDraft | None:
        """The exact draft a human signed off on, not merely the most recent one."""
        if not self.human_decision or not self.human_decision.approved:
            return None
        attempt = self.human_decision.draft_attempt_approved
        if attempt is None:
            return self.latest_draft
        return next((d for d in self.drafts if d.attempt == attempt), None)

    def transition(self, to: CaseStatus, actor: str, note: str | None = None) -> None:
        """Move the case to a new status, recording where it came from."""
        self.history.append(
            StatusTransition(from_status=self.status, to_status=to, actor=actor, note=note)
        )
        self.status = to
        self.updated_at = utcnow()
        self.revision += 1

    def set_response_deadline(self, days: int, accelerated_seconds_per_day: float | None) -> None:
        """Compute when the payer is late.

        In demo mode a day compresses to a configurable number of seconds so that
        weeks of lifecycle are observable in a short video. This is disclosed in
        the README and stated out loud in the demo.
        """
        base = self.submitted_at or utcnow()
        if accelerated_seconds_per_day is not None:
            self.response_deadline = base + timedelta(seconds=days * accelerated_seconds_per_day)
        else:
            self.response_deadline = base + timedelta(days=days)
