"""Python checks on the criteria matrix a model produced.

Mapping is the agent whose output everything downstream trusts. Drafting only
ever sees satisfied criteria; Verification checks the letter's claims against
this matrix. If a fabricated chart quote gets in here, it is laundered into an
appeal that looks perfectly well-supported all the way down.

So the model's answer is checked before it is believed, and every check is a
string operation:

  * A criterion id that is not in the retrieved set is dropped. The model does
    not get to invent criteria the payer never published.
  * An evidence locator that is not in the chart is dropped. A pointer to a note
    that does not exist is a fabricated citation.
  * An evidence quote that is not actually present at that locator is dropped.
    Pointing at a real note and describing something it does not say is the
    subtler version of the same failure.
  * A verdict of ``satisfied`` with no surviving evidence is **downgraded** to
    ``insufficient_documentation``, never dropped. Silence would read downstream
    as "the policy did not ask for that". Downgrading says the truthful thing:
    the record does not document it.

The direction of every adjustment is one-way. Nothing here can promote a
verdict, only weaken it.
"""

from __future__ import annotations

import re
import unicodedata

from core.schemas.chart import PatientChart
from core.schemas.criteria import CriteriaMatrix, CriterionVerdict
from core.schemas.enums import CriterionVerdictValue
from core.schemas.policy import RetrievalResult

# Below this, a "quote" is too short to be evidence of anything.
MIN_QUOTE_CHARS = 12


def normalise(text: str) -> str:
    """Collapse whitespace and unicode variance for substring comparison.

    A model reflowing a line break or turning a hyphen into an en dash is not
    fabrication, and treating it as such would reject honest evidence. Changing
    the words is fabrication. This normalises the former and preserves the
    latter.
    """
    text = unicodedata.normalize("NFKD", text)
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = re.sub(r"[‐-―]", "-", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def quote_is_present(quote: str, locator: str, chart_text: dict[str, str]) -> bool:
    """Whether the quote actually appears at the locator it claims."""
    source = chart_text.get(locator)
    if source is None:
        return False
    return normalise(quote) in normalise(source)


def chart_text_by_locator(chart: PatientChart) -> dict[str, str]:
    """Every citable location in the chart mapped to its full text."""
    index: dict[str, str] = {}
    for encounter in chart.encounters:
        index[encounter.locator] = " ".join(
            part
            for part in (
                encounter.encounter_type,
                encounter.reason,
                encounter.clinician,
                encounter.note,
            )
            if part
        )
    for lab in chart.labs:
        index[lab.locator] = (
            f"{lab.observed_date} {lab.name} {lab.value} {lab.unit or ''} "
            f"{lab.reference_range or ''}"
        )
    for med in chart.medications:
        index[med.locator] = " ".join(
            part
            for part in (med.name, med.dose, med.route, med.frequency)
            if part
        )
    return index


def sanitise_matrix(
    raw: CriteriaMatrix,
    retrieval: RetrievalResult,
    chart: PatientChart,
) -> tuple[CriteriaMatrix, list[str]]:
    """Return a matrix that only makes claims the inputs support.

    The second element is a list of human-readable adjustments, which goes into
    the audit event. A silent correction is indistinguishable from no error.
    """
    valid_ids = retrieval.section_ids()
    chart_text = chart_text_by_locator(chart)
    adjustments: list[str] = []
    kept: list[CriterionVerdict] = []

    for verdict in raw.verdicts:
        if verdict.criterion_id not in valid_ids:
            adjustments.append(
                f"dropped verdict on {verdict.criterion_id}: not a criterion in the "
                f"retrieved policy"
            )
            continue

        surviving = []
        for evidence in verdict.evidence:
            if evidence.locator not in chart_text:
                adjustments.append(
                    f"{verdict.criterion_id}: dropped evidence citing "
                    f"{evidence.locator!r}, which is not in the chart"
                )
                continue
            if len(evidence.quote.strip()) < MIN_QUOTE_CHARS:
                adjustments.append(
                    f"{verdict.criterion_id}: dropped a quote too short to be evidence"
                )
                continue
            if not quote_is_present(evidence.quote, evidence.locator, chart_text):
                adjustments.append(
                    f"{verdict.criterion_id}: dropped a quote not present at "
                    f"{evidence.locator!r}"
                )
                continue
            surviving.append(evidence)

        adjusted = verdict.model_copy(update={"evidence": surviving})

        if adjusted.verdict == CriterionVerdictValue.SATISFIED and not surviving:
            adjusted = adjusted.model_copy(
                update={
                    "verdict": CriterionVerdictValue.INSUFFICIENT_DOCUMENTATION,
                    "reasoning": (
                        "Downgraded automatically: the criterion was marked satisfied "
                        "but no surviving chart evidence supports it. "
                        + adjusted.reasoning
                    ),
                }
            )
            adjustments.append(
                f"{verdict.criterion_id}: downgraded satisfied to "
                f"insufficient_documentation, no verifiable chart evidence"
            )

        kept.append(adjusted)

    evaluated = {v.criterion_id for v in kept}
    criterion_ids = {
        criterion.criterion_id
        for section in retrieval.sections
        for criterion in section.criteria
    }
    unmapped = sorted(criterion_ids - evaluated)

    return (
        CriteriaMatrix(
            case_id=raw.case_id,
            policy_ids=sorted({s.policy_id for s in retrieval.sections}),
            verdicts=kept,
            chart_summary=raw.chart_summary,
            unmapped_criteria=unmapped,
        ),
        adjustments,
    )
