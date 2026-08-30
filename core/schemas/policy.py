"""Payer medical policy structures.

A policy section is the atom of this system. Retrieval returns them, Mapping
reasons over them, Drafting cites them by ``section_id``, and Verification
refuses to let a citation through unless the id appears in this set.
"""

from __future__ import annotations

from pydantic import Field, field_validator

from core.schemas.base import OverturnModel

# Section identifiers look like NBH-CARD-014-3.2 and are stable across the
# corpus. Verification does an exact match on them, so the format is enforced
# at ingest rather than trusted at citation time.
SECTION_ID_PATTERN = r"^NBH-[A-Z]{3,5}-\d{3}(-\d+(\.\d+)*)?$"


class PolicyCriterion(OverturnModel):
    """A single numbered requirement inside a policy section."""

    criterion_id: str = Field(description="Stable id, e.g. NBH-CARD-014-3.2.")
    text: str = Field(description="Verbatim criterion text from the policy.")
    requires_all_of: list[str] = Field(
        default_factory=list,
        description="Sub-criterion ids that must all hold for this one to be met.",
    )
    requires_any_of: list[str] = Field(
        default_factory=list,
        description="Sub-criterion ids of which at least one must hold.",
    )


class PolicySection(OverturnModel):
    """One retrievable chunk of a Northbeck Health Plan medical policy."""

    section_id: str = Field(description="Stable identifier, cited verbatim in appeals.")
    policy_id: str = Field(description="Parent policy, e.g. NBH-CARD-014.")
    policy_title: str
    section_heading: str
    text: str = Field(description="Verbatim section text. Never paraphrased in storage.")
    criteria: list[PolicyCriterion] = Field(default_factory=list)
    effective_date: str | None = None
    version: str | None = None
    source_uri: str | None = Field(
        default=None, description="Where the document lives, for human verification."
    )

    @field_validator("text")
    @classmethod
    def _text_must_be_substantive(cls, v: str) -> str:
        if len(v.strip()) < 20:
            raise ValueError("policy section text is too short to be a real section")
        return v


class RetrievedSection(PolicySection):
    """A policy section plus why Retrieval thinks it is relevant."""

    similarity: float = Field(ge=0.0, le=1.0, description="Vector similarity to the query.")
    matched_query: str = Field(description="The query text that surfaced this section.")


class RetrievalResult(OverturnModel):
    """Everything Retrieval hands to Mapping.

    ``sections`` is the closed world for the rest of the pipeline. Drafting may
    cite nothing outside it, and Verification enforces that.
    """

    query: str
    reformulated_query: str | None = Field(
        default=None,
        description="Set when the first retrieval scored below the floor and was retried.",
    )
    sections: list[RetrievedSection] = Field(default_factory=list)
    top_similarity: float = Field(default=0.0, ge=0.0, le=1.0)
    no_applicable_policy: bool = Field(
        default=False,
        description="True when nothing in the corpus governs this denial. The correct "
        "outcome is to decline to appeal, not to appeal weakly.",
    )

    def section_ids(self) -> set[str]:
        """The only identifiers a draft is permitted to cite."""
        ids: set[str] = set()
        for section in self.sections:
            ids.add(section.section_id)
            ids.update(c.criterion_id for c in section.criteria)
        return ids

    def text_for(self, section_id: str) -> str | None:
        """The verbatim text behind a cited identifier, section or criterion.

        Lives on the schema rather than in Verification because it is not a
        verification concern: it answers "what does the insurer's policy
        actually say here", and the approval screen has to ask that question
        too. A clerk asked to confirm that a quoted passage matches its source
        cannot do it unless the source is on the screen, and the interface is
        not permitted to import from ``agents/`` to find it.
        """
        for section in self.sections:
            if section.section_id == section_id:
                return section.text
            for criterion in section.criteria:
                if criterion.criterion_id == section_id:
                    return criterion.text
        return None
