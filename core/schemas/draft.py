"""The appeal letter as a structured object rather than a blob of prose.

Drafting returns citations as data, not embedded in the text, so Verification
can check each one without parsing English.
"""

from __future__ import annotations

from pydantic import Field

from core.schemas.base import OverturnModel


class Citation(OverturnModel):
    """A single reference from the letter back to the policy corpus."""

    section_id: str = Field(description="Policy identifier as cited in the letter body.")
    claim: str = Field(
        description="What the letter asserts this section says or requires. "
        "Verification checks this against the verbatim source text."
    )
    quoted_text: str | None = Field(
        default=None, description="Policy text quoted directly in the letter, if any."
    )
    supporting_criterion_ids: list[str] = Field(
        default_factory=list,
        description="Criteria matrix rows this citation rests on.",
    )


class AppealDraft(OverturnModel):
    """A letter, its citations, and the clinical assertions it makes.

    ``clinical_assertions`` exists so Verification has an explicit list to check
    against the criteria matrix rather than having to infer claims from prose.
    Drafting is instructed to enumerate every factual claim it makes.
    """

    case_id: str
    attempt: int = Field(ge=1, description="Which drafting attempt this is, 1-indexed.")

    subject_line: str
    body: str = Field(description="The full letter text, ready for a human to read.")

    citations: list[Citation] = Field(default_factory=list)
    clinical_assertions: list[str] = Field(
        default_factory=list,
        description="Every factual claim about the patient the letter makes. "
        "Each must trace to evidence in the criteria matrix.",
    )

    requested_action: str = Field(
        default="Overturn the denial and process the claim for payment.",
        description="What the letter asks the payer to do.",
    )
    model_used: str | None = None
    revision_feedback_applied: list[str] = Field(
        default_factory=list,
        description="Verification failures from prior attempts that this draft addresses.",
    )

    def cited_ids(self) -> set[str]:
        return {c.section_id for c in self.citations}
