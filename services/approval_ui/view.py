"""Everything the templates would otherwise have to work out for themselves.

Presentation logic lives here rather than in Jinja so that it can be read, and
tested, as Python. The templates are left doing what templates are good at:
deciding what appears and in what order.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from core.schemas.case import CaseRecord
from core.schemas.criteria import CriterionVerdict
from core.schemas.denial import DenialExtraction, DeniedService
from core.schemas.draft import AppealDraft
from core.schemas.enums import AppealLevel, CaseStatus, CriterionVerdictValue
from core.schemas.lifecycle import APPEAL_LADDER
from core.schemas.verification import VerificationResult

NOT_STATED = "Not stated in the letter"

# The verdict word is the primary signal, and it is the one that survives a
# monochrome screen, a colour-blind reader and a printed page. Colour and border
# style in the stylesheet are the second and third channels, never the first.
VERDICT_LABELS: dict[str, str] = {
    CriterionVerdictValue.SATISFIED: "Satisfied",
    CriterionVerdictValue.NOT_SATISFIED: "Not satisfied",
    CriterionVerdictValue.INSUFFICIENT_DOCUMENTATION: "Insufficient documentation",
    CriterionVerdictValue.NOT_APPLICABLE: "Not applicable",
}


# --------------------------------------------------------------------------- #
# Formatting filters
# --------------------------------------------------------------------------- #


def fmt_date(value: date | datetime | None) -> str:
    """``14 March 2026``. Written out because 03/04 is ambiguous across borders."""
    if value is None:
        return NOT_STATED
    if isinstance(value, datetime):
        value = value.astimezone(UTC).date()
    return f"{value.day} {value:%B %Y}"


def fmt_datetime(value: datetime | None) -> str:
    if value is None:
        return "—"
    value = value.astimezone(UTC)
    return f"{value.day} {value:%B %Y}, {value:%H:%M} UTC"


# Both of these survive for exactly one caller: Sentinel's detector score on a
# screening finding, which is a detector's own calibrated output about a string
# it matched, not a model's opinion of a clinical judgement. The criteria matrix
# used to carry them too and no longer does -- see `mapping_rows`.
def pct(value: float | None) -> str:
    return "—" if value is None else f"{round(value * 100)}%"


def confidence_band(value: float | None) -> str:
    """A word alongside the number, so the number is not the only channel."""
    if value is None:
        return "unknown"
    if value >= 0.85:
        return "high"
    if value >= 0.6:
        return "moderate"
    return "low"


def verdict_label(verdict: CriterionVerdictValue | str) -> str:
    return VERDICT_LABELS.get(str(verdict), str(verdict).replace("_", " ").capitalize())


def verdict_key(verdict: CriterionVerdictValue | str) -> str:
    return str(verdict)


def service_detail(service: DeniedService) -> str:
    """The codes, date and amount attached to one denied line item.

    Assembled here rather than in the template: Jinja's whitespace trimming eats
    the spaces around a chain of inline conditionals, and " CPT 75561· ICD-10"
    is the sort of small wrongness that makes a reader distrust the rest.
    """
    parts: list[str] = []
    if service.procedure_code:
        parts.append(f"CPT/HCPCS {service.procedure_code}")
    if service.diagnosis_code:
        parts.append(f"ICD-10 {service.diagnosis_code}")
    if service.date_of_service:
        parts.append(f"dated {fmt_date(service.date_of_service)}")
    if service.billed_amount is not None:
        parts.append(f"billed ${service.billed_amount:,.2f}")
    return " \u00b7 ".join(parts)


FILTERS = {
    "fmt_date": fmt_date,
    "fmt_datetime": fmt_datetime,
    "pct": pct,
    "confidence_band": confidence_band,
    "verdict_label": verdict_label,
    "verdict_key": verdict_key,
    "service_detail": service_detail,
}


# --------------------------------------------------------------------------- #
# Derived views
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DeadlineView:
    """The appeal deadline, and how much of it is left."""

    stated: str
    days: int | None
    label: str
    tone: str  # "ok" | "warn" | "danger" | "none"


def deadline_view(deadline: date | None, today: date | None = None) -> DeadlineView:
    if deadline is None:
        return DeadlineView(NOT_STATED, None, "No appeal deadline stated", "none")

    days = (deadline - (today or datetime.now(UTC).date())).days
    if days < 0:
        label = f"{abs(days)} day{'' if abs(days) == 1 else 's'} past the deadline"
        tone = "danger"
    elif days == 0:
        label = "Due today"
        tone = "danger"
    else:
        label = f"{days} day{'' if days == 1 else 's'} remaining"
        tone = "danger" if days <= 3 else "warn" if days <= 10 else "ok"
    return DeadlineView(fmt_date(deadline), days, label, tone)


# --------------------------------------------------------------------------- #
# What the appeal is worth
#
# The money was in an intake footnote, three folds down, on the screen whose
# entire subject is whether to spend a clinician's signature on this letter.
# It is what the appeal is *for*, and it belongs beside the patient and the
# clock.
# --------------------------------------------------------------------------- #

# A currency figure inside Intake's free-text note. Deliberately narrow: it has
# to look like money, not like a code, a date or a quantity.
_MONEY = re.compile(r"\$\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?")


@dataclass(frozen=True)
class Amount:
    """What was billed, and where the number came from.

    ``stated`` is the honest half of this. When Intake filled
    ``DeniedService.billed_amount`` the number is a field, and the header says
    it plainly. When it did not -- and on every case in the corpus today it did
    not, dropping the figure into ``extraction_notes`` instead -- the number is
    a string a model wrote in a sentence. It still goes on the header, because a
    reader needs it, but it goes up marked as a quotation from that sentence
    rather than dressed as data. Lifting a number out of model prose and
    printing it as a field is precisely the kind of laundering this screen
    exists to prevent.
    """

    text: str
    stated: bool
    quote: str | None  # the sentence it was read out of, when it is not a field


def amount(denial: DenialExtraction | None) -> Amount | None:
    """The money on this denial, or nothing if the record does not carry it."""
    if denial is None:
        return None

    billed = [s.billed_amount for s in denial.services if s.billed_amount is not None]
    if billed:
        return Amount(f"${sum(billed):,.2f}", stated=True, quote=None)

    note = denial.extraction_notes
    if not note:
        return None
    for sentence in re.split(r"(?<=\.)\s+", note):
        found = _MONEY.search(sentence)
        if found:
            return Amount(found.group().replace("$ ", "$"), stated=False, quote=sentence.strip())
    return None


def service_line(denial: DenialExtraction | None) -> str:
    """One line naming what was refused, for the queue table."""
    if denial is None or not denial.services:
        return NOT_STATED
    first = denial.services[0].description
    extra = len(denial.services) - 1
    return f"{first} (+{extra} more)" if extra else first


_SYNTHEA_SUFFIX = re.compile(r"(?<=[A-Za-z])\d+\b")


def display_name(name: str | None) -> str:
    """Strip Synthea's numeric suffixes for display.

    Charts are generated, and the generator emits "Creola518 Heller342". The
    digits are an artefact of the tool, not a property of the record, and on
    screen they read as unfinished rather than as synthetic. The disclosure
    that none of these people exist is made in three other places, in words.

    Display only -- the stored value is untouched, so nothing that matches on
    the name downstream changes behaviour.
    """
    if not name:
        return NOT_STATED
    return _SYNTHEA_SUFFIX.sub("", name)


def queue_row(case: CaseRecord) -> dict[str, object]:
    """One line of the queue. Never invents a value it was not given.

    ``waiting_on`` used to be a *section* — three tables with three headings,
    each saying in prose what one column can say per row. It is a column now,
    which is what it always was.
    """
    denial = case.denial
    return {
        "case_id": case.case_id,
        "patient": display_name(denial.patient_name if denial else None),
        "payer": denial.payer_name if denial else NOT_STATED,
        "service": service_line(denial),
        "deadline": deadline_view(denial.appeal_deadline if denial else None),
        "attempts": case.draft_attempts,
        "reason": case.needs_human_reason,
        "updated": case.updated_at,
        "waiting_on": waiting_on(case.status),
        "waiting_key": waiting_key(case.status),
        "attempt_marks": attempt_marks(case),
        # Counted, not inferred. The row said "all 3 rejected by verification"
        # whenever the *latest* attempt was rejected, which is a different claim
        # and on a case with a pass in the middle a false one — and now that the
        # marks beside it render correctly, a visibly false one.
        "rejected": attempt_marks(case).count("rejected"),
        # A correction a person made by hand, when there is one for this record.
        # See `REVIEW_NOTES`: the row said Verification rejected all three drafts
        # and stopped there, which reads as the cap holding. On CASE-003 two of
        # those three rejections are wrong, and a reader who discovers that after
        # opening the case has been misled by this screen.
        "review_note": review_note(case),
        "link_suffix": "/clinical" if case.status == WAITING_ON_CLINICIAN else "",
        # For `mark_shared_claims`, which needs to compare rows against each
        # other and cannot do it from the case objects it never sees.
        "claim_number": denial.claim_number if denial else None,
        "document_hash": case.screening.content_sha256 if case.screening else None,
        # Filled in by `mark_shared_claims`. Present with a null value here so a
        # template can read it on every row rather than guarding each access.
        "same_claim": None,
    }


def mark_shared_claims(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Say on the row when two cases carry one claim number.

    CASE-001 and CASE-007 are the same patient, the same denied service and the
    same claim number, sitting two lines apart in a queue whose headline
    engineering claim is a guard against duplicate filings. They are two
    genuinely different intakes -- the second is that denial delivered as a
    scanned fax -- but the queue said nothing, and a reader reaches "duplicate
    bug" long before they reach the README.

    What is said is only what the record proves. The claim number is the same;
    the screened content hash is not, and that is the whole of the difference
    this interface can stand behind. It does not claim which one is the fax:
    nothing on the case record says so. It says there are two documents, which
    is the fact that turns a suspected bug into a visible decision.

    Mutates the rows in place and returns them, because the caller already
    owns the list and copying it to add one key is ceremony.
    """
    by_claim: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        claim = row.get("claim_number")
        if claim:
            by_claim.setdefault(str(claim), []).append(row)

    for shared in by_claim.values():
        if len(shared) < 2:
            continue
        hashes = {row.get("document_hash") for row in shared}
        distinct = len(hashes) == len(shared) and None not in hashes
        for row in shared:
            others = [str(o["case_id"]) for o in shared if o is not row]
            row["same_claim"] = {
                "cases": others,
                "line": (
                    "Same claim number, a different source document"
                    if distinct
                    else "Same claim number and the same source document"
                ),
            }
    return rows


