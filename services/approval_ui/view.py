"""Everything the templates would otherwise have to work out for themselves.

Presentation logic lives here rather than in Jinja so that it can be read, and
tested, as Python. The templates are left doing what templates are good at:
deciding what appears and in what order.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime

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
    """One line of the queue. Never invents a value it was not given."""
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
    }


def _verification_summary(case: CaseRecord) -> str:
    latest = case.latest_verification
    if latest is None:
        return "not verified"
    return "passed" if latest.passed else "rejected"


def check_rows(result: VerificationResult | None) -> list[dict[str, object]]:
    """The three checks Verification runs, and how each one came out.

    Rendered even when everything passed. "Nothing was wrong" is only meaningful
    if the reader can see what was looked for.
    """
    if result is None:
        return []
    return [
        {
            "label": "Every cited section id exists in the retrieved policy set",
            "passed": not result.citations_nonexistent,
            "detail": (
                f"{result.citations_checked} citation"
                f"{'' if result.citations_checked == 1 else 's'} checked"
                if not result.citations_nonexistent
                else "Not found: " + ", ".join(result.citations_nonexistent)
            ),
        },
        {
            "label": "The source text supports what the letter says it says",
            "passed": not result.citations_unsupported,
            "detail": (
                "Each cited section was re-read against the claim made"
                if not result.citations_unsupported
                else "Unsupported: " + ", ".join(result.citations_unsupported)
            ),
        },
        {
            "label": "Every clinical assertion traces to a row in the criteria matrix",
            "passed": not result.ungrounded_assertions,
            "detail": (
                "No assertion was made that the matrix does not carry"
                if not result.ungrounded_assertions
                else f"{len(result.ungrounded_assertions)} assertion(s) with no matrix row"
            ),
        },
    ]


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


def evidence_count(verdicts: list[CriterionVerdict]) -> int:
    return sum(len(v.evidence) for v in verdicts)


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

    Separate from :func:`check_rows`, which reports the same three checks as a
    read-only table. The wording here is addressed to the person about to sign:
    a checkbox that says "citations resolve" without showing *which* citations
    were resolved is a box to tick rather than a thing to confirm.

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


@dataclass(frozen=True)
class Overview:
    """The whole workload in one glance."""

    tiles: list[Tile]
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

    tiles = [
        Tile(
            "Waiting on you",
            len(clerk),
            "Drafted, verified, and needing a clerk's decision.",
            "#awaiting-h",
            "act",
        ),
        Tile(
            "Waiting on a clinician",
            len(clinician),
            "A clerk has signed. Nothing sends until the clinician co-signs.",
            "#cosign-h",
            "act" if clinician else "quiet",
        ),
        Tile(
            "Sent back to you",
            len(back),
            "The fleet could not finish these, or a reviewer rejected the draft.",
            "#review-h",
            "act" if back else "quiet",
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
                    "waiting_on": _waiting_on(case.status),
                }
            )
    urgent.sort(key=lambda row: row["deadline"].days)  # type: ignore[union-attr,index]

    return Overview(
        tiles=tiles,
        closed=closed,
        urgent=urgent,
        total=len(cases),
        actionable=len(clerk) + len(clinician) + len(back),
    )


def _waiting_on(status: CaseStatus) -> str:
    if status == WAITING_ON_CLERK:
        return "your decision"
    if status == WAITING_ON_CLINICIAN:
        return "the clinician's co-sign"
    return "human review"


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
