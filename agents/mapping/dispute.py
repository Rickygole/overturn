"""Which criteria the payer actually disputed.

A case can have most criteria documented and still be hopeless, and the
difference is not a count. It is *which* criterion is missing.

Two cases in the corpus make the point. In CASE-003 the payer denied because the
initial evaluation was supposedly not inconclusive; the chart documents exactly
that, in an echocardiogram report that says so in terms. There is a real appeal
there. In CASE-006 the payer denied because a less intensive level of care was
not shown to have been tried; the chart is silent on it. Seven other criteria
are documented beautifully and none of them answer the question that was asked.

Counting satisfied criteria cannot tell those apart — CASE-006 clears any
threshold you set. So the test is: **does the chart document at least one
criterion the payer's own stated reason contests?** If yes, there is something
to argue. If no, the gap is in the documentation rather than in the
determination, and the useful thing to tell a billing clerk is to go and get the
missing note, not to send a letter that argues around the question.

This is deterministic. The payer's reason is text, the criteria are text, and
matching them is a string operation — so it is one, and no model is asked.
"""

from __future__ import annotations

import re

from core.schemas.criteria import CriteriaMatrix
from core.schemas.denial import DenialExtraction
from core.schemas.enums import CriterionVerdictValue
from core.schemas.policy import RetrievalResult

# Boilerplate that appears in every denial letter and every policy criterion.
# Left in, these dominate the overlap and every criterion looks equally disputed.
BOILERPLATE = frozenset(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "is",
        "are",
        "be",
        "been",
        "that",
        "this",
        "it",
        "its",
        "for",
        "from",
        "with",
        "by",
        "on",
        "at",
        "as",
        "not",
        "no",
        "clinical",
        "information",
        "submitted",
        "does",
        "establish",
        "medically",
        "necessary",
        "under",
        "plan",
        "applicable",
        "medical",
        "policy",
        "member",
        "record",
        "documented",
        "documentation",
        "must",
        "has",
        "have",
        "following",
        "criteria",
        "requested",
        "request",
        "review",
        "reviewer",
        "determination",
        "submitted",
        "shall",
        "may",
    ]
)

MIN_SHARED_TERMS = 2


def _terms(text: str) -> set[str]:
    """Distinctive words, keeping clinical acronyms.

    The length filter was ``len(word) > 3``, which deleted every three-letter
    acronym in the domain — a1c, cgm, mri, ecg, osa, ahi, iop — while keeping
    "2026". Those acronyms are the most discriminating words a denial reason
    contains, and dropping them left several realistic reasons matching nothing
    at all.
    """
    return {
        word
        for word in re.findall(r"[a-z][a-z0-9]*|[0-9]+[a-z]+", text.lower())
        if word not in BOILERPLATE
        and (len(word) > 3 or any(c.isdigit() for c in word) or len(word) == 3)
    }


def _rarity(retrieval: RetrievalResult) -> dict[str, float]:
    """How discriminating each term is across the criteria being considered.

    Counting shared terms equally is what let CASE-008 tie. That letter recites
    what the reviewer *considered* — the electrocardiogram, the echocardiogram
    report — before stating what it actually turned on, which was an unresolved
    MRI contraindication. The recitation matched two satisfied criteria as
    strongly as the holding matched the unmet one, one of the ties was
    satisfied, and the case read as answerable. The system would have drafted a
    letter arguing points the reviewer explicitly conceded and never touched the
    one it denied on.

    "Echocardiogram" appears in several criteria of that policy.
    "Contraindication" appears in exactly one. Weighting by that difference is
    what separates a recitation from a holding.
    """
    import math

    criteria = [c for section in retrieval.sections for c in section.criteria]
    if not criteria:
        return {}

    document_frequency: dict[str, int] = {}
    for criterion in criteria:
        for term in _terms(criterion.text):
            document_frequency[term] = document_frequency.get(term, 0) + 1

    total = len(criteria)
    return {
        term: math.log((total - count + 0.5) / (count + 0.5) + 1.0)
        for term, count in document_frequency.items()
    }


def _is_exclusion(section_heading: str) -> bool:
    """Exclusion sections are not things a chart can satisfy in the payer's favour.

    Asking whether an exclusion is "satisfied" inverts the sign: a satisfied
    exclusion means the service is excluded, which is the opposite of an
    answerable dispute.
    """
    return "exclusion" in section_heading.lower()