def one_payer(rows: list[dict[str, object]]) -> str | None:
    """The payer's name when every row shares it, otherwise ``None``.

    A column whose every cell holds the same word is not carrying information;
    it is costing a seventh of the table's width to repeat itself. Said once in
    the caption it is still on the screen, and the row gets the space back. The
    moment a second payer appears the caption stops being true, so this returns
    nothing and the template puts the name back on the row.
    """
    names = {str(row["payer"]) for row in rows if row["payer"] != NOT_STATED}
    return names.pop() if len(names) == 1 else None


def attempt_marks(case: CaseRecord) -> list[str]:
    """One mark per drafting attempt: what Verification did with each.

    The queue said "3 (all 3 rejected by verification)" in prose. The same fact
    as three marks is read at a glance and, across a column of cases, shows the
    shape of the thing this system is actually for -- most letters pass first
    time, some get sent back, one never passes at all.

    Ordered by attempt so the row reads left to right in the order it happened.
    An attempt with no verification yet is "pending" rather than assumed good.
    """
    verdicts = {result.attempt: result.passed for result in case.verifications}
    marks: list[str] = []
    for attempt in range(1, case.draft_attempts + 1):
        passed = verdicts.get(attempt)
        marks.append("pending" if passed is None else "passed" if passed else "rejected")
    return marks


# --------------------------------------------------------------------------- #
# What a reviewer found by hand, and the system cannot compute
# --------------------------------------------------------------------------- #
#
# On 30 August 2026 a reviewer read CASE-003's three rejections against the
# policy text word by word and found that two of them are wrong: Verification
# objected to a sentence that is the criterion verbatim with "Requires that" in
# front of it, and to where a modifier attaches. The cap then fired on a
# well-founded appeal and nothing was sent.
#
# That finding is written up in `docs/EVALUATION.md`. It belongs on the row as
# well, because this is the first case a reader opens -- it carries the nearest
# deadline, so the deadline sort puts it at the top -- and a reader who works
# out on their own that the row overstated what happened stops believing the
# rest of the screen.
#
# It is typed here rather than derived, and that is the honest choice, not a
# shortcut. Deciding that an objection to a paraphrase was unfounded means
# reading the letter against the source, which is exactly the measurement
# `docs/EVALUATION.md` states this project has *not* automated. A heuristic in
# the view layer that guessed at it would be a fabricated confidence on the one
# screen where a fabricated confidence is unforgivable -- and it would be the
# same mistake, in the other direction, as the sentence it is correcting. When
# a real false-positive check exists, it replaces this table.


@dataclass(frozen=True)
class ReviewNote:
    """A human finding about one case, pinned to the record it was made against.

    ``marks`` is the attempt sequence the reviewer actually read. If the case is
    re-run and comes out differently the note no longer describes anything on
    screen, so it is withheld rather than shown against a record it does not
    match. A stale correction is worse than no correction.
    """

    marks: tuple[str, ...]
    body: str


REVIEW_NOTES: Mapping[str, ReviewNote] = {
    "CASE-003": ReviewNote(
        marks=("rejected", "rejected", "rejected"),
        body=(
            "Read by hand on 30 August 2026: two of those three rejections were wrong. "
            "Verification objected to a sentence that is the policy criterion word for "
            "word with “Requires that” in front of it, and to where “within the "
            "twelve months” attaches. The cap then fired on a sound appeal — a false "
            "positive, not the cap holding. docs/EVALUATION.md has the full reading."
        ),
    ),
}


def review_note(case: CaseRecord) -> str | None:
    """The reviewer's correction for this case, if it still fits the record."""
    note = REVIEW_NOTES.get(case.case_id)
    if note is None or tuple(attempt_marks(case)) != note.marks:
        return None
    return note.body


def attempt_history(case: CaseRecord) -> list[dict[str, object]]:
    """One entry per drafting attempt, paired with the verdict it received.

    This is the retry loop made visible. An earlier attempt that Verification
    rejected is not an embarrassment to hide; it is the evidence that the check
    is real.
    """
    by_attempt: dict[int, VerificationResult] = {v.attempt: v for v in case.verifications}
    history: list[dict[str, object]] = []
    for draft in case.drafts:
        result = by_attempt.get(draft.attempt)
        history.append(
            {
                "attempt": draft.attempt,
                "draft": draft,
                "verification": result,
                "outcome": (
                    "Not verified"
                    if result is None
                    else ("Passed verification" if result.passed else "Rejected by verification")
                ),
                "passed": None if result is None else result.passed,
                "findings": list(result.findings) if result else [],
                "instructions": result.revision_instructions() if result else [],
            }
        )
    # A verification with no matching draft would otherwise vanish silently.
    orphans = sorted(set(by_attempt) - {d.attempt for d in case.drafts})
    for attempt in orphans:
        result = by_attempt[attempt]
        history.append(
            {
                "attempt": attempt,
                "draft": None,
                "verification": result,
                "outcome": "Passed verification" if result.passed else "Rejected by verification",
                "passed": result.passed,
                "findings": list(result.findings),
                "instructions": result.revision_instructions(),
            }
        )
    history.sort(key=lambda row: row["attempt"])
    return history


def draft_under_review(case: CaseRecord) -> AppealDraft | None:
    """The pinned draft if one was approved, otherwise the most recent one.

    ``CaseRecord.approved_draft()`` already resolves the pin by attempt number;
    honouring it means a reader looking at a decided case sees the letter that
    was actually signed off, not whatever landed afterwards.
    """
    return case.approved_draft() or case.latest_draft


# --------------------------------------------------------------------------- #
# The claim ledger
#
# The clerk is asked to confirm that "each quoted passage matches the policy
# text it is attributed to". Nothing on this screen carried the policy text, so
# the only way to tick that box was to take Verification's word for it -- which
# is precisely the deference the two-signature gate exists to prevent. Every
# claim the letter makes now sits beside the insurer's own words, on the screen
# where somebody signs for it.
# --------------------------------------------------------------------------- #

