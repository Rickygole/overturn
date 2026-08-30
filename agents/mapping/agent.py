"""The Mapping agent: the analytical core.

For each criterion in the retrieved policy, decide whether the chart documents
what it asks for, and point at where. Everything downstream is derived from this
table, and nothing downstream may assert a fact that is not in it.

Sections are mapped one at a time rather than all at once. A single call
covering a whole policy encourages a model to reason about the case as a whole
and then back-fill verdicts to match its overall impression, which is the exact
failure this agent exists to avoid. One section per call keeps the question
narrow, and the results are merged here.

Not every criteria-bearing section is asked about. A policy can cover more than
one service — NBH-CARD-014 covers cardiac MRI in section 3 and coronary CT
angiography in section 4 — and criteria written for the modality that was not
requested are `not_applicable`, which is a verdict this system had and did not
use. See ``agents/mapping/scope.py`` for how that is decided and, more to the
point, for the cases where it refuses to decide.
"""

from __future__ import annotations

from dataclasses import dataclass

from agents.base import OverturnAgent
from agents.mapping.scope import (
    denied_service_description,
    not_applicable_verdicts,
    out_of_scope_sections,
)
from agents.mapping.validate import sanitise_matrix
from core.audit import Recording
from core.schemas.chart import PatientChart
from core.schemas.criteria import CriteriaMatrix
from core.schemas.denial import DenialExtraction
from core.schemas.enums import AgentName
from core.schemas.policy import RetrievalResult, RetrievedSection

from .prompts import MAPPING_SYSTEM


@dataclass(frozen=True)
class MappingRequest:
    """A chart, the policy it is being measured against, and what was denied.

    ``denial`` is optional because the schema allows a caller to have nothing
    but a chart and a policy, and because every existing caller predates it.
    Without it the agent maps every criterion in the retrieved policy, which is
    what it did before — including criteria for services nobody requested.
    """

    chart: PatientChart
    retrieval: RetrievalResult
    denial: DenialExtraction | None = None

    def to_firestore(self) -> dict:
        return {
            "patient_id": self.chart.patient_id,
            "policy_ids": sorted({s.policy_id for s in self.retrieval.sections}),
            "section_ids": sorted(s.section_id for s in self.retrieval.sections),
            "denied_service": (
                denied_service_description(self.denial) if self.denial is not None else None
            ),
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
        out_of_scope = out_of_scope_sections(request.denial, request.retrieval)
        in_scope = [s for s in sections if s.section_id not in out_of_scope]
        scoped = (
            f", {len(sections) - len(in_scope)} section(s) not for the denied service"
            if out_of_scope
            else ""
        )
        return (
            f"{len(in_scope)} criteria-bearing section(s), "
            f"{sum(len(s.criteria) for s in in_scope)} criteria{scoped}, "
            f"chart with {len(request.chart.encounters)} encounters"
        )

    def _execute(
        self,
        case_id: str,
        request: MappingRequest,
        rec: Recording,
        attempt: int,
    ) -> CriteriaMatrix:
        out_of_scope = out_of_scope_sections(request.denial, request.retrieval)
        sections = criteria_sections(request.retrieval)
        chart_text = request.chart.render()
        denied_service = (
            denied_service_description(request.denial) if request.denial is not None else None
        )

        merged = CriteriaMatrix(case_id=case_id)
        input_tokens = 0
        output_tokens = 0
        model_used: str | None = None

        for section in sections:
            if (reason := out_of_scope.get(section.section_id)) is not None:
                # Not asked. A criterion written for a service nobody requested
                # has an answer already, and putting it to a model produces a
                # ruling on the wrong study — on CASE-003, an argument against
                # our own appeal, recorded inside our own criteria matrix.
                merged.verdicts.extend(not_applicable_verdicts(case_id, section, reason))
                continue

            partial, response = self.llm.structured(
                agent=self.name.value,
                operation=self.operation,
                system=MAPPING_SYSTEM,
                prompt=self._render_prompt(case_id, section, chart_text, denied_service),
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
        rec.decision = self._describe(clean, adjustments, out_of_scope)
        if adjustments:
            rec.extra["adjustments"] = adjustments
        if out_of_scope:
            rec.extra["out_of_scope_sections"] = out_of_scope
        return clean

    @staticmethod
    def _render_prompt(
        case_id: str,
        section: RetrievedSection,
        chart_text: str,
        denied_service: str | None = None,
    ) -> str:
        criteria = "\n\n".join(f"CRITERION {c.criterion_id}\n{c.text}" for c in section.criteria)
        # First, because it is what every verdict below is relative to. A
        # criterion governing some other service is not unmet, it is not
        # applicable, and the model cannot tell the difference without knowing
        # what was asked for.
        denied = (
            f"SERVICE DENIED, as the payer's letter names it:\n  {denied_service}\n\n"
            if denied_service
            else ""
        )
        return (
            f"{denied}"
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
    def _describe(
        matrix: CriteriaMatrix,
        adjustments: list[str],
        out_of_scope: dict[str, str] | None = None,
    ) -> str:
        from collections import Counter

        counts = Counter(v.verdict.value for v in matrix.verdicts)
        summary = ", ".join(f"{n} {verdict}" for verdict, n in sorted(counts.items()))
        detail = f"{len(matrix.verdicts)} criteria evaluated ({summary})"
        detail += f"; appealable basis: {matrix.has_appealable_basis}"
        if out_of_scope:
            detail += (
                f"; {', '.join(sorted(out_of_scope))} not put to the model, "
                f"criteria for a service other than the one denied"
            )
        if adjustments:
            # Each adjustment already says what changed and why. Reporting only
            # a count read as an unexplained correction layer applied after the
            # fact, which is exactly what it must never be allowed to become.
            shown = "; ".join(adjustments[:3])
            more = f" (+{len(adjustments) - 3} more)" if len(adjustments) > 3 else ""
            detail += f"; validation changed {len(adjustments)}: {shown}{more}"
        if matrix.unmapped_criteria:
            detail += f"; {len(matrix.unmapped_criteria)} criteria not evaluated"
        return detail
