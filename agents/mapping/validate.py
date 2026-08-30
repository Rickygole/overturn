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


# Words that reverse the meaning of what follows them. A quote that starts just
# after one of these is verbatim and says the opposite of the record.
NEGATIONS = frozenset(
    [
        "not",
        "no",
        "never",
        "without",
        "unless",
        "cannot",
        "denies",
        "denied",
        "denying",
        "absent",
        "lacks",
        "lacking",
        "failed",
        "fails",
        "failing",
        "declined",
        "refused",
        "unable",
        "neither",
        "nor",
    ]
)

# Where a negation stops reaching.
#
# The first version treated "and", "or" and every comma as boundaries, and that
# is backwards: coordination is precisely how clinical negation distributes.
# "Denies chest pain and shortness of breath" negates both. "No evidence of
# infarction, ischaemia, or amyloid deposition" negates all three. Quoting from
# after the coordinator produced verbatim evidence asserting the opposite of the
# record — on CASE-006 it accepted "homicidal ideation, intent or plan" out of a
# note that denies exactly that, about a psychiatric patient.
#
# But a coordinator sometimes does open a new clause: "he no longer senses
# hypoglycaemia AND describes two further episodes" — the second half is not
# negated, and flagging it would drop real evidence.
#
# What separates the two is whether a new predicate begins. A coordinator
# followed by a verb starts one; a coordinator followed by a noun continues the
# list the negation governs. That is the distinction below, and it is a
# heuristic rather than a parser — stated plainly because the failure mode when
# it is wrong is dropping honest evidence.
HARD_BOUNDARIES = frozenset(["but", "however", "although", "though", "whereas"])
COORDINATORS = frozenset({"and", "or", "nor"})
_SENTENCE_MARKS = ".;:"

# Verb forms common in clinical notes. A coordinator followed by one of these is
# opening a new predicate, so the preceding negation does not reach past it.
_PREDICATE_OPENERS = frozenset(
    [
        "describes",
        "reports",
        "states",
        "notes",
        "confirms",
        "denies",
        "has",
        "have",
        "had",
        "is",
        "are",
        "was",
        "were",
        "presented",
        "underwent",
        "completed",
        "demonstrates",
        "shows",
        "showed",
        "remains",
        "remained",
        "continues",
        "continued",
        "began",
        "started",
        "developed",
        "returned",
        "attended",
        "received",
        "requires",
        "required",
        "indicates",
        "indicated",
        "reveals",
        "revealed",
        "exhibits",
    ]
)

MAX_LOOKBACK_WORDS = 25


def drops_a_leading_negation(quote: str, source: str) -> bool:
    """Whether the quote begins inside the reach of a negation it omits.

    Public because Verification asks the same question of a different pair of
    texts. A letter's restatement of a policy criterion can be word-for-word
    verbatim and still say the opposite of the criterion, by starting after the
    "no" — the identical failure this catches in chart evidence.

    This is the truncation attack, and it needs no altered words at all. The
    record says "he did not feel the usual warning symptoms"; the quote is
    "feel the usual warning symptoms". Every word is verbatim, the substring
    test passes, and the evidence now says the opposite of the chart.
    """
    normalised_source = normalise(source)
    normalised_quote = normalise(quote)
    start = normalised_source.find(normalised_quote)
    if start <= 0:
        return False

    quote_words = [w.strip(_SENTENCE_MARKS + ",") for w in normalised_quote.split()]
    if any(word in NEGATIONS for word in quote_words):
        return False  # the quote carries its own negation; nothing was dropped

    preceding = normalised_source[:start].split()
    window_start = max(0, len(preceding) - MAX_LOOKBACK_WORDS)

    # Walk backwards from the word immediately before the quote.
    for position in range(len(preceding) - 1, window_start - 1, -1):
        raw = preceding[position]
        bare = raw.strip(_SENTENCE_MARKS + ",")

        if bare in NEGATIONS:
            return True
        if bare in HARD_BOUNDARIES or any(mark in raw for mark in _SENTENCE_MARKS):
            return False
        if bare in COORDINATORS:
            # Does a new predicate begin here? The word directly after the
            # coordinator answers it: a verb opens a clause, a noun continues
            # the list the negation governs.
            # The word after the coordinator may be the quote's own first word,
            # which is the common case — that is exactly where a truncation
            # starts — so it is not in `preceding` at all.
            after = (
                preceding[position + 1]
                if position + 1 < len(preceding)
                else (quote_words[0] if quote_words else "")
            )
            if after.strip(_SENTENCE_MARKS + ",") in _PREDICATE_OPENERS:
                return False
            # Otherwise it is a coordinated list; the negation carries on.
    return False


def normalise_locator(locator: str) -> str:
    """Canonical form of a chart locator.

    The chart renders locators inside brackets — ``[enc/2026-05-19/endocrinology]``
    — and a model asked to copy one back frequently copies the brackets with it,
    or wraps it in quotes, or keeps the trailing punctuation of the line it sat
    on. None of that is fabrication and none of it should cost a case its
    evidence, but a bare dictionary lookup treats all of it as a locator that
    does not exist.

    That failure was invisible offline, because the offline fixtures were
    authored with exact keys. Against a real model every single locator missed,
    every satisfied verdict was downgraded for want of evidence, and a case that
    plainly qualified came out `declined_no_basis`.
    """
    return locator.strip().strip("[]()<>\"'` ,.;:").strip()


def resolve_locator(locator: str, chart_text: dict[str, str]) -> str | None:
    """The chart key this locator refers to, or None if there is no such place.

    Tolerant about formatting, strict about identity: a locator that does not
    name a real location in the chart still resolves to nothing.
    """
    if locator in chart_text:
        return locator

    wanted = normalise_locator(locator).lower()
    if not wanted:
        return None
    for key in chart_text:
        if normalise_locator(key).lower() == wanted:
            return key
    return None


def quote_is_present(quote: str, locator: str, chart_text: dict[str, str]) -> bool:
    """Whether the quote appears at the locator, and still means what it meant.

    A substring test alone is not enough. "Verbatim" and "faithful" are not the
    same property, and the gap between them does not require changing a single
    word — it only requires stopping early.
    """
    key = resolve_locator(locator, chart_text)
    if key is None:
        return False
    source = chart_text[key]
    if normalise(quote) not in normalise(source):
        return False
    return not drops_a_leading_negation(quote, source)


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
            part for part in (med.name, med.dose, med.route, med.frequency) if part
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
            resolved = resolve_locator(evidence.locator, chart_text)
            if resolved is not None and resolved != evidence.locator:
                # Canonicalise, so the stored citation is one a person can
                # actually follow back into the chart.
                evidence = evidence.model_copy(update={"locator": resolved})
            if resolved is None:
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
                    f"{verdict.criterion_id}: dropped a quote not present at {evidence.locator!r}"
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
                        "but no surviving chart evidence supports it. " + adjusted.reasoning
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
        criterion.criterion_id for section in retrieval.sections for criterion in section.criteria
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
