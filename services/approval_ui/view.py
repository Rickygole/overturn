"""Everything the templates would otherwise have to work out for themselves.

Presentation logic lives here rather than in Jinja so that it can be read, and
tested, as Python. The templates are left doing what templates are good at:
deciding what appears and in what order.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from core.schemas.case import CaseRecord
from core.schemas.criteria import CriterionVerdict
from core.schemas.denial import DenialExtraction, DeniedService
from core.schemas.draft import AppealDraft
from core.schemas.enums import CaseStatus, CriterionVerdictValue
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


def service_line(denial: DenialExtraction | None) -> str:
    """One line naming what was refused, for the queue table."""
    if denial is None or not denial.services:
        return NOT_STATED
    first = denial.services[0].description
    extra = len(denial.services) - 1
    return f"{first} (+{extra} more)" if extra else first


def queue_row(case: CaseRecord) -> dict[str, object]:
    """One line of the queue. Never invents a value it was not given.

    ``waiting_on`` used to be a *section* — three tables with three headings,
    each saying in prose what one column can say per row. It is a column now,
    which is what it always was.
    """
    denial = case.denial
    return {
        "case_id": case.case_id,
        "patient": (denial.patient_name if denial and denial.patient_name else NOT_STATED),
        "payer": denial.payer_name if denial else NOT_STATED,
        "service": service_line(denial),
        "deadline": deadline_view(denial.appeal_deadline if denial else None),
        "attempts": case.draft_attempts,
        "verified": _verification_summary(case),
        "reason": case.needs_human_reason,
        "updated": case.updated_at,
        "waiting_on": waiting_on(case.status),
        "waiting_key": waiting_key(case.status),
        "attempt_marks": attempt_marks(case),
        "link_suffix": "/clinical" if case.status == WAITING_ON_CLINICIAN else "",
    }


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


def _verification_summary(case: CaseRecord) -> str:
    latest = case.latest_verification
    if latest is None:
        return "not verified"
    return "passed" if latest.passed else "rejected"


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


def claim_ledger(case: CaseRecord, draft: AppealDraft | None) -> list[dict[str, object]]:
    """One row per claim the letter makes: the claim, the policy text it rests
    on, the chart evidence under it, and the verdict.

    Driven by ``draft.citations`` rather than by the matrix, because the
    question at this gate is "is what the letter says true", not "what did
    Mapping conclude". A matrix row the letter never uses is not something a
    clerk has to sign for; a claim with nothing under it is.

    Flagged rows sort first. Everything else keeps the letter's own order,
    which is the order the reader just met the claims in.
    """
    if draft is None:
        return []

    retrieval = case.retrieval
    verdicts = {v.criterion_id: v for v in (case.criteria.verdicts if case.criteria else [])}
    findings = case_findings(case)

    rows: list[dict[str, object]] = []
    for position, citation in enumerate(draft.citations):
        source = retrieval.text_for(citation.section_id) if retrieval else None
        source_note = None if source else (NOT_RETRIEVED if retrieval else NO_RETRIEVAL)

        supporting = [verdicts[c] for c in citation.supporting_criterion_ids if c in verdicts]
        unevaluated = [c for c in citation.supporting_criterion_ids if c not in verdicts]
        contested = findings_at(
            findings, citation.section_id, *citation.supporting_criterion_ids
        )

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
            }
        )

    rows.sort(key=lambda row: (not row["flagged"], row["position"]))
    return rows


def _flag_reason(
    contested: list[Finding],
    source_note: str | None,
    unevaluated: list[str],
    weak: list[CriterionVerdict],
    unevidenced: list[CriterionVerdict],
) -> str | None:
    """One line naming why a row sorted to the top, in severity order."""
    if contested:
        return f"Verification contested this on attempt {contested[0].attempt}."
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

    Confidence is suppressed on a contested or unevidenced row. `100%` beside
    an evidence cell reading "No chart evidence cited" is a contradiction on
    its face, and a clerk reads the number and moves on.
    """
    matrix = case.criteria
    if matrix is None:
        return []
    findings = case_findings(case)

    rows: list[dict[str, object]] = []
    for verdict in matrix.verdicts:
        contested = findings_at(findings, verdict.criterion_id)
        show_confidence = not contested and bool(verdict.evidence)
        rows.append(
            {
                "verdict": verdict,
                "findings": contested,
                "contested": bool(contested),
                "show_confidence": show_confidence,
                "confidence_note": _confidence_note(contested, verdict),
            }
        )
    return rows


def _confidence_note(contested: list[Finding], verdict: CriterionVerdict) -> str | None:
    if contested:
        return "Not shown — Verification contested this row."
    if not verdict.evidence:
        return "Not shown — no chart evidence is cited on this row."
    return None


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
    """One sentence naming which attempt this is and what was caught before it."""

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
        text = (
            f"Attempt {draft.attempt}, and Verification passed it. "
            f"Nothing was sent back."
        )
        return Provenance(text, draft.attempt, [], [])

    listed = _join_numbers(sent_back)
    text = (
        f"Attempt {draft.attempt}. Verification sent {listed} back"
        + (f" — it caught {_join_prose(caught)}." if caught else ".")
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
        headline=(
            f"Sentinel found {count} item{'' if count == 1 else 's'} in this document"
        ),
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
    """One segment of the caseload bar."""

    label: str
    count: int
    tone: str


@dataclass(frozen=True)
class Overview:
    """The whole workload in one glance."""

    tiles: list[Tile]
    bands: list[Band]
    closed: list[Tile]
    urgent: list[dict[str, object]]
    total: int
    actionable: int


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
                    "patient": (
                        case.denial.patient_name
                        if case.denial and case.denial.patient_name
                        else NOT_STATED
                    ),
                    "deadline": seen,
                    "waiting_on": waiting_on(case.status),
                }
            )
    urgent.sort(key=lambda row: row["deadline"].days)  # type: ignore[union-attr,index]

    # The same counts as one shape. Five numbers read as five facts; one bar
    # reads as a workload -- how much of today is waiting on a person, how much
    # is running, how much is done. Empty bands are dropped rather than drawn
    # as slivers: a segment too thin to see is a segment that misleads.
    bands = [
        Band("Waiting on you", len(clerk), "act"),
        Band("Waiting on a clinician", len(clinician), "act"),
        Band("Sent back to you", len(back), "review"),
        Band("Agents still working", len(in_flight), "wait"),
        Band("With the payer", len(with_payer), "wait"),
        Band("Closed", sum(len(by_status.get(st, [])) for st, _, _ in CLOSED_STATUSES), "quiet"),
    ]

    return Overview(
        tiles=tiles,
        bands=[band for band in bands if band.count],
        closed=closed,
        urgent=urgent,
        total=len(cases),
        actionable=len(clerk) + len(clinician) + len(back),
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
    return f"Recorded as {model}, before the backend was tracked"
