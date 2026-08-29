"""Verification output: the reason this system can be trusted to send a letter.

Verification answers three questions about a draft, in order:

1. Does every cited section id exist in the retrieved source set?
2. Does the source text actually support what the letter says it says?
3. Does the letter assert any clinical fact absent from the criteria matrix?

A failure on any one of them rejects the draft. The failure text is fed back to
Drafting as revision instructions rather than discarded.
"""

from __future__ import annotations

from pydantic import Field, computed_field

from core.schemas.base import OverturnModel


class VerificationFinding(OverturnModel):
    """One specific problem with a draft."""

    check: str = Field(
        description="Which check failed: citation_exists, citation_accurate, or assertion_grounded."
    )
    severity: str = Field(description="'fatal' rejects the draft; 'advisory' is noted only.")
    locus: str = Field(description="The citation id or the asserted sentence at fault.")
    detail: str = Field(description="What is wrong, phrased as an instruction Drafting can act on.")
    source_text: str | None = Field(
        default=None, description="The verbatim policy text that contradicts the claim, if any."
    )


class VerificationResult(OverturnModel):
    """The verdict on one drafting attempt."""

    case_id: str
    attempt: int = Field(ge=1)

    citations_checked: int = 0
    citations_nonexistent: list[str] = Field(
        default_factory=list,
        description="Cited ids not present in the retrieval result. Always fatal.",
    )
    citations_unsupported: list[str] = Field(
        default_factory=list,
        description="Ids that exist but whose source text does not support the claim made.",
    )
    ungrounded_assertions: list[str] = Field(
        default_factory=list,
        description="Clinical claims in the letter with no row in the criteria matrix.",
    )

    findings: list[VerificationFinding] = Field(default_factory=list)
    checked_by_model: str | None = None
    checked_by_backend: str | None = Field(
        default=None,
        description="'vertex'/'adk' for a real model, 'scripted' for the offline "
        "stub. The claim that a second model caught an overclaim is only worth "
        "anything if it is true, so the interface has to be able to tell.",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def passed(self) -> bool:
        """A draft passes only when all three checks are clean."""
        return not (
            self.citations_nonexistent
            or self.citations_unsupported
            or self.ungrounded_assertions
            or any(f.severity == "fatal" for f in self.findings)
        )

    def revision_instructions(self) -> list[str]:
        """Feedback handed back to Drafting for the next attempt."""
        instructions = [f.detail for f in self.findings if f.severity == "fatal"]
        for cid in self.citations_nonexistent:
            instructions.append(
                f"Remove the citation to {cid}. That identifier does not exist in the "
                f"retrieved policy set. Cite only identifiers you were given."
            )
        for cid in self.citations_unsupported:
            instructions.append(
                f"The citation to {cid} misstates what that section says. Re-read the "
                f"verbatim text and either restate it accurately or drop the point."
            )
        for claim in self.ungrounded_assertions:
            instructions.append(
                f"Remove or rephrase this assertion, which is not supported by any row in "
                f"the criteria matrix: {claim!r}"
            )
        return instructions
