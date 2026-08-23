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
    """The approval gate. Nothing leaves this system without one of these."""

    decided_at: datetime = Field(default_factory=utcnow)
    decided_by: str
    approved: bool
    note: str | None = None
    draft_attempt_approved: int | None = None


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
        """
        if self.status != CaseStatus.SUBMITTED or self.response_deadline is None:
            return False
        return utcnow() >= self.response_deadline

    @property
    def latest_draft(self) -> AppealDraft | None:
        return self.drafts[-1] if self.drafts else None

    @property
    def latest_verification(self) -> VerificationResult | None:
        return self.verifications[-1] if self.verifications else None

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
