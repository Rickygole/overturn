"""The deterministic half of verification.

Two of the three checks do not need a model and therefore do not get one. A
model cannot be wrong about a question it is never asked, and "does this
identifier appear in this set" is a set membership test.

This is the check that catches a hallucinated citation, it runs in
microseconds, it costs nothing, and it cannot have a bad day.
"""

from __future__ import annotations

from core.schemas.criteria import CriteriaMatrix
from core.schemas.draft import AppealDraft
from core.schemas.enums import CriterionVerdictValue
from core.schemas.policy import RetrievalResult
from core.schemas.verification import VerificationFinding


def check_citation_existence(
    draft: AppealDraft, retrieval: RetrievalResult
) -> tuple[list[str], list[VerificationFinding]]:
    """Every cited identifier must exist in the retrieved policy set."""
    known = retrieval.section_ids()
    missing = sorted(draft.cited_ids() - known)

    findings = [
        VerificationFinding(
            check="citation_exists",
            severity="fatal",
            locus=section_id,
            detail=(
                f"The letter cites {section_id}, which does not exist in the "
                f"retrieved policy. Remove it. Only these identifiers were "
                f"provided: {', '.join(sorted(known)[:12])}"
                + ("..." if len(known) > 12 else "")
            ),
        )
        for section_id in missing
    ]
    return missing, findings


def check_supporting_criteria(
    draft: AppealDraft, matrix: CriteriaMatrix
) -> list[VerificationFinding]:
    """A citation may only rest on criteria the matrix marked satisfied.

    The subtler sibling of the existence check. A citation to a section that
    genuinely exists, resting on a criterion the chart does not document, is
    an argument built on nothing — and it will resolve correctly if you only
    check that the identifier is real.
    """
    verdicts = {v.criterion_id: v for v in matrix.verdicts}
    findings: list[VerificationFinding] = []

    for citation in draft.citations:
        for criterion_id in citation.supporting_criterion_ids:
            verdict = verdicts.get(criterion_id)
            if verdict is None:
                findings.append(
                    VerificationFinding(
                        check="citation_exists",
                        severity="fatal",
                        locus=criterion_id,
                        detail=(
                            f"Citation to {citation.section_id} rests on criterion "
                            f"{criterion_id}, which was never evaluated against the "
                            f"chart. Drop the point."
                        ),
                    )
                )
            elif verdict.verdict != CriterionVerdictValue.SATISFIED:
                findings.append(
                    VerificationFinding(
                        check="citation_accurate",
                        severity="fatal",
                        locus=criterion_id,
                        detail=(
                            f"Citation to {citation.section_id} rests on criterion "
                            f"{criterion_id}, which the chart does NOT satisfy "
                            f"(verdict: {verdict.verdict.value}). Remove this "
                            f"argument rather than restating it more cautiously."
                        ),
                    )
                )
    return findings


def check_assertions_enumerated(draft: AppealDraft) -> list[VerificationFinding]:
    """A letter making claims with an empty assertion list is not checkable."""
    if draft.clinical_assertions or not draft.citations:
        return []
    return [
        VerificationFinding(
            check="assertion_grounded",
            severity="fatal",
            locus="clinical_assertions",
            detail=(
                "The letter cites policy criteria but enumerates no clinical "
                "assertions. Every factual claim about the patient must be listed "
                "so it can be checked against the criteria matrix."
            ),
        )
    ]


def resolve_section_text(section_id: str, retrieval: RetrievalResult) -> str | None:
    """The verbatim text behind a cited identifier, section or criterion."""
    for section in retrieval.sections:
        if section.section_id == section_id:
            return section.text
        for criterion in section.criteria:
            if criterion.criterion_id == section_id:
                return criterion.text
    return None


def evidence_corpus(matrix: CriteriaMatrix) -> str:
    """Every chart quote the matrix stands on, as one block.

    What a clinical assertion in the letter is checked against.
    """
    quotes: list[str] = []
    for verdict in matrix.verdicts:
        if verdict.verdict != CriterionVerdictValue.SATISFIED:
            continue
        quotes.extend(f"[{e.locator}] {e.quote}" for e in verdict.evidence)
    return "\n".join(quotes)
