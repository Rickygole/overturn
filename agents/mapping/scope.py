"""Which of a policy's coverage sections govern the service that was denied.

A policy can cover more than one thing. NBH-CARD-014 covers two::

    ## NBH-CARD-014-3 — Coverage Criteria: Cardiac Magnetic Resonance Imaging
    ## NBH-CARD-014-4 — Coverage Criteria: Coronary CT Angiography

CASE-003 is a cardiac MRI denial, and Mapping ruled on the coronary CT criteria
anyway, because it was handed every criterion the policy contains. Three
verdicts came back and all three were noise. One of them was worse than noise:
NBH-CARD-014-4.2 asks whether the member has undergone coronary
revascularisation, this patient had CABG in 2014, and the matrix therefore
recorded — in our own analysis, on the screen a payer's reviewer would be shown
— an argument against our own appeal drawn from criteria that do not apply to
the requested study.

`not_applicable` was already in the verdict enum and already meant "the
criterion does not apply to this request". It was simply never used.

**What this module does not do.** It does not correct the model. It runs before
the model is asked anything, and its output is a list of sections that are not
put to the model at all; the criteria in them are recorded as `not_applicable`
with a reason naming the policy's own heading. That is a scope determination
made from the policy's table of contents, not a clinical judgement, and it is
the kind of question a string comparison answers better than a model does.

**How it decides, and when it refuses to.** Only headings that name a service
are considered — the corpus writes them as ``Coverage Criteria: <service>`` —
and only where one policy has two or more of them. Terms shared between two
such headings in the same policy are struck out first, because a word that
appears in both cannot tell them apart; NBH-MSK-022 splits its criteria into
"Non-Urgent Presentation" and "Urgent Presentation", which are two
presentations of one service and not two services, and striking the shared
words leaves nothing for a denied service to match. The rule fires only when
exactly one section's distinctive terms appear in the denied service. Anything
else — a tie, no match, an unparseable heading — leaves every section in scope,
which is today's behaviour.

Two further refusals, both deliberate:

  * a criterion the payer's own letter turned on is never scoped out. If the
    determination argues about it, it applies, whatever the headings say.
  * sections whose heading names no service — Documentation Requirements,
    Exclusions — are never scoped out. They are written to serve the whole
    policy and frequently cross-reference the section that does apply.
"""

from __future__ import annotations

import re

from agents.mapping.dispute import primary_disputed_criteria
from core.schemas.criteria import CriterionVerdict
from core.schemas.denial import DenialExtraction
from core.schemas.enums import CriterionVerdictValue
from core.schemas.policy import RetrievalResult, RetrievedSection

# The corpus convention: a heading that scopes its criteria to one service
# qualifies itself after a colon. "Coverage Criteria: Coronary CT Angiography".
_QUALIFIED_HEADING = re.compile(
    r"^(?P<kind>[^:]*\b(?:coverage|criteria)\b[^:]*):\s*(?P<service>.+)$", re.IGNORECASE
)

_WORD = re.compile(r"[a-z][a-z0-9]*")

# Words that carry no service identity. "Imaging" is left in deliberately: it
# distinguishes an imaging study from a device or a programme, and it is only
# ever used here after terms shared between two headings have been struck.
_UNINFORMATIVE = frozenset(
    [
        "and",
        "or",
        "of",
        "the",
        "a",
        "an",
        "for",
        "with",
        "without",
        "in",
        "to",
        "coverage",
        "criteria",
        "criterion",
        "service",
        "services",
        "material",
        "including",
        "include",
        "includes",
        "quantity",
        "unspecified",
    ]
)

# Abbreviations a denial letter uses where a policy heading spells the modality
# out, and the reverse. Expansion runs on both sides, so "cardiac MRI" and
# "Cardiac Magnetic Resonance Imaging" meet in the middle. Kept small on
# purpose: every entry is a claim that two strings name the same thing, and a
# wrong one silently scopes out a section that applied.
_ABBREVIATIONS = {
    "mri": {"magnetic", "resonance", "imaging"},
    "mr": {"magnetic", "resonance"},
    "cmr": {"cardiac", "magnetic", "resonance", "imaging"},
    "ct": {"computed", "tomography"},
    "cta": {"computed", "tomography", "angiography"},
    "ccta": {"coronary", "computed", "tomography", "angiography"},
    "cgm": {"continuous", "glucose", "monitoring"},
    "cpap": {"continuous", "positive", "airway", "pressure"},
    "iop": {"intensive", "outpatient"},
}


