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

This module is now wired into the pipeline at the two points where a genuine,
derivable observation exists. `agents/orchestrator/effects.py` and
`agents/orchestrator/pipeline.py` route every write through the gateway
handle on `Fleet.memory`, the same way every other datastore consumer in this
codebase does:

  * **On submission** (`Pipeline.try_submit`, once transmission succeeds):
    `record_submission` notes that an appeal went out against this
    payer/policy/reason. Nothing about the outcome is known yet.
  * **On escalation** (`Pipeline._escalate_one`, the scheduled job): the
    payer's window on the current rung has elapsed with nothing back, which
    is what every escalation in this system means -- `services/payer_sim.py`
    only drives the ladder on `PayerBehaviour.SILENT`, and nothing in this
    codebase ever attaches a `PayerResponse` to a case. `outcome="no_response"`
    is recorded, honestly, rather than inventing an "overturned" or "upheld"
    this system has no way to have observed. The elapsed time against the
    published window is real: it is computed from `submitted_at`, which is a
    stored timestamp, not a guess.

What is deliberately *not* wired: a call recording a real payer response with
a real outcome. No code path in this system ever produces one -- the payer is
simulated, and the simulation's own docstring says it does not respond.
Wiring that call site here would mean fabricating the "overturned" and
"upheld" figures `PayerObservation.summarise()` is capable of showing, and a
number nobody observed is worse than no number. When a real payer response
exists to record, `record_resolution(case, outcome=...)` is already the
function that takes it.

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


def _policy_id(case: CaseRecord) -> str | None:
    """The one policy this observation is scoped to, when Retrieval found one.

    Shared by every method below that needs it, so ``_update`` and a reader
    asking "what does the system know about this payer" resolve the same
    policy for the same case.
    """
    return (
        case.retrieval.sections[0].policy_id
        if case.retrieval and case.retrieval.sections
        else None
    )


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

    def recall_for_case(self, case: CaseRecord) -> PayerObservation | None:
        """What the system has learned about this case's own payer/policy/reason.

        Never an observation about the case itself -- memory is never keyed on
        a patient -- but the scope a reader most often wants: what has this
        payer done on every *other* case that shares this policy and reason
        code. Used by the case page to show what is known before this case's
        own outcome is.
        """
        if case.denial is None:
            return None
        return self.recall(case.denial.payer_name, _policy_id(case), case.denial.denial_reason_code)

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
        observed = self.recall_for_case(case)
        if observed is None or observed.median_response_days is None:
            return published_window
        return max(published_window, int(observed.median_response_days))

    def _update(self, case: CaseRecord, apply) -> PayerObservation | None:
        if case.denial is None:
            return None

        collection = self._gateway.authorize(MEMORY_COLLECTION, Access.WRITE)
        policy_id = _policy_id(case)
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
