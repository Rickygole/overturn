"""The durable case record.

This is the product. No process runs between a submitted appeal and a payer's
response weeks later; the entire state of the work lives in this document in
Firestore, and any worker that picks the case up can resume from it.
"""

from __future__ import annotations

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
            approved_attempt = self.human_decision.draft_attempt_approved
            if (
                approved_attempt is not None
                and cosign.draft_attempt_signed is not None
                and cosign.draft_attempt_signed != approved_attempt
            ):
                return False
        return True

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
