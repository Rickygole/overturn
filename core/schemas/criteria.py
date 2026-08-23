"""The criteria matrix: the analytical core of an Overturn case.

Mapping produces one row per policy criterion. Everything downstream is
derived from this table, and nothing downstream is permitted to assert a fact
that does not appear in it.
"""

from __future__ import annotations

from pydantic import Field, computed_field

from core.schemas.base import OverturnModel
from core.schemas.enums import CriterionVerdictValue


class ChartEvidence(OverturnModel):
    """A pointer back into the patient chart.

    ``locator`` is what makes a verdict auditable: a human can open the chart at
    that spot and see the same thing the agent saw.
    """

    locator: str = Field(
        description="Where in the chart this came from, e.g. 'encounter 2026-03-14 / assessment'."
    )
    quote: str = Field(description="Verbatim excerpt from the chart. Never paraphrased.")
    document_type: str | None = Field(
        default=None, description="Progress note, imaging report, medication list, and so on."
    )
    observed_date: str | None = None


class CriterionVerdict(OverturnModel):
    """One row of the matrix: does the chart document this criterion?

    This is a documentation-matching judgement, not a clinical one. The agent
    never decides whether care is appropriate, only whether the record contains
    what the payer's own published policy asks for.
    """

    criterion_id: str = Field(description="Must exist in the retrieved section set.")
    criterion_text: str = Field(description="Verbatim criterion text, carried for the draft.")
    section_id: str = Field(description="Parent section id, cited in the appeal.")

    verdict: CriterionVerdictValue
    evidence: list[ChartEvidence] = Field(
        default_factory=list,
        description="Empty is only valid for not_satisfied or insufficient_documentation.",
    )
    reasoning: str = Field(
        description="Why the evidence does or does not meet the criterion, in one or two sentences."
    )
    confidence: float = Field(ge=0.0, le=1.0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def usable_in_appeal(self) -> bool:
        """Only satisfied criteria with actual evidence reach the Drafting agent."""
        return self.verdict == CriterionVerdictValue.SATISFIED and bool(self.evidence)


class CriteriaMatrix(OverturnModel):
    """The full mapping of policy criteria against one patient's chart."""

    case_id: str
    policy_ids: list[str] = Field(default_factory=list)
    verdicts: list[CriterionVerdict] = Field(default_factory=list)
    chart_summary: str | None = Field(
        default=None, description="One paragraph of context, carried into the draft."
    )
    unmapped_criteria: list[str] = Field(
        default_factory=list,
        description="Criterion ids Mapping could not evaluate at all, stated rather than hidden.",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def satisfied_count(self) -> int:
        return sum(1 for v in self.verdicts if v.usable_in_appeal)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_appealable_basis(self) -> bool:
        """Whether there is anything worth writing an appeal about.

        One satisfied criterion is not a case. We require that the satisfied
        rows outnumber the outright failures, otherwise the honest action is to
        decline and tell the clerk why.
        """
        failed = sum(1 for v in self.verdicts if v.verdict == CriterionVerdictValue.NOT_SATISFIED)
        return self.satisfied_count > 0 and self.satisfied_count >= failed

    def appealable_verdicts(self) -> list[CriterionVerdict]:
        """Exactly what Drafting is allowed to see."""
        return [v for v in self.verdicts if v.usable_in_appeal]
