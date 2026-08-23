"""The closed brief handed to the Drafting agent.

Drafting has no retrieval access. This is the whole of what it is allowed to
know, and it is assembled here from the case rather than fetched by the agent.

That is the structural reason Drafting cannot invent a citation: there is no
parameter on its ``run`` method that could carry a ``RetrievalResult``, no
policy identifier reaches it except the ones attached to satisfied criteria,
and its gateway policy grants it no access to the policy corpus. The prompt
tells it not to invent citations, but the prompt is not what stops it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from core.schemas.case import CaseRecord
from core.schemas.criteria import CriterionVerdict


@dataclass(frozen=True)
class DraftingBrief:
    """Everything Drafting may work from, and nothing else."""

    case_id: str
    attempt: int
    payer_name: str
    claim_number: str | None
    member_id: str | None
    service_description: str
    denial_reason_text: str
    verdicts: tuple[CriterionVerdict, ...]
    verbatim_sections: Mapping[str, str]
    chart_summary: str | None
    revision_instructions: tuple[str, ...]

    @property
    def citable_ids(self) -> set[str]:
        """The only identifiers that may appear in the letter."""
        ids = {v.criterion_id for v in self.verdicts}
        ids |= {v.section_id for v in self.verdicts}
        return ids

    def to_firestore(self) -> dict:
        return {
            "case_id": self.case_id,
            "attempt": self.attempt,
            "criteria": [v.criterion_id for v in self.verdicts],
            "revision_instructions": list(self.revision_instructions),
        }


class NothingToArgue(ValueError):
    """No satisfied criteria, so there is no appeal to write."""


def build_brief(case: CaseRecord, instructions: Sequence[str] = ()) -> DraftingBrief:
    """Assemble the brief from a case record.

    Only satisfied criteria with surviving evidence are included. A criterion
    the chart does not document is not an argument, and handing it to a writer
    invites one to be manufactured.
    """
    if case.criteria is None or case.retrieval is None or case.denial is None:
        raise NothingToArgue(f"case {case.case_id} is not ready for drafting")

    verdicts = tuple(case.criteria.appealable_verdicts())
    if not verdicts:
        raise NothingToArgue(f"case {case.case_id} has no satisfied criteria with chart evidence")

    # Only the sections those criteria live in, quoted verbatim. A section the
    # argument does not rest on is not context, it is an opportunity.
    needed = {v.section_id for v in verdicts}
    verbatim = {
        section.section_id: section.text
        for section in case.retrieval.sections
        if section.section_id in needed
    }

    services = case.denial.services
    description = services[0].description if services else "the denied service"
    if len(services) > 1:
        description += f" and {len(services) - 1} other line item(s)"

    return DraftingBrief(
        case_id=case.case_id,
        attempt=len(case.drafts) + 1,
        payer_name=case.denial.payer_name,
        claim_number=case.denial.claim_number,
        member_id=case.denial.member_id,
        service_description=description,
        denial_reason_text=case.denial.denial_reason_text,
        verdicts=verdicts,
        verbatim_sections=verbatim,
        chart_summary=case.criteria.chart_summary,
        revision_instructions=tuple(instructions),
    )


def render(brief: DraftingBrief) -> str:
    """The brief as the prompt text Drafting receives."""
    lines = [
        f"PAYER: {brief.payer_name}",
        f"CLAIM NUMBER: {brief.claim_number or '(not stated in the denial letter)'}",
        f"MEMBER ID: {brief.member_id or '(not stated in the denial letter)'}",
        f"SERVICE DENIED: {brief.service_description}",
        "",
        "THE PAYER'S STATED REASON FOR DENIAL, VERBATIM:",
        brief.denial_reason_text,
        "",
        "=" * 70,
        "SATISFIED CRITERIA — these are the only arguments available to you.",
        "=" * 70,
    ]

    for verdict in brief.verdicts:
        lines += [
            "",
            f"CRITERION {verdict.criterion_id}  (in section {verdict.section_id})",
            f"  Criterion text: {verdict.criterion_text}",
            f"  Why it is met:  {verdict.reasoning}",
            "  Documented in the chart by:",
        ]
        for evidence in verdict.evidence:
            lines.append(f'    - [{evidence.locator}] "{evidence.quote}"')

    lines += ["", "=" * 70, "VERBATIM POLICY TEXT FOR THE SECTIONS YOU MAY CITE", "=" * 70]
    for section_id, text in sorted(brief.verbatim_sections.items()):
        lines += ["", f"--- {section_id} ---", text]

    lines += [
        "",
        "=" * 70,
        "CITABLE IDENTIFIERS — citing anything not on this list will be rejected:",
        "  " + ", ".join(sorted(brief.citable_ids)),
        "=" * 70,
    ]

    if brief.revision_instructions:
        lines += [
            "",
            "!" * 70,
            f"THIS IS ATTEMPT {brief.attempt}. A previous draft was REJECTED in verification.",
            "Fix every one of the following. Do not restate the rejected claims in "
            "softer language; remove them or replace them with something the "
            "criteria above actually support.",
            "!" * 70,
        ]
        for index, instruction in enumerate(brief.revision_instructions, 1):
            lines.append(f"  {index}. {instruction}")

    return "\n".join(lines)