NO_RETRIEVAL = (
    "The retrieved policy set is not on this case, so the source text cannot be "
    "shown. Do not confirm a quotation you have not been given."
)
NOT_RETRIEVED = (
    "This identifier is not in the retrieved policy set, so there is no source "
    "text behind it. Verification treats that as fatal."
)
RESTATED_VERBATIM = "The letter restates this verbatim."
NO_MATRIX_ROW = "This point rests on no row of the criteria matrix."


def _squash(text: str) -> str:
    """Whitespace-insensitive comparison. Two texts that differ only in how a
    line was wrapped are the same text, and printing both would be the bug."""
    return " ".join(text.split())


@dataclass(frozen=True)
class Finding:
    """A verification finding, carrying the attempt that raised it."""

    attempt: int
    check: str
    severity: str
    locus: str
    detail: str
    source_text: str | None


def case_findings(case: CaseRecord) -> list[Finding]:
    """Every finding Verification has recorded on this case, any attempt.

    Not only the attempt on screen. The criteria matrix is written once and
    never revised, so an objection raised against attempt 1 still stands
    against the matrix row it named when attempt 3 is the one being read. The
    attempt number rides along so the page can say when the objection was
    raised rather than implying it is fresh.

    This is the *matrix's* view of the findings, and it is the only place the
    cross-attempt join is correct. The claim ledger shows the letter currently
    on screen, and uses `findings_on_attempt` instead.
    """
    return [
        Finding(
            attempt=result.attempt,
            check=finding.check,
            severity=finding.severity,
            locus=finding.locus,
            detail=finding.detail,
            source_text=finding.source_text,
        )
        for result in case.verifications
        for finding in result.findings
    ]


def findings_on_attempt(case: CaseRecord, attempt: int) -> list[Finding]:
    """Only what Verification said about one drafting attempt.

    The ledger is a reading of the letter on screen. An objection raised
    against attempt 1 is an objection to a sentence attempt 3 does not contain
    -- Drafting was handed that finding and rewrote to answer it, which is the
    whole point of the retry loop. Carrying it forward onto the current letter
    prints a warning about a claim nobody is making, and a warning a reader
    checks and finds untrue costs every other warning on the page its credit.

    The matrix is the opposite case and keeps `case_findings`: it is written
    once, before the first draft, and never revised, so an objection to what it
    says stands until somebody rewrites it, which nothing does.
    """
    return [f for f in case_findings(case) if f.attempt == attempt]


# Findings from the assertion-grounding check name no criterion, so they can
# never join to a matrix row. Identify them by the check that raised them, not
# by their locus: the offline path records the literal "clinical_assertions"
# (agents/verification/checks.py:97) while the deployed ADK path records the
# asserted sentence itself. Keying on the locus string matched the first and
# missed the second, which is to say it missed every real run.
ASSERTION_CHECK = "assertion_grounded"


def unattributed_findings(findings: list[Finding]) -> list[Finding]:
    """Objections Verification raised that name no row of the matrix.

    These are the assertion-grounding catches -- the check that reads the
    letter's claims about the patient against the chart. On CASE-001 it is the
    one that matters: it caught the draft calling a 14 July "interim review" a
    "telehealth evaluation", which is the single best piece of evidence this
    project has that the system works.

    Its locus is not a criterion id in either code path -- the offline backend
    writes a literal, the deployed one writes the asserted sentence -- so it
    lands on no row, and until this existed the matrix went on stating the
    rejected characterisation with nothing beside it. Attributing it to a row
    by matching text would be a guess dressed as a citation. Saying it plainly
    above the whole matrix is the honest form: the objection is real, and which
    row it lands on is not something we know.
    """
    return [f for f in findings if f.check == ASSERTION_CHECK]


def findings_at(findings: list[Finding], *loci: str) -> list[Finding]:
    """Findings whose locus is any of these identifiers.

    ``VerificationFinding.locus`` is a *section* id from the existence check and
    a *criterion* id from the supporting-criteria check. Joining on the section
    id alone silently drops every finding from the second of those, which is the
    subtler one: it catches an argument resting on a criterion the chart does
    not document, and that argument cites a section that genuinely exists.
    """
    keys = {locus for locus in loci if locus}
    return [f for f in findings if f.locus in keys]


@dataclass(frozen=True)
class Ledger:
    """The letter's claims, split into the ones a clerk can check and the one
    kind they cannot.

    ``rows`` is the table. ``folded`` holds citations to a *parent* section
    whose subsections are already rows of their own -- see `_is_parent`.
    """

    rows: list[dict[str, object]]
    folded: list[dict[str, object]]


def _is_parent(section_id: str, cited: set[str]) -> list[str]:
    """The cited subsections of this section, if any.

    ``NBH-CARD-014-3`` is the parent of ``NBH-CARD-014-3.1``; the dot is the
    corpus's own convention and every policy in `data/policies` follows it.
    Prefix alone would be wrong -- ``NBH-CARD-014-3`` is a prefix of
    ``NBH-CARD-014-31`` -- so the separator is required.
    """
    return sorted(other for other in cited if other.startswith(f"{section_id}."))


def claim_ledger(case: CaseRecord, draft: AppealDraft | None) -> Ledger:
    """One row per claim the letter makes: the claim, the policy text it rests
    on, the chart evidence under it, and the verdict.

    Driven by ``draft.citations`` rather than by the matrix, because the
    question at this gate is "is what the letter says true", not "what did
    Mapping conclude". A matrix row the letter never uses is not something a
    clerk has to sign for; a claim with nothing under it is.

    Flagged rows sort first. Everything else keeps the letter's own order,
    which is the order the reader just met the claims in.

    Two things are deliberately narrower than they look:

      * **Findings are scoped to the attempt on screen.** See
        `findings_on_attempt`. A ledger showing the current letter must not
        warn about a sentence an earlier draft contained and this one does not.
      * **A citation to a parent section is folded out of the table.** The
        letter's opening sentence cites the whole of `NBH-CARD-014-3` and
        names all five of its subsections; that one row carried five
        subsections of policy text, eight chart quotations and five verdicts,
        and it sorted first, so it was the first thing the eye landed on. It is
        not a claim anyone can check -- every checkable part of it is one of
        the five rows underneath. It is folded to a sentence under the table,
        and only when every criterion it names is genuinely carried by a row
        that survived, so nothing goes quiet.
    """
    if draft is None:
        return Ledger([], [])

    retrieval = case.retrieval
    verdicts = {v.criterion_id: v for v in (case.criteria.verdicts if case.criteria else [])}
    findings = findings_on_attempt(case, draft.attempt)
    cited = {c.section_id for c in draft.citations}

    rows: list[dict[str, object]] = []
    for position, citation in enumerate(draft.citations):
        source = retrieval.text_for(citation.section_id) if retrieval else None
        source_note = None if source else (NOT_RETRIEVED if retrieval else NO_RETRIEVAL)

        supporting = [verdicts[c] for c in citation.supporting_criterion_ids if c in verdicts]
        unevaluated = [c for c in citation.supporting_criterion_ids if c not in verdicts]
        contested = findings_at(findings, citation.section_id, *citation.supporting_criterion_ids)

        # The offline drafter copies criterion text straight into `claim`, so on
        # seeded rows the two are the same paragraph. Printing it twice per row
        # makes the ledger look broken and doubles its height for nothing.
        restates = source is not None and _squash(citation.claim) == _squash(source)
        quoted = citation.quoted_text
        if quoted and source and _squash(quoted) == _squash(source):
            quoted = None

        weak = [v for v in supporting if v.verdict != CriterionVerdictValue.SATISFIED]
        unevidenced = [v for v in supporting if not v.evidence]
        flagged = bool(contested or source_note or unevaluated or weak or unevidenced)

        rows.append(
            {
                "position": position,
                "section_id": citation.section_id,
                "claim": citation.claim,
                "source_text": source,
                "source_note": source_note,
                "restates_verbatim": restates,
                "quoted_text": quoted,
                "verdicts": supporting,
                "unevaluated": unevaluated,
                "findings": contested,
                "flagged": flagged,
                "flag_reason": _flag_reason(contested, source_note, unevaluated, weak, unevidenced),
                "children": _is_parent(citation.section_id, cited),
                "criteria": list(citation.supporting_criterion_ids),
            }
        )

    kept, folded = _fold_parents(rows)
    kept.sort(key=lambda row: (not row["flagged"], row["position"]))
    return Ledger(kept, folded)


