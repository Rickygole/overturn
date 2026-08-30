"""Cross-case memory.

The track asks for persistent, secure, cross-session context over extended
timelines. The managed Memory Bank *was* reachable on this project — the probe
in `docs/PLATFORM_PROBE.md` got `200 {}` from it, the same as Agent Runtime.
It is not used here by choice, not because it was unavailable: a managed
Memory Bank is scoped to a session, and what this needs to survive is weeks
between a submission and a payer's answer, queried by payer/policy/reason
code rather than by session. That is the same reasoning `ARCHITECTURE.md`
gives for running the fleet on Cloud Run instead of the managed Agent Runtime.
So this implements the same contract directly on Firestore instead.

Say the other half plainly too: this module is implemented and covered by
`tests/test_memory.py`, but nothing in the running pipeline calls it. No agent
imports `core.memory`. The write and read grants exist in `core/gateway.py`
and the collection is named in `core/config.py`, but a grant is not a call
site, and this is one until an agent actually reaches for it.

What is worth remembering here is narrow and specific, and getting the scope
right matters more than the storage:

**What this remembers.** Facts about a *payer's behaviour* that only become
visible across many cases and many weeks. How long this payer actually takes to
answer a first-level appeal, as opposed to the thirty days it publishes. Which
denial reason codes get overturned when appealed and which never do. Which
policy sections have been successfully argued before.

**What this deliberately does not remember.** Anything about a patient. Memory
is keyed on payer, policy, and denial reason code — never on a member. A
clinic's leverage against an insurer is a legitimate thing to accumulate; a
longitudinal profile of a sick person assembled as a side effect is not, and
the difference is a schema decision rather than a policy anyone has to follow.

The observations are used to inform a human, not to change an agent's verdict.
Mapping does not get told "this criterion usually passes" — that is how you
build a system that agrees with itself.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from core.gateway import Access, GatewayHandle
from core.schemas.base import OverturnModel, utcnow
from core.schemas.case import CaseRecord
from core.store import DocumentStore

MEMORY_COLLECTION = "case_memory"


class PayerObservation(OverturnModel):
    """What has been learned about one payer, at one policy, one reason code.

    Keyed on nothing patient-identifying. That is the whole design constraint.
    """

    key: str = Field(description="payer|policy_id|reason_code")
    payer_name: str
    policy_id: str | None = None
    denial_reason_code: str | None = None

    appeals_submitted: int = 0
    responses_received: int = 0
    overturned: int = 0
    upheld: int = 0

    response_days_observed: list[float] = Field(
        default_factory=list,
        description="Actual turnaround, which is frequently not the published window.",
    )
    escalations_required: list[int] = Field(
        default_factory=list, description="How many rungs it took, per resolved case."
    )
    sections_successfully_cited: list[str] = Field(default_factory=list)

    first_seen: datetime = Field(default_factory=utcnow)
    last_updated: datetime = Field(default_factory=utcnow)

    @property
    def median_response_days(self) -> float | None:
        if not self.response_days_observed:
            return None
        ordered = sorted(self.response_days_observed)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / 2

    @property
    def overturn_rate(self) -> float | None:
        """Only meaningful once enough cases have actually resolved."""
        resolved = self.overturned + self.upheld
        if resolved < 3:
            return None
        return self.overturned / resolved

    def summarise(self) -> str:
        """One line a billing clerk can act on."""
        parts: list[str] = []
        if (rate := self.overturn_rate) is not None:
            parts.append(
                f"{self.overturned} of {self.overturned + self.upheld} appeals on this "
                f"reason code were overturned ({rate:.0%})"
            )
        if (median := self.median_response_days) is not None:
            parts.append(f"this payer has taken a median of {median:.0f} days to respond")
        if self.escalations_required:
            average = sum(self.escalations_required) / len(self.escalations_required)
            if average >= 1:
                parts.append(f"resolution has typically required {average:.1f} escalation(s)")
        if not parts:
            return "Not enough resolved cases yet to say anything useful."
        return "; ".join(parts).capitalize() + "."


def observation_key(payer_name: str, policy_id: str | None, reason_code: str | None) -> str:
    return "|".join([payer_name, policy_id or "any", reason_code or "any"])


class MemoryBank:
    """Scoped read and write access to cross-case memory."""

    def __init__(self, store: DocumentStore, gateway: GatewayHandle) -> None:
        self._store = store
        self._gateway = gateway

    def recall(
        self, payer_name: str, policy_id: str | None, reason_code: str | None
    ) -> PayerObservation | None:
        collection = self._gateway.authorize(MEMORY_COLLECTION, Access.READ)
        key = observation_key(payer_name, policy_id, reason_code)
        data = self._store.get(collection, key)
        return PayerObservation.model_validate(data) if data else None

    def record_submission(self, case: CaseRecord) -> PayerObservation | None:
        """Note that an appeal went out. Called once per submission."""
        return self._update(
            case, lambda obs: setattr(obs, "appeals_submitted", obs.appeals_submitted + 1)
        )

    def record_resolution(self, case: CaseRecord, outcome: str) -> PayerObservation | None:
        """Note how a case actually ended, and how long it took.

        This is the entry that makes the memory worth having: the published
        response window is a claim, and this is the measurement.
        """

        def apply(obs: PayerObservation) -> None:
            obs.responses_received += 1
            if outcome == "overturned":
                obs.overturned += 1
            elif outcome == "upheld":
                obs.upheld += 1

            if case.submitted_at:
                elapsed = (utcnow() - case.submitted_at).total_seconds() / 86400
                obs.response_days_observed.append(round(elapsed, 2))
                # Keep the window bounded; ancient turnaround is not evidence
                # about how this payer behaves now.
                obs.response_days_observed = obs.response_days_observed[-50:]

            obs.escalations_required.append(case.escalation_count)
            obs.escalations_required = obs.escalations_required[-50:]

            if outcome == "overturned" and case.approved_draft():
                cited = sorted(case.approved_draft().cited_ids())
                obs.sections_successfully_cited = sorted(
                    set(obs.sections_successfully_cited) | set(cited)
                )[:100]

        return self._update(case, apply)

    def expected_response_days(self, case: CaseRecord, published_window: int) -> int:
        """What to actually expect, informed by what this payer has done before.

        Used to decide when to *look*, never to decide the outcome. The deadline
        written onto the case stays the published one, because that is the
        contractual figure and the one an appeal would cite.
        """
        if case.denial is None:
            return published_window
        observed = self.recall(
            case.denial.payer_name,
            case.retrieval.sections[0].policy_id
            if case.retrieval and case.retrieval.sections
            else None,
            case.denial.denial_reason_code,
        )
        if observed is None or observed.median_response_days is None:
            return published_window
        return max(published_window, int(observed.median_response_days))

    def _update(self, case: CaseRecord, apply) -> PayerObservation | None:
        if case.denial is None:
            return None

        collection = self._gateway.authorize(MEMORY_COLLECTION, Access.WRITE)
        policy_id = (
            case.retrieval.sections[0].policy_id
            if case.retrieval and case.retrieval.sections
            else None
        )
        key = observation_key(case.denial.payer_name, policy_id, case.denial.denial_reason_code)

        def mutate(current: dict | None) -> dict:
            observation = (
                PayerObservation.model_validate(current)
                if current
                else PayerObservation(
                    key=key,
                    payer_name=case.denial.payer_name,
                    policy_id=policy_id,
                    denial_reason_code=case.denial.denial_reason_code,
                )
            )
            apply(observation)
            observation.last_updated = utcnow()
            return observation.to_firestore()

        result = self._store.atomic_update(collection, key, mutate)
        return PayerObservation.model_validate(result) if result else None


def contains_no_patient_identifiers(observation: PayerObservation) -> bool:
    """Assertion used by the tests, and stated here so the rule is findable.

    Memory is keyed on payer, policy and reason code. If a patient identifier
    ever appears in this collection, the scope has slipped and the fix is a
    schema change, not a redaction pass.
    """
    blob = observation.model_dump_json().lower()
    return not any(
        token in blob for token in ("member_id", "patient_name", "date_of_birth", "nbh-4417")
    )
