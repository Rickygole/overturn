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
    return {
        word
        for word in re.findall(r"[a-z0-9]+", text.lower())
        if word not in BOILERPLATE and len(word) > 3
    }


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

    scored: list[tuple[str, int]] = []
    for section in retrieval.sections:
        for criterion in section.criteria:
            shared = reason_terms & _terms(criterion.text)
            if len(shared) >= MIN_SHARED_TERMS:
                scored.append((criterion.criterion_id, len(shared)))

    if not scored:
        return []
    top = max(score for _, score in scored)
    return sorted(cid for cid, score in scored if score == top)


def has_answerable_dispute(matrix: CriteriaMatrix, disputed: list[str]) -> tuple[bool, str]:
    """Whether the chart documents anything the payer actually questioned.

    Returns ``(answerable, explanation)``. The explanation is written for the
    billing clerk who has to decide what to do next, so it says which criterion
    is missing rather than reporting a score.
    """
    if not disputed:
        # The reason was boilerplate. Fall back to the general test rather than
        # declining a case we simply could not parse.
        return matrix.has_appealable_basis, (
            "The payer's stated reason is too generic to tie to a specific "
            "criterion, so this was assessed on the criteria as a whole."
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
