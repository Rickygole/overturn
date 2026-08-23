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
from core.schemas.denial import DenialExtraction
from core.schemas.draft import AppealDraft
from core.schemas.enums import CriterionVerdictValue
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


FILTERS = {
    "fmt_date": fmt_date,
    "fmt_datetime": fmt_datetime,
    "pct": pct,
    "confidence_band": confidence_band,
    "verdict_label": verdict_label,
    "verdict_key": verdict_key,
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