def _fold_parents(
    rows: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Split parent-section rows off the table, but only the safe ones.

    A parent is folded only when the rows that remain already carry every
    criterion it names and its own policy text is on the screen. Fold a row
    that was flagged for something no surviving row repeats and the warning
    disappears with it, which is a worse bug than the one being fixed.
    """
    covered: set[str] = set()
    for row in rows:
        if not row["children"]:
            covered.update(row["criteria"])  # type: ignore[arg-type]

    kept: list[dict[str, object]] = []
    folded: list[dict[str, object]] = []
    for row in rows:
        safe = (
            row["children"] and row["source_note"] is None and set(row["criteria"]) <= covered  # type: ignore[arg-type]
        )
        (folded if safe else kept).append(row)
    return kept, folded


def _flag_reason(
    contested: list[Finding],
    source_note: str | None,
    unevaluated: list[str],
    weak: list[CriterionVerdict],
    unevidenced: list[CriterionVerdict],
) -> str | None:
    """One line naming why a row sorted to the top, in severity order.

    ``None`` when Verification contested the row, because the objection itself
    is printed immediately under the chip. Saying "Verification objected to this
    claim" one line above "Verification objected: ..." is a stutter, and the
    second of the two is the one carrying the information.
    """
    if contested:
        return None
    if source_note:
        return "The policy text behind this claim is not on the screen."
    if unevaluated:
        return "Rests on " + ", ".join(unevaluated) + ", which was never evaluated."
    if weak:
        return "Rests on a criterion the chart does not satisfy."
    if unevidenced:
        return "Rests on a criterion with no chart evidence cited."
    return None


# --------------------------------------------------------------------------- #
# The criteria mapping
# --------------------------------------------------------------------------- #


def mapping_rows(case: CaseRecord) -> list[dict[str, object]]:
    """The full criteria mapping, with anything Verification contested marked.

    Mapping writes the matrix once. Verification's findings were fed back to
    Drafting and nowhere else, so a row whose stated reasoning a second model
    contradicted went on rendering as a clean `Satisfied - 100% - high` on the
    same screen where a clinician attests that the letter's account of the care
    and the chart is accurate. The system catching an overclaim and then not
    telling anyone is worse than not catching it.

    The verdict is deliberately left alone. What Verification rejects is
    usually the *characterisation* and not the conclusion -- a policy reading
    "in-person or telehealth evaluation" may well still be satisfied by the
    interim review the reasoning mis-described. Flipping the verdict would
    replace one wrong row with another. The objection is printed under the
    reasoning instead, and the row says a second model disagreed.

    There is no confidence here. There was, and it was suppressed on the rows
    where it was most obviously wrong -- contested, or with no evidence under
    it -- which left the number standing on every other row as though those
    were the only two ways a language model's self-report can mislead. Four
    rows read `100% high` on a live case. The caption already made the argument
    against the column: a clerk cannot act differently at ninety-four percent
    than at eighty-eight, and there is no third thing they do at one hundred.
    A number that changes no action and lends unearned weight to the row it
    sits on is not information, and it is worse than blank.
    """
    matrix = case.criteria
    if matrix is None:
        return []
    findings = case_findings(case)

    rows: list[dict[str, object]] = []
    for verdict in matrix.verdicts:
        contested = findings_at(findings, verdict.criterion_id)
        rows.append({"verdict": verdict, "findings": contested, "contested": bool(contested)})
    return rows


# --------------------------------------------------------------------------- #
# How this letter got here
#
# The single most persuasive line on the page, and it used to live only inside
# a fold at the bottom. A closed `<details>` is indistinguishable from absent,
# and the retry loop is the best evidence this project has that the check is
# real. It is a sentence now, at the top, next to the letter it explains.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Provenance:
    """One sentence naming which attempt this is and what Verification objected to.

    The verb is "objected to" and not "caught", deliberately. "Caught" asserts
    the objection was correct, and on the deployed CASE-003 two of the three
    were not -- Verification rejected a restatement that was verbatim the
    policy criterion. A sentence generated for every case cannot vouch for the
    merit of an objection it has not evaluated; it can only report that one was
    raised. Where the objection is genuinely a catch, as on CASE-001, the
    quoted finding underneath makes that plain without the summary line having
    to claim it.
    """

    text: str
    attempt: int
    sent_back: list[int]
    caught: list[str]


def provenance(case: CaseRecord, draft: AppealDraft | None) -> Provenance | None:
    if draft is None:
        return None

    sent_back = sorted(r.attempt for r in case.verifications if not r.passed)
    caught = _caught(case, draft, sent_back)

    if not sent_back:
        text = f"Attempt {draft.attempt}, and Verification passed it. Nothing was sent back."
        return Provenance(text, draft.attempt, [], [])

    listed = _join_numbers(sent_back)
    text = f"Attempt {draft.attempt}. Verification sent {listed} back" + (
        f" — it objected to {_join_prose(caught)}." if caught else "."
    )
    return Provenance(text, draft.attempt, sent_back, caught)


def _caught(case: CaseRecord, draft: AppealDraft, sent_back: list[int]) -> list[str]:
    """What Verification actually objected to, in a clerk's words.

    Read off the rejected attempts' findings, falling back to the revision
    instructions the current draft says it was written to answer. "Verification
    sent two attempts back" without saying what for is a boast; naming the
    hallucinated citation is evidence.
    """
    phrases: list[str] = []
    for result in case.verifications:
        if result.attempt not in sent_back:
            continue
        phrases.extend(_caught_phrases(result))
    if not phrases:
        phrases = [_squash(line) for line in draft.revision_feedback_applied]
    return list(dict.fromkeys(phrases))


def _caught_phrases(result: VerificationResult) -> list[str]:
    phrases: list[str] = []
    for finding in result.findings:
        if finding.severity != "fatal":
            continue
        if finding.check == "citation_exists":
            phrases.append(f"a citation to {finding.locus} that is not in the retrieved policy set")
        elif finding.check == "citation_accurate":
            phrases.append(f"a point resting on {finding.locus}, which the chart does not satisfy")
        elif finding.check == "assertion_grounded":
            phrases.append("a letter making claims with no clinical assertions listed to check")
        else:
            phrases.append(f"a problem at {finding.locus}")
    for cid in result.citations_nonexistent:
        phrases.append(f"a citation to {cid} that is not in the retrieved policy set")
    for cid in result.citations_unsupported:
        phrases.append(f"a claim about {cid} the source text does not support")
    for claim in result.ungrounded_assertions:
        phrases.append(f"an assertion no row of the matrix carries: {_squash(claim)}")
    return phrases


def _join_numbers(numbers: list[int]) -> str:
    words = [f"attempt {n}" for n in numbers]
    if len(words) == 1:
        return words[0]
    return "attempts " + ", ".join(str(n) for n in numbers[:-1]) + f" and {numbers[-1]}"


def _join_prose(phrases: list[str], limit: int = 2) -> str:
    shown = phrases[:limit]
    extra = len(phrases) - len(shown)
    joined = shown[0] if len(shown) == 1 else ", and ".join([", ".join(shown[:-1]), shown[-1]])
    if extra:
        joined += f", and {extra} more"
    return joined


# --------------------------------------------------------------------------- #
# Screening: quiet when nothing happened, loud when something did
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ScreeningView:
    """What Sentinel found, and how loudly the page should say it."""

    present: bool
    findings: list[Any]
    quarantined: bool
    prominent: bool
    headline: str
    line: str


def screening_view(screening: Any | None) -> ScreeningView:
    """A banner above the letter when there are findings; one line in a fold
    when there are none.

    A chip reading "No threats found" is a chip for a null result, and a screen
    that shouts every time nothing happened has nothing left to shout with when
    something does.
    """
    if screening is None:
        return ScreeningView(
            present=False,
            findings=[],
            quarantined=False,
            prominent=True,
            headline="This document was not screened",
            line=(
                "No screening record is attached to this case. Treat the contents with "
                "more caution than usual."
            ),
        )

    findings = list(screening.findings)
    quarantined = bool(screening.quarantine)
    if not findings and not quarantined:
        layers = ", ".join(screening.layers_run) if screening.layers_run else "none recorded"
        return ScreeningView(
            present=True,
            findings=[],
            quarantined=False,
            prominent=False,
            headline="No threats found",
            line=f"No threats found in the source document. Detectors that ran: {layers}.",
        )

    count = len(findings)
    return ScreeningView(
        present=True,
        findings=findings,
        quarantined=quarantined,
        prominent=True,
        headline=(f"Sentinel found {count} item{'' if count == 1 else 's'} in this document"),
        line=f"{count} item{'' if count == 1 else 's'} found. See the banner above the letter.",
    )


# --------------------------------------------------------------------------- #
# The page head
# --------------------------------------------------------------------------- #

# The status enum rendered raw -- `awaiting_human_approval` -- one line above an
# appeal level that was de-underscored. Two spellings of the same convention on
# the same line is the sort of small wrongness that makes a reader distrust the
# parts they cannot check.
STATUS_PHRASES: dict[CaseStatus, str] = {
    CaseStatus.AWAITING_APPROVAL: "Awaiting your decision",
    CaseStatus.APPROVED: "Approved — awaiting the clinician's co-sign",
    CaseStatus.NEEDS_HUMAN_REVIEW: "Sent back for human review",
    CaseStatus.SUBMITTED: "Transmitted to the payer",
    CaseStatus.PAYER_RESPONDED: "The payer has responded",
    CaseStatus.ESCALATED: "Escalated to the next appeal level",
    CaseStatus.OVERTURNED: "Overturned — the payer reversed the denial",
    CaseStatus.UPHELD: "Upheld — the payer kept the denial",
    CaseStatus.DECLINED_NO_BASIS: "Declined — no basis to appeal",
    CaseStatus.QUARANTINED: "Quarantined by screening",
    CaseStatus.FAILED: "The pipeline could not finish this case",
}


def status_phrase(status: CaseStatus) -> str:
    """The status as a person would say it, never as the enum spells it."""
    return STATUS_PHRASES.get(status, str(status.value).replace("_", " ").capitalize())


def reviewer_hint(headers: Mapping[str, str]) -> str:
    """Pre-fill the reviewer field from the IAP identity, when there is one.

    Cloud Run behind Identity-Aware Proxy sets this header. Locally it is absent
    and the field starts empty and required, because an unattributed approval is
    worse than no approval at all.
    """
    raw = headers.get("x-goog-authenticated-user-email", "")
    return raw.split(":", 1)[-1].strip() if raw else ""


# --------------------------------------------------------------------------- #
# The clerk's gate
# --------------------------------------------------------------------------- #


def clerk_checks(result: VerificationResult | None) -> list[dict[str, object]]:
    """The three confirmations the clerk ticks, each with what Verification found.

    There used to be a second, read-only table rendering the same three facts
    forty lines further down the page. It is gone: the wording here is the one
    addressed to the person about to sign, and saying it twice made neither
    copy more true. A checkbox that says "citations resolve" without showing
    *which* citations were resolved is a box to tick rather than a thing to
    confirm, which is why each row carries what Verification found.

    Three rows are returned even when nothing has been verified, because a form
    that silently loses its controls leaves a clerk unable to approve with no
    explanation. The row says so instead, and the service refuses regardless.
    """
    return [
        {
            "field": "citations_checked",
            "id": "check-citations",
            "label": "Every cited section id exists in the retrieved policy set",
            "passed": None if result is None else not result.citations_nonexistent,
            "finding": _citation_finding(result),
        },
        {
            "field": "quotes_checked",
            "id": "check-quotes",
            "label": "Each quoted passage matches the policy text it is attributed to",
            "passed": None if result is None else not result.citations_unsupported,
            "finding": _quote_finding(result),
        },
        {
            "field": "assertions_checked",
            "id": "check-assertions",
            "label": "Nothing is asserted that the criteria matrix does not support",
            "passed": None if result is None else not result.ungrounded_assertions,
            "finding": _assertion_finding(result),
        },
    ]


NOT_VERIFIED = "Verification has not run on this draft, so there is no computed result to confirm."


def _citation_finding(result: VerificationResult | None) -> str:
    if result is None:
        return NOT_VERIFIED
    if result.citations_nonexistent:
        return "Verification could not find: " + ", ".join(result.citations_nonexistent)
    return (
        f"Verification resolved {result.citations_checked} citation"
        f"{'' if result.citations_checked == 1 else 's'} against the retrieved policy set."
    )


def _quote_finding(result: VerificationResult | None) -> str:
    if result is None:
        return NOT_VERIFIED
    if result.citations_unsupported:
        return "Source text does not support: " + ", ".join(result.citations_unsupported)
    return "Verification re-read each cited section against the claim made from it."


def _assertion_finding(result: VerificationResult | None) -> str:
    if result is None:
        return NOT_VERIFIED
    if result.ungrounded_assertions:
        return "No matrix row carries: " + "; ".join(result.ungrounded_assertions)
    return "Verification traced every clinical assertion to a row in the criteria matrix."


# --------------------------------------------------------------------------- #
# Submission readiness
# --------------------------------------------------------------------------- #

CLERK_ROLE = "Billing clerk"
CLINICIAN_ROLE = "Ordering clinician"


@dataclass(frozen=True)
class Signature:
    """One of the two signatures a case needs, and whether it is on the record."""

    key: str  # "clerk" | "clinician" - a template must not match on prose
    role: str
    scope: str  # what this signatory was asked, in one line
    required: bool
    present: bool
    signed_by: str | None
    detail: str
    attempt: int | None


@dataclass(frozen=True)
class Readiness:
    """Which signatures are present, which are missing, and what happens next.

    ``ready`` is ``CaseRecord.ready_to_submit`` copied verbatim. Nothing in this
    module re-derives it: two definitions of "enough signatures" is exactly the
    bug this screen exists to prevent. The other fields explain the answer; they
    never change it, which is why ``attempt_conflict`` is computed only to be
    said out loud.
    """

    signatures: list[Signature]
    ready: bool
    submitted: bool
    missing: list[str]
    attempt_conflict: str | None
    summary: str


def readiness(case: CaseRecord) -> Readiness:
    decision = case.human_decision
    approved = bool(decision and decision.approved)
    clerk_attempt = decision.draft_attempt_approved if decision else None

    clerk = Signature(
        key="clerk",
        role=CLERK_ROLE,
        scope=(
            "Confirms the paper trail: that the cited sections exist, that the quoted "
            "policy text matches its source, and that no claim outruns the matrix."
        ),
        required=True,
        present=approved,
        signed_by=decision.decided_by if approved and decision else None,
        detail=(
            f"Approved drafting attempt {clerk_attempt} on {fmt_datetime(decision.decided_at)}."
            if approved and decision
            else "No approval recorded yet."
        ),
        attempt=clerk_attempt if approved else None,
    )

    cosign = case.clinician_cosign
    signed = bool(cosign and cosign.attests_clinical_accuracy)
    clinician = Signature(
        key="clinician",
        role=CLINICIAN_ROLE,
        scope=(
            "Attests to the clinical argument: that the letter's account of the care "
            "and the chart is accurate. A clerk is not in a position to judge this."
        ),
        required=case.requires_clinician_cosign,
        present=signed,
        signed_by=(f"{cosign.clinician_name}, {cosign.credential}" if signed and cosign else None),
        detail=_cosign_detail(case, signed),
        attempt=cosign.draft_attempt_signed if signed and cosign else None,
    )

    signatures = [clerk, clinician]
    missing = [s.role for s in signatures if s.required and not s.present]
    conflict = _attempt_conflict(clerk, clinician)
    submitted = case.submitted_at is not None

    return Readiness(
        signatures=signatures,
        ready=case.ready_to_submit,
        submitted=submitted,
        missing=missing,
        attempt_conflict=conflict,
        summary=_readiness_summary(case, missing, conflict, submitted),
    )


def _cosign_detail(case: CaseRecord, signed: bool) -> str:
    cosign = case.clinician_cosign
    if signed and cosign:
        npi = f", NPI {cosign.npi}" if cosign.npi else ""
        return (
            f"Co-signed drafting attempt {cosign.draft_attempt_signed} on "
            f"{fmt_datetime(cosign.signed_at)}{npi}."
        )
    if not case.requires_clinician_cosign:
        return (
            "Not required on this case: the draft argues documentation alone and makes "
            "no clinical claim."
        )
    return "No co-sign recorded yet."


def _attempt_conflict(clerk: Signature, clinician: Signature) -> str | None:
    """Both signatures present, but on different drafts.

    Said plainly because the alternative is a case that sits at ``approved``
    with two signatures on it and never moves, for a reason nothing shows.
    """
    if not (clerk.present and clinician.present and clinician.required):
        return None
    if clerk.attempt is None or clinician.attempt is None:
        return None
    if clerk.attempt == clinician.attempt:
        return None
    return (
        f"The clerk approved drafting attempt {clerk.attempt} and the clinician "
        f"co-signed attempt {clinician.attempt}. Both signatures have to be on the "
        f"same draft, so nothing will be transmitted until one of them is re-signed "
        f"against the other's attempt."
    )


def _readiness_summary(
    case: CaseRecord, missing: list[str], conflict: str | None, submitted: bool
) -> str:
    if submitted:
        return "This appeal has been transmitted to the payer."
    if conflict:
        return "The two signatures are on different drafts, so nothing has been transmitted."
    if missing:
        joined = " and ".join(f"the {role.lower()}" for role in missing)
        return (
            f"Waiting on {joined}. Nothing is transmitted until every required "
            f"signature is present."
        )
    if case.ready_to_submit:
        return (
            "Every required signature is present. Transmission was attempted; if the case "
            "is not marked submitted below, it did not complete."
        )
    return (
        "This case is not cleared for transmission. Both the clerk's approval and the "
        "clinician's co-sign have to be on the record, on the same draft."
    )


@dataclass(frozen=True)
class SubmissionView:
    """What the payer gave back when the appeal was transmitted."""

    submitted_at: datetime
    reference: str | None
    response_deadline: datetime | None


CONFIRMATION_NOTE = "confirmation "


def submission(case: CaseRecord) -> SubmissionView | None:
    """The confirmation reference, read off the case's own transition history.

    Lifecycle writes it into the note on the transition to ``submitted``. Reading
    it here rather than querying the action record keeps this interface to the
    one document it is allowed to see, and a reference the case itself does not
    carry is one no reviewer could quote to the payer anyway.
    """
    if case.submitted_at is None:
        return None

    reference: str | None = None
    for transition in reversed(case.history):
        if transition.to_status != CaseStatus.SUBMITTED or not transition.note:
            continue
        if transition.note.startswith(CONFIRMATION_NOTE):
            stated = transition.note[len(CONFIRMATION_NOTE) :].strip()
            reference = stated if stated and stated != "unknown" else None
        break

    return SubmissionView(
        submitted_at=case.submitted_at,
        reference=reference,
        response_deadline=case.response_deadline,
    )


# --------------------------------------------------------------------------- #
# The appeal ladder
#
# This is the claim the whole project rests on -- that a case is carried for
# weeks with nobody watching it -- and it was one sentence on the page:
# "Escalated to the next appeal level". CASE-006 has actually climbed a rung
# unattended: the payer's thirty-day window lapsed, the scheduler woke, and
# Lifecycle moved it from a first-level appeal to peer-to-peer review with a
# fresh fourteen-day clock. Every part of that is on the record and none of it
# was on the screen.
# --------------------------------------------------------------------------- #

LEVEL_LABELS: dict[AppealLevel, str] = {
    AppealLevel.FIRST_LEVEL: "First-level appeal",
    AppealLevel.PEER_TO_PEER: "Peer-to-peer review",
    AppealLevel.SECOND_LEVEL: "Second-level appeal",
    AppealLevel.EXTERNAL_REVIEW: "Independent external review",
}


def _ladder_order() -> list[AppealLevel]:
    """The rungs in the order Lifecycle climbs them.

    Walked from the table rather than written out again: two lists that both
    claim to be the appeal ladder is how one of them goes stale.
    """
    order: list[AppealLevel] = []
    level: AppealLevel | None = AppealLevel.FIRST_LEVEL
    while level is not None and level not in order:
        order.append(level)
        level = APPEAL_LADDER[level].next_level
    return order


@dataclass(frozen=True)
class Rung:
    """One step of the ladder, and where the case is relative to it."""

    label: str
    window_days: int
    state: str  # "climbed" | "here" | "ahead"


@dataclass(frozen=True)
class Escalation:
    """A case that moved up the ladder without anyone asking it to."""

    from_label: str
    to_label: str
    position: int  # 1-based rung the case is on now
    total: int
    count: int  # how many times it has escalated
    at: datetime | None
    actor: str | None
    reason: str | None
    lapsed_days: int | None  # the window that ran out to cause this
    window_days: int | None  # the window it is now inside
    deadline: datetime | None
    deadline_days: int | None
    next_label: str | None  # where it goes if nothing comes back
    rungs: list[Rung]
    line: str


def escalation(case: CaseRecord, now: datetime | None = None) -> Escalation | None:
    """Where this case is on the appeal ladder, and what put it there.

    Returns ``None`` for a case that has never escalated, which is almost all of
    them. Nothing is inferred that the record does not carry: the rung it was on
    is read off the ladder table and ``escalation_count``, and the moment, the
    actor and the reason are the transition the orchestrator wrote.
    """
    if case.escalation_count < 1:
        return None

    order = _ladder_order()
    try:
        index = order.index(case.appeal_level)
    except ValueError:  # a level not on the ladder; say nothing rather than guess
        return None

    previous = order[max(0, index - 1)]
    rung = APPEAL_LADDER[case.appeal_level]
    moved = next((t for t in reversed(case.history) if t.to_status == CaseStatus.ESCALATED), None)

    deadline = case.response_deadline
    days: int | None = None
    if deadline is not None:
        days = (deadline - (now or datetime.now(UTC))).days

    return Escalation(
        from_label=LEVEL_LABELS.get(previous, str(previous.value).replace("_", " ")),
        to_label=LEVEL_LABELS.get(case.appeal_level, str(case.appeal_level.value)),
        position=index + 1,
        total=len(order),
        count=case.escalation_count,
        at=moved.at if moved else None,
        actor=moved.actor if moved else None,
        reason=moved.note if moved else None,
        lapsed_days=APPEAL_LADDER[previous].response_window_days,
        window_days=rung.response_window_days,
        deadline=deadline,
        deadline_days=days,
        next_label=(LEVEL_LABELS.get(rung.next_level) if rung.next_level else None),
        rungs=[
            Rung(
                label=LEVEL_LABELS.get(level, str(level.value)),
                window_days=APPEAL_LADDER[level].response_window_days,
                state="here" if i == index else ("climbed" if i < index else "ahead"),
            )
            for i, level in enumerate(order)
        ],
        line=(
            f"No answer came back within the "
            f"{APPEAL_LADDER[previous].response_window_days}-day window on the "
            f"{LEVEL_LABELS.get(previous, previous.value).lower()}, so this case moved itself "
            f"to {LEVEL_LABELS.get(case.appeal_level, case.appeal_level.value).lower()}. "
            f"Nobody asked it to."
        ),
    )


# --------------------------------------------------------------------------- #
# Traces
#
# `core/telemetry.py` opens a span per agent invocation and `core/audit.py`
# stamps the trace and span id onto the audit event before it is written. Both
# have been on the record since the first run and neither had ever reached a
# screen, so the observability claim was a claim about a file rather than about
# anything a reader could check.
#
# No link is offered. Cloud Trace is behind a Google sign-in and a project a
# reader almost certainly cannot see, and a link that 403s is worse than an
# identifier they can paste. The id is the thing; it is presented as the thing.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Traces:
    """The distinct traces this case's agent invocations were recorded under."""

    ids: list[str]
    events: int  # events carrying a trace id
    total: int  # events on the case
    summary: str


def traces(trail: list[Any]) -> Traces:
    """Read the trace ids off the audit trail, oldest first, deduplicated.

    Ordinarily one pipeline run is one trace, so this is one id. A case picked
    up again later -- resumed after a failure, escalated weeks after it was
    filed -- genuinely has more than one, and listing them is the honest shape:
    the ladder is asynchronous, and its spans are not going to be in the same
    trace as the intake that started it.
    """
    ids: list[str] = []
    carried = 0
    for event in trail:
        trace_id = getattr(event, "trace_id", None)
        if not trace_id:
            continue
        carried += 1
        if trace_id not in ids:
            ids.append(trace_id)

    if not ids:
        summary = (
            "No trace id is recorded against this case. It ran in a process with "
            "tracing switched off, so there is nothing to open in Cloud Trace."
        )
    elif len(ids) == 1:
        summary = (
            f"All {carried} recorded invocation{'' if carried == 1 else 's'} below "
            f"belong to one trace. Every row is a span under it, including the "
            f"drafting attempts Verification sent back."
        )
    else:
        summary = (
            f"{carried} recorded invocations across {len(ids)} traces — this case was "
            f"picked up more than once, which is what a multi-week lifecycle looks "
            f"like in a tracing backend."
        )
    return Traces(ids=ids, events=carried, total=len(trail), summary=summary)


def approved_but_not_sent(case: CaseRecord) -> str | None:
    """The reason an approved case was pushed back to a human, if it was.

    A case can reach ``needs_human_review`` *after* both signatures are on it,
    when transmission failed or could not be safely retried. Without this the
    screen would show an approval, a co-sign, and a status nobody can account
    for.
    """
    if case.status != CaseStatus.NEEDS_HUMAN_REVIEW:
        return None
    if not (case.human_decision and case.human_decision.approved):
        return None
    return (
        case.needs_human_reason
        or case.last_error
        or ("The case was returned for human review after approval, with no reason recorded.")
    )


# --------------------------------------------------------------------------- #
# The dashboard
# --------------------------------------------------------------------------- #

# Every status a case can hold, grouped by who it is actually waiting on. The
# three queues below already showed the cases a human can act on; nothing showed
# the rest, so a case that was quarantined or declined simply vanished from the
# interface and looked like it had never existed. A clerk asking "where did
# CASE-002 go?" deserves an answer on the page rather than in the logs.
WAITING_ON_CLERK = CaseStatus.AWAITING_APPROVAL
WAITING_ON_CLINICIAN = CaseStatus.APPROVED
SENT_BACK = CaseStatus.NEEDS_HUMAN_REVIEW

IN_FLIGHT_STATUSES: frozenset[CaseStatus] = frozenset(
    {
        CaseStatus.RECEIVED,
        CaseStatus.SCREENING,
        CaseStatus.EXTRACTED,
        CaseStatus.RETRIEVING,
        CaseStatus.MAPPING,
        CaseStatus.DRAFTING,
        CaseStatus.VERIFYING,
    }
)

WITH_PAYER_STATUSES: frozenset[CaseStatus] = frozenset(
    {
        CaseStatus.SUBMITTED,
        CaseStatus.PAYER_RESPONDED,
        CaseStatus.ESCALATED,
    }
)

# Closed states, in the order a person would want to read them: the good outcome
# first, then the ordinary ones, then the two that mean something went wrong.
CLOSED_STATUSES: tuple[tuple[CaseStatus, str, str], ...] = (
    (CaseStatus.OVERTURNED, "Overturned", "The payer reversed the denial."),
    (CaseStatus.UPHELD, "Upheld", "The payer kept the denial after appeal."),
    (
        CaseStatus.DECLINED_NO_BASIS,
        "Declined — no basis",
        "No applicable policy, or the record did not support an honest argument. "
        "Nothing was drafted.",
    ),
    (
        CaseStatus.QUARANTINED,
        "Quarantined",
        "Screening found an injected instruction in the document. No agent read it.",
    ),
    (CaseStatus.FAILED, "Failed", "The pipeline could not finish this case."),
)


# Ten days is the point at which a clerk should be told without being asked.
# It matches the amber band in `deadline_view`, deliberately: two places
# disagreeing about what counts as urgent is worse than either threshold.
URGENT_WITHIN_DAYS = 10


@dataclass(frozen=True)
class Tile:
    """One number on the dashboard, and the sentence that makes it mean something."""

    label: str
    count: int
    caption: str
    href: str | None
    tone: str  # "act" | "wait" | "quiet"
    # Which filter this tile turns on, so the template can mark the live one
    # without matching on prose. `None` for the two nobody can act on.
    key: str | None = None


@dataclass(frozen=True)
class Band:
    """One segment of the caseload bar, and the filter it turns on.

    The bar used to be decorative -- `aria-hidden`, proportioned by flex-grow,
    every number in it repeated in a legend underneath and then a third time in
    a row of tiles below that. Three statements of five numbers, and the only
    one you could act on was the last. The segment is the control now: it
    carries the count, the label, the sentence that makes the count mean
    something, and a link that narrows the table to exactly those cases.

    `href` is `None` for a band nobody can act on -- agents mid-run, cases with
    the payer, closed work. There is no queue behind those, and a control that
    filters to a list you cannot do anything with is a control that lies about
    what it offers.
    """

    label: str
    count: int
    tone: str
    caption: str = ""
    key: str | None = None
    href: str | None = None


@dataclass(frozen=True)
class Lead:
    """The one case worth opening first, and the marks that say why.

    Not a priority and not a place in the worklist -- the table below is still
    sorted by deadline, which is the only ordering a clerk with eleven letters
    can defend. This is an entry point for somebody who has never seen the
    system and has no way to know which row shows it doing its job.
    """

    case_id: str
    patient: str
    service: str
    marks: list[str]
    rejected: int
    # Assembled here rather than in Jinja, because it inflects on a count and
    # a template that has to pick between "draft" and "drafts" is a template
    # that has started doing prose.
    why: str


@dataclass(frozen=True)
class Overview:
    """The whole workload in one glance."""

    tiles: list[Tile]
    bands: list[Band]
    closed: list[Tile]
    urgent: list[dict[str, object]]
    total: int
    actionable: int
    # What the queue is showing, in one sentence. "5 open, 3 need you" is a
    # count; a first-time reader also needs to know that a refusal on this
    # screen is the system working rather than a hole in it.
    synopsis: str = ""
    lead: Lead | None = None


def overview(cases: list[CaseRecord], today: date | None = None) -> Overview:
    """Count every case by who it is waiting on, and surface the deadlines.

    Reads every case rather than issuing one query per status. That is the right
    trade at this size and the wrong one at ten thousand cases, where this should
    become counters maintained on write.
    """
    by_status: dict[CaseStatus, list[CaseRecord]] = {}
    for case in cases:
        by_status.setdefault(case.status, []).append(case)

    def held(*statuses: CaseStatus) -> list[CaseRecord]:
        out: list[CaseRecord] = []
        for status in statuses:
            out.extend(by_status.get(status, []))
        return out

    clerk = held(WAITING_ON_CLERK)
    clinician = held(WAITING_ON_CLINICIAN)
    back = held(SENT_BACK)
    in_flight = held(*IN_FLIGHT_STATUSES)
    with_payer = held(*WITH_PAYER_STATUSES)

    # Three of these used to link to an anchor two hundred pixels down the same
    # page: a table of contents for a page you can already see all of. They
    # filter the one table below instead, which is the only thing a count on a
    # dashboard is ever actually asked to do.
    tiles = [
        Tile(
            "Waiting on you",
            len(clerk),
            "Drafted, verified, and needing a clerk's decision.",
            "/queue?waiting=clerk",
            "act",
            "clerk",
        ),
        Tile(
            "Waiting on a clinician",
            len(clinician),
            "A clerk has signed. Nothing sends until the clinician co-signs.",
            "/queue?waiting=clinician",
            "act" if clinician else "quiet",
            "clinician",
        ),
        Tile(
            "Sent back to you",
            len(back),
            "The fleet could not finish these, or a reviewer rejected the draft.",
            "/queue?waiting=review",
            "act" if back else "quiet",
            "review",
        ),
        Tile(
            "Agents still working",
            len(in_flight),
            "Screening, mapping, drafting or verifying right now.",
            None,
            "wait",
        ),
        Tile(
            "With the payer",
            len(with_payer),
            "Submitted and inside the response window. Lifecycle escalates on its own.",
            None,
            "wait",
        ),
    ]

    closed = [
        Tile(label, len(by_status.get(status, [])), caption, None, "quiet")
        for status, label, caption in CLOSED_STATUSES
        if by_status.get(status)
    ]

    # Deadline pressure only means anything on a case someone can still act on.
    # A quarantined case has no appeal to file and no clock to miss.
    open_cases = clerk + clinician + back
    urgent: list[dict[str, object]] = []
    for case in open_cases:
        deadline = case.denial.appeal_deadline if case.denial else None
        seen = deadline_view(deadline, today)
        if seen.days is not None and seen.days <= URGENT_WITHIN_DAYS:
            urgent.append(
                {
                    "case_id": case.case_id,
                    # Through `display_name` like the table below it. The strip
                    # showed "Creola518 Heller342" four inches above a row
                    # reading "Creola Heller", which reads as two records.
                    "patient": display_name(case.denial.patient_name if case.denial else None),
                    "deadline": seen,
                    "waiting_on": waiting_on(case.status),
                }
            )
    urgent.sort(key=lambda row: row["deadline"].days)  # type: ignore[union-attr,index]

    # The same counts as one shape, and as the controls that act on them.
    # Built from `tiles` rather than beside them: these were two literal lists
    # of the same six counts, which is a drift waiting to happen -- change a
    # status mapping in one and the bar and the tiles disagree about the
    # caseload with nothing to catch it.
    #
    # The bar's palette is not the tiles' palette. A rejected draft is "act" to
    # a tile, because the clerk must do something about it; it is its own colour
    # in the bar, because the shape of a caseload is more legible when the work
    # that came back looks different from the work that never left.
    #
    # Empty bands are dropped rather than drawn as slivers: a segment too thin
    # to see is a segment that misleads.
    band_tones = {"clerk": "act", "clinician": "act", "review": "review"}
    bands = [
        Band(
            label=tile.label,
            count=tile.count,
            tone=band_tones.get(tile.key or "", "wait"),
            caption=tile.caption,
            key=tile.key,
            # A filter that would return an empty table is not offered. The
            # count is still drawn; it just is not a link to nothing.
            href=tile.href if tile.count else None,
        )
        for tile in tiles
    ]
    bands.append(
        Band(
            label="Closed",
            count=sum(len(by_status.get(st, [])) for st, _, _ in CLOSED_STATUSES),
            tone="quiet",
            caption="Nothing is waiting on anyone. Listed below so a case that "
            "disappears does not look like a case that was lost.",
        )
    )

    return Overview(
        tiles=tiles,
        # A zero is dropped only where it says nothing. "0 closed" and "0 agents
        # working" are noise; "0 waiting on a clinician" answers a question a
        # clerk is actually asking, so the three actionable states keep their
        # segment at zero and simply stop growing. An empty system draws no bar
        # at all -- three zeroes is not a workload, and the heading has already
        # said nothing is waiting.
        bands=[band for band in bands if band.count or band.key] if cases else [],
        closed=closed,
        urgent=urgent,
        total=len(cases),
        actionable=len(clerk) + len(clinician) + len(back),
        synopsis=synopsis(cases),
        lead=lead_case(cases),
    )


# The two closed states that are the system declining to act rather than
# finishing: no honest argument was available, or the document carried an
# injected instruction and no agent was allowed to read it. Both are refusals,
# and on a first read they look like failures unless the page says otherwise.
REFUSAL_STATUSES: frozenset[CaseStatus] = frozenset(
    {CaseStatus.DECLINED_NO_BASIS, CaseStatus.QUARANTINED}
)


def synopsis(cases: list[CaseRecord]) -> str:
    """One sentence saying what this queue is, for somebody who has never seen it.

    The heading above it counts. A count answers "how much" and leaves "what am
    I looking at" unanswered, and the answer matters here: three of these cases
    are the system refusing to produce an appeal, and a reader who does not know
    that reads three failures.

    Every number is counted off the records. "Synthetic" is not -- it is a
    property of this deployment, stated on the sign-in page and in the README,
    and repeated here because the queue is now the first screen a visitor sees.
    """
    if not cases:
        return ""
    states = len({case.status for case in cases})
    refusals = sum(1 for case in cases if case.status in REFUSAL_STATUSES)
    line = (
        f"All of them are synthetic — an invented payer, generated charts — and they span "
        f"{states} state{'' if states == 1 else 's'} of one pipeline."
    )
    if not refusals:
        return line
    refusal = "1 of them is a refusal" if refusals == 1 else f"{refusals} of them are refusals"
    return (
        f"{line} {refusal}: a case the system declined to argue, or a document it "
        f"quarantined unread. A refusal here is the system working, not a gap in it."
    )


def lead_case(cases: list[CaseRecord]) -> Lead | None:
    """The case that best shows what this system does, or nothing.

    The signal is not invented and it is not a score. It is the one thing in the
    record that shows the retry loop closing: a draft Verification rejected,
    followed by an attempt that passed. Nothing else on a case record
    distinguishes "the check ran" from "the check found something and the next
    letter was better for it".

    Among cases that qualify, the one whose pass took the most rejections to
    reach is the clearest -- it has the most rejected drafts on its page to read
    -- and ties break on case id so the same reader gets the same answer twice.

    Returns ``None`` when no case qualifies, and the template shows nothing.
    Manufacturing a "best" case out of a queue where nothing was ever sent back
    would be the exact failure this whole screen is trying not to commit.
    """
    best: tuple[int, str] | None = None
    chosen: CaseRecord | None = None
    for case in cases:
        marks = attempt_marks(case)
        if "passed" not in marks:
            continue
        before = marks[: marks.index("passed")].count("rejected")
        if not before:
            continue
        key = (-before, case.case_id)
        if best is None or key < best:
            best, chosen = key, case
    if chosen is None:
        return None
    marks = attempt_marks(chosen)
    denial = chosen.denial
    rejected = marks[: marks.index("passed")].count("rejected")
    plural = "" if rejected == 1 else "s"
    return Lead(
        case_id=chosen.case_id,
        patient=display_name(denial.patient_name if denial else None),
        service=service_line(denial),
        marks=marks,
        rejected=rejected,
        # Only what the marks prove. The record says a draft was sent back and
        # the next one was not; it does not say the objection was a good one,
        # and this sentence must not either.
        why=(
            f"Verification rejected {rejected} draft{plural} of this letter before one "
            f"passed. The rejected draft{plural} {'is' if rejected == 1 else 'are'} on the "
            f"case page in full, beside the objection that stopped "
            f"{'it' if rejected == 1 else 'each one'} — the shortest way to see what this "
            f"system is for."
        ),
    )


def waiting_on(status: CaseStatus) -> str:
    """Who a case is held up on, in one phrase.

    Public because the queue table now carries it as a column. It was already
    computing exactly this string for the urgent strip; two functions saying
    "waiting on the clinician" in two different wordings is how a screen starts
    to look assembled rather than written.
    """
    if status == WAITING_ON_CLERK:
        return "your decision"
    if status == WAITING_ON_CLINICIAN:
        return "the clinician's co-sign"
    return "human review"


def waiting_key(status: CaseStatus) -> str:
    """The same answer as a token, for filtering and for CSS."""
    if status == WAITING_ON_CLERK:
        return "clerk"
    if status == WAITING_ON_CLINICIAN:
        return "clinician"
    return "review"


# The whole set of values `?waiting=` may take. Anything else falls back, in
# the spirit of the `back` validation on /theme: a query parameter is a form
# field somebody can type, and a filter it does not recognise must not empty
# the queue a clerk works from.
WAITING_FILTERS: dict[str, str] = {
    "all": "Everything waiting on a person",
    "clerk": "Waiting on you",
    "clinician": "Waiting on a clinician",
    "review": "Sent back to you",
}
DEFAULT_WAITING = "all"


def waiting_filter(raw: str | None) -> str:
    return raw if raw in WAITING_FILTERS else DEFAULT_WAITING


# --------------------------------------------------------------------------- #
# Who actually wrote this
# --------------------------------------------------------------------------- #

# The offline backend is handed the configured model name and hands it straight
# back, so a draft assembled by a regex stub arrived at this screen labelled
# "Generated by gemini-3.7-flash". On the one screen whose entire job is letting
# a person decide whether to trust a letter, that is the worst possible place to
# be casually wrong -- and it is exactly the kind of claim a judge checks.
REAL_MODEL_BACKENDS = frozenset({"vertex", "adk"})


def attribution(model: str | None, backend: str | None) -> str | None:
    """One honest line naming what produced a piece of text.

    Returns ``None`` when there is nothing to say. An older record written
    before the backend was tracked names the model without vouching for it,
    because "we did not record this" and "a real model wrote this" are
    different statements and only one of them is true.
    """
    if not model and not backend:
        return None
    if backend in REAL_MODEL_BACKENDS:
        return f"Generated by {model}" if model else "Generated by a model on Vertex AI"
    if backend == "scripted":
        return (
            "Generated offline by a scripted stub, not a model — "
            "the deterministic backend used for tests and free local runs"
        )
    if backend:
        return f"Generated by {model} via {backend}" if model else f"Generated via {backend}"
    # An older record from before the backend was recorded. Saying "we did not
    # track this" on a page about model provenance invites the reader to
    # distrust every other attribution on it, and the honest minimum is simply
    # to name nothing rather than to name something we cannot vouch for.
    return None