def _terms(text: str) -> set[str]:
    """Service-identifying words, with known abbreviations expanded."""
    words = {word for word in _WORD.findall(text.lower()) if word not in _UNINFORMATIVE}
    expanded = set(words)
    for word in words:
        expanded |= _ABBREVIATIONS.get(word, set())
    return expanded


def service_qualifier(section: RetrievedSection) -> str | None:
    """The service a coverage-criteria heading scopes itself to, if it names one."""
    match = _QUALIFIED_HEADING.match(section.section_heading.strip())
    return match.group("service").strip() if match else None


def denied_service_description(denial: DenialExtraction) -> str | None:
    """What the letter says was denied, in the letter's own words."""
    described = [service.description.strip() for service in denial.services if service.description]
    return "; ".join(described) or None


def out_of_scope_sections(
    denial: DenialExtraction | None, retrieval: RetrievalResult
) -> dict[str, str]:
    """Sections whose criteria govern a service other than the one denied.

    Maps ``section_id`` to a sentence saying why, written for the screen: it
    names both headings and the denied service, so a reader can check the
    determination against the policy's own table of contents.

    Empty whenever the question cannot be answered confidently, which is most
    of the corpus. Every policy but two has a single, unqualified
    "Coverage Criteria" section and this returns nothing for them.
    """
    if denial is None:
        return {}

    description = denied_service_description(denial)
    if not description:
        return {}
    service_terms = _terms(description)
    if not service_terms:
        return {}

    disputed = set(primary_disputed_criteria(denial, retrieval))

    candidates: dict[str, list[tuple[RetrievedSection, str]]] = {}
    for section in retrieval.sections:
        if not section.criteria:
            continue
        qualifier = service_qualifier(section)
        if qualifier is None:
            continue
        candidates.setdefault(section.policy_id, []).append((section, qualifier))

    out_of_scope: dict[str, str] = {}
    for sections in candidates.values():
        if len(sections) < 2:
            continue

        # A word appearing in two headings of the same policy cannot tell them
        # apart. Strike it before scoring, or "urgent" in both
        # "Urgent Presentation" and "Non-Urgent Presentation" decides a case.
        seen: dict[str, int] = {}
        for _, qualifier in sections:
            for term in _terms(qualifier):
                seen[term] = seen.get(term, 0) + 1
        distinctive = {
            section.section_id: {t for t in _terms(qualifier) if seen[t] == 1}
            for section, qualifier in sections
        }

        scores = {
            section.section_id: len(service_terms & distinctive[section.section_id])
            for section, _ in sections
        }
        matched = [section_id for section_id, score in scores.items() if score]
        if len(matched) != 1:
            continue  # a tie, or nothing matched: leave every section in scope

        governing_id = matched[0]
        governing = next(q for s, q in sections if s.section_id == governing_id)

        for section, qualifier in sections:
            if section.section_id == governing_id:
                continue
            if any(c.criterion_id in disputed for c in section.criteria):
                # The payer's own letter argues about a criterion in here, so it
                # applies regardless of what the heading says.
                continue
            out_of_scope[section.section_id] = (
                f"{section.section_id} sets the coverage criteria for {qualifier}. "
                f"The denied service is {description!r}, which this policy covers "
                f"under {governing_id} ({governing}). Criteria for a different "
                f"service do not apply to this request."
            )

    return out_of_scope


def not_applicable_verdicts(
    case_id: str, section: RetrievedSection, reason: str
) -> list[CriterionVerdict]:
    """One `not_applicable` row per criterion in a section that does not apply.

    Rows rather than silence. A criterion left out of the matrix reads
    downstream as one Mapping could not reach, and
    ``CriteriaMatrix.has_appealable_basis`` counts those against the case under
    the worst-case rule. "This does not apply" and "we do not know" are
    different facts, and the matrix has a value for each.
    """
    return [
        CriterionVerdict(
            criterion_id=criterion.criterion_id,
            criterion_text=criterion.text,
            section_id=section.section_id,
            verdict=CriterionVerdictValue.NOT_APPLICABLE,
            evidence=[],
            reasoning=reason,
            confidence=1.0,
        )
        for criterion in section.criteria
    ]
