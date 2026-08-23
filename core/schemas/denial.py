"""What the Intake agent pulls out of a denial letter."""

from __future__ import annotations

from datetime import date

from pydantic import Field

from core.schemas.base import OverturnModel


class DeniedService(OverturnModel):
    """One line item the payer refused to cover."""

    description: str = Field(description="The service as the letter names it.")
    procedure_code: str | None = Field(
        default=None, description="CPT/HCPCS code if the letter states one."
    )
    diagnosis_code: str | None = Field(
        default=None, description="ICD-10 code if the letter states one."
    )
    date_of_service: date | None = None
    billed_amount: float | None = None


class DenialExtraction(OverturnModel):
    """Structured form of an inbound denial letter.

    Every field is nullable except the ones a letter cannot omit and still be a
    denial letter. Intake is instructed to leave a field null rather than guess;
    a null here is recoverable, an invented claim number is not.
    """

    payer_name: str
    member_id: str | None = None
    claim_number: str | None = None
    patient_name: str | None = None
    patient_dob: date | None = None

    services: list[DeniedService] = Field(
        default_factory=list, description="Line items denied, at least one expected."
    )

    denial_reason_text: str = Field(
        description="The payer's stated reason, quoted as closely as the letter allows."
    )
    denial_reason_code: str | None = Field(
        default=None, description="Payer reason code, e.g. 'CO-50' or a plan-specific code."
    )

    date_of_denial: date | None = None
    appeal_deadline: date | None = Field(
        default=None, description="Deadline the letter states for filing an appeal."
    )
    appeal_window_days: int | None = Field(
        default=None, description="Days allowed, when the letter gives a window not a date."
    )

    referenced_policy_hint: str | None = Field(
        default=None,
        description="Any policy name or number the letter itself cites, used to seed retrieval.",
    )

    extraction_notes: str | None = Field(
        default=None, description="Anything ambiguous or illegible, stated plainly."
    )
    source_document_uri: str | None = None
    ocr_used: bool = Field(
        default=False, description="True when the source was a scan or fax image."
    )
