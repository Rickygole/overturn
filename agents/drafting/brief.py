"""The closed brief handed to the Drafting agent.

Drafting has no retrieval access. This is the whole of what it is allowed to
know, and it is assembled here from the case rather than fetched by the agent.

That is the structural reason Drafting cannot invent a citation: there is no
parameter on its ``run`` method that could carry a ``RetrievalResult``, no
policy identifier reaches it except the ones attached to satisfied criteria,
and its gateway policy grants it no access to the policy corpus. The prompt
tells it not to invent citations, but the prompt is not what stops it.

The brief also carries the administrative facts a letter needs on its face —
member, claim number, date of service, amount in dispute, ordering provider.
Those are *not* material for the argument and the agent is told to leave them
alone; :mod:`agents.drafting.letter` renders them deterministically. They live
here because this is the one place with the case record in hand, and because a
value the model never sees is a value the model cannot get wrong.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from core.schemas.base import utcnow
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

    # --- Letter face. Rendered by agents.drafting.letter, never by the model. ---
    letter_date: date = field(default_factory=lambda: utcnow().date())
    appeal_level: str = "first_level_appeal"
    patient_name: str | None = None
    patient_dob: date | None = None
    date_of_service: date | None = None
    procedure_code: str | None = None
    service_line: str | None = None
    diagnosis_code: str | None = None
    amount_in_dispute: str | None = None
    amount_basis: str | None = None
    date_of_denial: date | None = None
    denial_reason_code: str | None = None
    appeal_deadline: date | None = None
    ordering_provider: str | None = None
    ordering_provider_specialty: str | None = None

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


# A currency figure in the intake footnote, but only one carrying a label that
# says what it is. Intake writes free prose there, and the disputed amount on a
# denial notice is genuinely useful on the appeal — so it is read, but read
# narrowly. A bare "$2,940.00" with no label near it is not accepted, and two
# different labelled amounts are not accepted either: an ambiguous amount is a
# gap, and a gap a clerk fills is cheaper than a wrong number on a letter.
_LABEL = r"(?P<label>estimated allowed|allowed|billed|charged|amount in dispute|disputed)"
_FIGURE = r"\$\s?(?P<amount>\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)"
# Both orders, because the note is a sentence a model wrote and it may put the
# label on either side of the figure.
_LABEL_THEN_FIGURE = re.compile(_LABEL + r"[^.$\n]{0,60}?" + _FIGURE, re.IGNORECASE)
_FIGURE_THEN_LABEL = re.compile(_FIGURE + r"[^.$\n]{0,60}?" + _LABEL, re.IGNORECASE)

_AMOUNT_BASIS = {
    "estimated allowed": "estimated allowed amount, as stated in the determination notice",
    "allowed": "allowed amount, as stated in the determination notice",
    "billed": "amount billed, as stated in the determination notice",
    "charged": "amount charged, as stated in the determination notice",
    "amount in dispute": "as stated in the determination notice",
    "disputed": "as stated in the determination notice",
}


def _money(value: float) -> str:
    return f"${value:,.2f}"


def amount_in_dispute(case: CaseRecord) -> tuple[str | None, str | None]:
    """The money at stake, and what kind of figure it is.

    Two sources, in order of how structured they are. ``billed_amount`` on the
    line item is a typed field and is preferred. Failing that the intake
    footnote is searched, because on a prior-authorisation denial the notice
    states an *estimated allowed* amount rather than a billed one, Intake has
    nowhere typed to put it, and it writes it into ``extraction_notes`` instead.

    Returns ``(None, None)`` rather than a guess. The amount is the one number
    on this letter a payer will reconcile against its own system.
    """
    denial = case.denial
    if denial is None:
        return None, None

    billed = [s.billed_amount for s in denial.services if s.billed_amount is not None]
    if billed:
        return _money(sum(billed)), "amount billed"

    found: list[tuple[str, str]] = []
    for pattern in (_LABEL_THEN_FIGURE, _FIGURE_THEN_LABEL):
        found += [
            (m.group("label").lower(), m.group("amount").replace(",", ""))
            for m in pattern.finditer(denial.extraction_notes or "")
        ]
    if len({amount for _, amount in found}) != 1:
        return None, None  # nothing labelled, or two figures that disagree

    label, raw = found[0]
    try:
        value = float(raw)
    except ValueError:
        return None, None
    return _money(value), _AMOUNT_BASIS.get(label, "as stated in the determination notice")


def ordering_provider(case: CaseRecord, chart) -> tuple[str | None, str | None]:
    """Who ordered the care, derived from the chart the appeal actually rests on.

    The denial notice names a requesting provider, but Intake has no field for
    it, so it does not survive extraction. The chart does name clinicians, and
    the appeal already stands on a specific set of encounters — the ones quoted
    as evidence under the satisfied criteria.

    The rule: among those encounters, the clinician appearing on the most of
    them. It is a derivation, not a lookup, so it is held to a standard — a tie
    at the top yields nothing, and nothing means the letter carries a gap. On
    the eight staged cases the rule agrees with the "Requesting Provider" line
    of the denial notice on every case that reaches drafting; where it would not
    have, it declines instead of guessing.

    A tempting worse rule: take the most recent cited encounter. That names the
    respiratory therapist who fitted the mask on CASE-005 and the case manager
    on CASE-006, neither of whom ordered anything.
    """
    if chart is None or case.criteria is None:
        return None, None

    encounters = {e.locator: e for e in chart.encounters}
    cited = [
        encounters[evidence.locator]
        for verdict in case.criteria.appealable_verdicts()
        for evidence in verdict.evidence
        if evidence.locator in encounters
    ]
    counts = Counter((e.clinician, e.specialty) for e in cited if e.clinician)
    ranked = counts.most_common(2)
    if not ranked:
        return None, None
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None, None  # No clear answer. Say so rather than pick one.
    (name, specialty), _ = ranked[0]
    return name, specialty


def _chart_for(case_id: str):
    """The patient chart, or ``None`` if it is not on disk.

    Read here for four identity fields — date of birth, and the ordering
    clinician's name and specialty — and for nothing else. The Drafting *agent*
    still never sees a chart: the brief is assembled outside it, and only these
    scalars cross into the letter's face. A missing chart is not an error at
    this stage; the letter gets gaps instead.
    """
    try:
        from agents.mapping.charts import load_chart

        return load_chart(case_id)
    except Exception:  # chart absent or unreadable — the letter says so
        return None


# Sentinel for "no chart was supplied, go and look for one". Distinct from an
# explicit ``chart=None``, which means "this case has no chart" and must not
# quietly turn into a disk read that finds one.
_LOOK_ON_DISK = object()


def build_brief(
    case: CaseRecord,
    instructions: Sequence[str] = (),
    *,
    chart=_LOOK_ON_DISK,
    today: date | None = None,
) -> DraftingBrief:
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

    # The same service, written for the face of the letter rather than for the
    # prompt: the procedure code sits with the line item it belongs to, and the
    # remaining items are counted in English. "receiver and transmitter and 1
    # other line item(s) (CPT/HCPCS E2103)" attaches the code to the wrong
    # thing, which on a claim is not a cosmetic problem.
    if services:
        first = services[0]
        service_line = first.description
        if first.procedure_code:
            service_line += f" (CPT/HCPCS {first.procedure_code})"
        remaining = len(services) - 1
        if remaining:
            plural = "" if remaining == 1 else "s"
            service_line += f", and {remaining} further line item{plural} on the same claim"
    else:
        service_line = description

    if chart is _LOOK_ON_DISK:
        chart = _chart_for(case.case_id)
    provider, specialty = ordering_provider(case, chart)
    amount, basis = amount_in_dispute(case)

    return DraftingBrief(
        case_id=case.case_id,
        attempt=len(case.drafts) + 1,
        payer_name=case.denial.payer_name,
        claim_number=case.denial.claim_number,
        member_id=case.denial.member_id,
        service_description=description,
        service_line=service_line,
        denial_reason_text=case.denial.denial_reason_text,
        verdicts=verdicts,
        verbatim_sections=verbatim,
        chart_summary=case.criteria.chart_summary,
        revision_instructions=tuple(instructions),
        letter_date=today or utcnow().date(),
        appeal_level=str(case.appeal_level),
        patient_name=case.denial.patient_name or (chart.name if chart else None),
        patient_dob=case.denial.patient_dob or (chart.date_of_birth if chart else None),
        date_of_service=services[0].date_of_service if services else None,
        procedure_code=services[0].procedure_code if services else None,
        diagnosis_code=services[0].diagnosis_code if services else None,
        amount_in_dispute=amount,
        amount_basis=basis,
        date_of_denial=case.denial.date_of_denial,
        denial_reason_code=case.denial.denial_reason_code,
        appeal_deadline=case.denial.appeal_deadline,
        ordering_provider=provider,
        ordering_provider_specialty=specialty,
    )


def render(brief: DraftingBrief) -> str:
    """The brief as the prompt text Drafting receives."""
    lines = [
        f"PAYER: {brief.payer_name}",
        f"CLAIM NUMBER: {brief.claim_number or '(not stated in the denial letter)'}",
        f"MEMBER ID: {brief.member_id or '(not stated in the denial letter)'}",
        f"SERVICE DENIED: {brief.service_description}",
        "",
        "WRITE THE ARGUMENT ONLY. The date line, the payer's appeals address, the "
        "member and claim block, the date of service, the amount in dispute, the "
        "ordering provider and NPI, the salutation and the signature block are "
        "added around your text from the case record. Do not write them, and do "
        "not leave placeholders for them.",
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