def disputed_criteria(denial: DenialExtraction, retrieval: RetrievalResult) -> list[str]:
    """Criterion ids the payer's stated reason appears to contest.

    Returns every criterion sharing enough distinctive vocabulary with the
    denial reason, best first. Empty when the reason is pure boilerplate, which
    is a real thing payers send and which the caller has to treat as "we cannot
    tell" rather than "nothing is disputed".
    """
    reason_terms = _terms(denial.denial_reason_text)
    if not reason_terms:
        return []

    scored: list[tuple[str, int]] = []
    for section in retrieval.sections:
        for criterion in section.criteria:
            shared = reason_terms & _terms(criterion.text)
            if len(shared) >= MIN_SHARED_TERMS:
                scored.append((criterion.criterion_id, len(shared)))

    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return [criterion_id for criterion_id, _ in scored]


def primary_disputed_criteria(denial: DenialExtraction, retrieval: RetrievalResult) -> list[str]:
    """Only the criteria the reason matches most strongly, ties included.

    A denial reason has one primary subject, and everything below the top tier
    is vocabulary bleed — "care", "intensive" and "level" appear in half the
    criteria of a behavioural health policy. Accepting any weak match as
    "disputed" made a case answerable whenever *something* adjacent happened to
    be documented, which is how a chart that is silent on the only question the
    payer asked reads as a strong appeal.

    Ties are kept because a reason genuinely can contest two things at once, and
    documenting either one of them is a real answer.
    """
    reason_terms = _terms(denial.denial_reason_text)
    if not reason_terms:
        return []

    weights = _rarity(retrieval)
    scored: list[tuple[str, float]] = []
    for section in retrieval.sections:
        if _is_exclusion(section.section_heading):
            continue
        for criterion in section.criteria:
            shared = reason_terms & _terms(criterion.text)
            if len(shared) < MIN_SHARED_TERMS:
                continue
            scored.append((criterion.criterion_id, sum(weights.get(term, 1.0) for term in shared)))

    if not scored:
        return []

    # Ties only within a small margin, so a genuinely dual dispute still counts
    # while a recitation that merely brushes the top does not.
    top = max(score for _, score in scored)
    return sorted(cid for cid, score in scored if score >= top * 0.85)


def has_answerable_dispute(matrix: CriteriaMatrix, disputed: list[str]) -> tuple[bool, str]:
    """Whether the chart documents anything the payer actually questioned.

    Returns ``(answerable, explanation)``. The explanation is written for the
    billing clerk who has to decide what to do next, so it says which criterion
    is missing rather than reporting a score.
    """
    if not disputed:
        # The reason was boilerplate, or extraction did not recover it. Fall back
        # to the general test rather than declining a case we simply could not
        # parse — but say so, because "we could not tell" and "we checked" are
        # different facts and the clerk is entitled to know which one this is.
        return matrix.has_appealable_basis, (
            "The payer's stated reason could not be tied to a specific criterion, "
            "so this was assessed on the criteria as a whole. Read the denial "
            "letter before relying on this one."
        )

    verdicts = {v.criterion_id: v for v in matrix.verdicts}
    answered = [
        criterion_id
        for criterion_id in disputed
        if verdicts.get(criterion_id)
        and verdicts[criterion_id].verdict is CriterionVerdictValue.SATISFIED
        and verdicts[criterion_id].evidence
    ]

    if answered:
        return True, (
            f"The payer's reason turns on {', '.join(disputed[:3])}, and the chart "
            f"documents {', '.join(answered[:3])}. There is a point to argue."
        )

    unmet = [
        criterion_id
        for criterion_id in disputed[:3]
        if criterion_id in verdicts
        and verdicts[criterion_id].verdict is CriterionVerdictValue.INSUFFICIENT_DOCUMENTATION
    ]
    detail = "the chart is silent on exactly that" if unmet else "the chart does not address it"
    return False, (
        f"The payer denied on {', '.join(disputed[:3])}, and {detail}. "
        f"Other criteria are well documented, but none of them answer the "
        f"question that was actually asked. The gap is in the documentation "
        f"rather than in the determination — the useful next step is to obtain "
        f"that note, not to send an appeal that argues around it."
    )
