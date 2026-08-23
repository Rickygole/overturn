"""The Mapping agent: the analytical core.

For each criterion in the retrieved policy, decide whether the chart documents
what it asks for, and point at where. Everything downstream is derived from this
table, and nothing downstream may assert a fact that is not in it.

Sections are mapped one at a time rather than all at once. A single call
covering a whole policy encourages a model to reason about the case as a whole
and then back-fill verdicts to match its overall impression, which is the exact
failure this agent exists to avoid. One section per call keeps the question
narrow, and the results are merged here.
"""

from __future__ import annotations

from dataclasses import dataclass

from agents.base import OverturnAgent
from agents.mapping.validate import sanitise_matrix
from core.audit import Recording
from core.schemas.chart import PatientChart
from core.schemas.criteria import CriteriaMatrix
from core.schemas.enums import AgentName
from core.schemas.policy import RetrievalResult, RetrievedSection

from .prompts import MAPPING_SYSTEM


@dataclass(frozen=True)
class MappingRequest:
    """A chart and the policy it is being measured against."""

    chart: PatientChart
    retrieval: RetrievalResult

    def to_firestore(self) -> dict:
        return {
            "patient_id": self.chart.patient_id,
            "policy_ids": sorted({s.policy_id for s in self.retrieval.sections}),
            "section_ids": sorted(s.section_id for s in self.retrieval.sections),
        }


def criteria_sections(retrieval: RetrievalResult) -> list[RetrievedSection]:
    """Only sections that actually contain numbered criteria.

    Scope and appeal-rights sections have no criteria to map, and asking a model
    to map them invites it to invent some.
    """
    return [section for section in retrieval.sections if section.criteria]


class MappingAgent(OverturnAgent[MappingRequest, CriteriaMatrix]):
    """Produces the criteria matrix."""

    name = AgentName.MAPPING
    operation = "map_section"

    def _summarise(self, request: MappingRequest) -> str:
        sections = criteria_sections(request.retrieval)
        return (
            f"{len(sections)} criteria-bearing section(s), "
            f"{sum(len(s.criteria) for s in sections)} criteria, "
            f"chart with {len(request.chart.encounters)} encounters"
        )

    def _execute(
        self,
        case_id: str,
        request: MappingRequest,
        rec: Recording,
        attempt: int,
    ) -> CriteriaMatrix:
        sections = criteria_sections(request.retrieval)
        chart_text = request.chart.render()

        merged = CriteriaMatrix(case_id=case_id)
        input_tokens = 0
        output_tokens = 0
        model_used: str | None = None

        for section in sections:
            partial, response = self.llm.structured(
                agent=self.name.value,
                operation=self.operation,
                system=MAPPING_SYSTEM,
                prompt=self._render_prompt(case_id, section, chart_text),
                schema=CriteriaMatrix,
                model=self.settings.model_flash,
            )
            merged.verdicts.extend(partial.verdicts)
            if partial.chart_summary and not merged.chart_summary:
                merged.chart_summary = partial.chart_summary
            model_used = response.model
            input_tokens += response.input_tokens or 0
            output_tokens += response.output_tokens or 0

        clean, adjustments = sanitise_matrix(merged, request.retrieval, request.chart)

        rec.model = model_used
        rec.input_tokens = input_tokens or None
        rec.output_tokens = output_tokens or None
        rec.decision = self._describe(clean, adjustments)
        if adjustments:
            rec.extra["adjustments"] = adjustments
        return clean

    @staticmethod
    def _render_prompt(case_id: str, section: RetrievedSection, chart_text: str) -> str:
        criteria = "\n\n".join(f"CRITERION {c.criterion_id}\n{c.text}" for c in section.criteria)
        return (
            f"POLICY {section.policy_id} — {section.policy_title}\n"
            f"SECTION {section.section_id} — {section.section_heading}\n\n"
            f"Section text, verbatim:\n{section.text}\n\n"
            f"Criteria to evaluate, one verdict each:\n\n{criteria}\n\n"
            f"{'=' * 70}\n"
            f"PATIENT CHART\n"
            f"Locators are shown in brackets. Copy them exactly when citing evidence.\n"
            f"{'=' * 70}\n\n{chart_text}\n\n"
            f"Return one verdict for each of the {len(section.criteria)} criteria above, "
            f"with case_id {case_id!r}."
        )

    @staticmethod
    def _describe(matrix: CriteriaMatrix, adjustments: list[str]) -> str:
        from collections import Counter

        counts = Counter(v.verdict.value for v in matrix.verdicts)
        summary = ", ".join(f"{n} {verdict}" for verdict, n in sorted(counts.items()))
        detail = f"{len(matrix.verdicts)} criteria evaluated ({summary})"
        detail += f"; appealable basis: {matrix.has_appealable_basis}"
        if adjustments:
            detail += f"; {len(adjustments)} verdict(s) adjusted in validation"
        if matrix.unmapped_criteria:
            detail += f"; {len(matrix.unmapped_criteria)} criteria not evaluated"
        return detail
