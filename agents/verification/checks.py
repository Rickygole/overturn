"""The deterministic half of verification.

Two of the three checks do not need a model and therefore do not get one. A
model cannot be wrong about a question it is never asked, and "does this
identifier appear in this set" is a set membership test.

This is the check that catches a hallucinated citation, it runs in
microseconds, it costs nothing, and it cannot have a bad day.

:func:`is_faithful_restatement` was added on 29 August and belongs to the same
family, pointed the other way. It does not reject anything. It answers one
narrow question — *is this claim the source text itself, restated?* — and when
the answer is yes the citation is verified without a model call at all. The
model was rejecting verbatim restatements on CASE-003, twice, and no prompt is
a guarantee. A claim that is the source's own words, carrying the source's own
negations, cannot misstate the source, and that is a string operation rather
than a judgement.

It is one-way on purpose: a claim it does not recognise is not condemned, it is
simply passed to the model exactly as before. Nothing here can fail a draft.
"""

from __future__ import annotations

import re

from agents.mapping.validate import drops_a_leading_negation, normalise
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
                f"provided: {', '.join(sorted(known)[:12])}" + ("..." if len(known) > 12 else "")
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


# Framing a writer puts in front of a criterion before restating it. None of
# these words changes what the criterion requires: "Requires that X" asserts
# exactly X where X is already a requirement. The live model rejected
# NBH-CARD-014-3.5 on CASE-003 for exactly this shape.
_FRAMING = re.compile(
    r"^[\s\"'(]*"
    r"(?:(?:this|the)\s+)?"
    r"(?:section|criterion|policy|plan|paragraph|provision)?\s*"
    r"(?:requires?|provides?|states?|says?|specifies|mandates|establishes|sets\s+out)"
    r"(?:\s+that)?\s*[:,]?\s*",
    re.IGNORECASE,
)

# Below this, a claim is too short for containment to mean anything. Half a
# clause lifted out of a long criterion is not a restatement of it, and the
# model is the right thing to ask about it.
MIN_RESTATEMENT_CHARS = 40


def strip_framing(claim: str) -> str:
    """The claim with any leading "Requires that"-style framing removed."""
    return _FRAMING.sub("", claim.strip(), count=1).strip()


def is_faithful_restatement(claim: str, source: str) -> bool:
    """Whether the claim is the source text restated, and nothing more.

    True only when the claim — with framing stripped, whitespace collapsed and
    case folded — appears inside the source verbatim, *and* does not begin
    inside the reach of a negation it left behind. That second condition is the
    truncation attack, and it is why this cannot be a plain substring test:
    "a relative contraindication exists, the medical record documents that it
    has been addressed" is word-for-word out of NBH-CARD-014-3.5 and drops the
    "There is no contraindication ... or" that makes it conditional.

    False means "not obviously a restatement", not "wrong". The caller asks the
    model in that case.
    """
    for candidate in (strip_framing(claim), claim.strip()):
        trimmed = candidate.strip().rstrip(" .;:,")
        normalised = normalise(trimmed)
        if len(normalised) < MIN_RESTATEMENT_CHARS:
            continue
        if normalised not in normalise(source):
            continue
        if drops_a_leading_negation(trimmed, source):
            continue
        return True
    return False


def resolve_section_text(section_id: str, retrieval: RetrievalResult) -> str | None:
    """The verbatim text behind a cited identifier, section or criterion.

    Kept as a name here because two call sites read better for it. The lookup
    itself moved onto :class:`RetrievalResult` so the approval screen can ask
    the same question without importing from ``agents/``.
    """
    return retrieval.text_for(section_id)


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
