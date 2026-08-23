"""The guard that stops a redelivered message from filing a second appeal.

Pub/Sub delivers at least once. Cloud Run restarts. A handler *will* be invoked
twice with the same message, and the second invocation must not repeat work that
has an effect outside this system.

Every such action goes through :meth:`IdempotencyGuard.execute`. Before doing
anything, it claims the action by creating a document whose id is
``{case_id}:{action_type}:{attempt}``. The create is conditional: if the
document exists, someone already claimed this action and the stored result is
replayed instead of the action being re-executed.

Three cases the naive version gets wrong, handled here:

1. **The worker crashed mid-action.** The claim would sit in ``claimed`` forever
   and block the action permanently. Claims therefore carry a lease; an expired
   claim can be taken over, and the takeover is recorded.
2. **Two workers race the same claim.** Exactly one create succeeds. The loser
   does not execute; it waits for the winner's result or reports in-flight so
   the message is redelivered rather than dropped.
3. **The same key arrives with a different payload.** That is a caller bug, not
   a duplicate. It is surfaced loudly instead of silently replaying a result
   that answers a different question.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Any, TypeVar

from core.gateway import Access, GatewayHandle
from core.schemas.action import ActionRecord
from core.schemas.base import utcnow
from core.schemas.enums import ActionType
from core.store import DocumentStore
from core.telemetry import agent_span

logger = logging.getLogger(__name__)

T = TypeVar("T")

ACTIONS_COLLECTION = "actions"
DEFAULT_LEASE_SECONDS = 300


class ActionInFlight(Exception):
    """Another worker holds a live claim on this action.

    Deliberately not a failure. The caller should let the message be redelivered
    so the action is attempted again once the current holder finishes or its
    lease expires.
    """


class PayloadMismatch(Exception):
    """The same action key arrived with a different payload."""


class ActionPreviouslyFailed(Exception):
    """This action was attempted before and raised.

    Deliberately not replayed as a result. The earlier version returned the
    failed record the same way it returned a completed one, so a caller that
    had just seen a transient network error on its first try would, on retry,
    receive a successful-looking outcome carrying ``result=None`` — and go on to
    mark the case submitted with a payer deadline for an appeal that was never
    sent. The only trace was the word "unknown" in a history note.

    A deliberate retry is a new attempt number, made by something that has
    decided the earlier failure was safe to repeat.
    """

    def __init__(self, action_key: str, error: str | None) -> None:
        self.action_key = action_key
        self.error = error
        super().__init__(
            f"action {action_key} previously failed and was not retried "
            f"automatically: {error or 'no error recorded'}"
        )


class UnsafeToRetry(Exception):
    """A claim on a non-idempotent action expired without completing.

    The worker holding it died somewhere between calling the payer and recording
    that it had. Re-running would risk a second appeal on one claim; not running
    risks none. Neither is safe to choose automatically, so the case goes to a
    person with both possibilities stated.
    """


# Actions whose external effect cannot be safely repeated. A duplicate
# notification is noise; a duplicate appeal confuses the payer's own process and
# can reset a review clock, which is worse than the appeal not going out.
NOT_SAFELY_REPEATABLE: frozenset[ActionType] = frozenset(
    {ActionType.SUBMIT_APPEAL, ActionType.ESCALATE, ActionType.FILE_EXTERNAL_REVIEW}
)


class _Decision(Enum):
    """What the claim step concluded. Internal to this module."""

    RUN = auto()  # we hold the claim; execute
    REPLAY = auto()  # already done; return the stored result
    IN_FLIGHT = auto()  # someone else holds a live claim
    MISMATCH = auto()  # same key, different payload
    FAILED = auto()  # ran before and raised; not replayed as a result
    UNSAFE = auto()  # a non-repeatable action's claim died mid-flight


@dataclass(frozen=True)
class ActionOutcome:
    """What happened, and whether it actually ran."""

    result: Any
    replayed: bool
    delivery_count: int
    action_key: str

    @property
    def executed(self) -> bool:
        return not self.replayed


def payload_digest(payload: Any) -> str:
    """Stable hash of an action payload.

    ``sort_keys`` matters: two dictionaries that differ only in key order are
    the same payload, and treating them as different would turn every
    redelivery into a spurious mismatch.
    """
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class IdempotencyGuard:
    """Single entry point for every action with an external effect."""

    def __init__(
        self,
        store: DocumentStore,
        gateway: GatewayHandle,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self._store = store
        self._gateway = gateway
        self._lease = timedelta(seconds=lease_seconds)

    def execute(
        self,
        case_id: str,
        action_type: ActionType,
        payload: dict[str, Any],
        fn: Callable[[], Any],
        attempt: int = 1,
    ) -> ActionOutcome:
        """Run ``fn`` exactly once for this ``(case_id, action_type, attempt)``."""
        collection = self._gateway.authorize(ACTIONS_COLLECTION, Access.WRITE)
        key = ActionRecord.make_key(case_id, action_type, attempt)
        digest = payload_digest(payload)

        with agent_span(
            self._gateway.agent.value,
            case_id,
            f"action.{action_type.value}",
            attempt=attempt,
            action_key=key,
        ) as span:
            decision, record = self._claim(collection, key, case_id, action_type, attempt, digest)
            delivery_count = int(record.get("delivery_count", 1))
            span.set_attribute("overturn.delivery_count", delivery_count)

            if decision is _Decision.MISMATCH:
                stored = str(record.get("payload_sha256", ""))
                raise PayloadMismatch(
                    f"action {key} was previously claimed with a different payload "
                    f"({stored[:12]} vs {digest[:12]}); the caller is reusing an "
                    f"action key for different work"
                )

            if decision is _Decision.REPLAY:
                span.set_attribute("overturn.idempotent_replay", True)
                logger.info(
                    "idempotent replay: %s already %s, returning stored result",
                    key,
                    record.get("status"),
                )
                return ActionOutcome(
                    result=record.get("result"),
                    replayed=True,
                    delivery_count=delivery_count,
                    action_key=key,
                )

            if decision is _Decision.FAILED:
                span.set_attribute("overturn.previously_failed", True)
                raise ActionPreviouslyFailed(key, record.get("error"))

            if decision is _Decision.UNSAFE:
                span.set_attribute("overturn.unsafe_to_retry", True)
                logger.error(
                    "action %s cannot be safely retried: the previous worker died "
                    "mid-flight on a non-repeatable action",
                    key,
                )
                raise UnsafeToRetry(
                    f"action {key} was claimed by a worker that died before recording "
                    f"the outcome. It may or may not have reached the payer. A person "
                    f"has to check before this is attempted again."
                )

            if decision is _Decision.IN_FLIGHT:
                span.set_attribute("overturn.idempotent_replay", False)
                raise ActionInFlight(
                    f"action {key} is claimed by another worker; let the message redeliver"
                )

            span.set_attribute("overturn.idempotent_replay", False)
            if record.get("taken_over"):
                span.set_attribute("overturn.claim_taken_over", True)
                logger.warning("took over expired claim on %s", key)
            return self._run_and_record(collection, key, fn, delivery_count)

    # -- internals ---------------------------------------------------------- #

    def _claim(
        self,
        collection: str,
        key: str,
        case_id: str,
        action_type: ActionType,
        attempt: int,
        digest: str,
    ) -> tuple[_Decision, dict[str, Any]]:
        """Claim the action, or work out why we cannot, in one atomic step.

        Everything here happens inside a single ``atomic_update``. The earlier
        version of this method read the record, decided, and then wrote — and
        two redeliveries racing an expired claim both passed the expiry check
        and both went on to execute. Deciding and claiming have to be the same
        operation or the guard does not guard anything.
        """
        outcome: dict[str, Any] = {}

        def mutate(current: dict[str, Any] | None) -> dict[str, Any] | None:
            if current is None:
                fresh = ActionRecord(
                    action_key=key,
                    case_id=case_id,
                    action_type=action_type,
                    attempt=attempt,
                    payload_sha256=digest,
                    status="claimed",
                ).to_firestore()
                outcome["decision"] = _Decision.RUN
                outcome["record"] = fresh
                return fresh

            stored_digest = current.get("payload_sha256")
            if stored_digest and stored_digest != digest:
                outcome["decision"] = _Decision.MISMATCH
                outcome["record"] = current
                return None  # abort the write; nothing about this call is valid

            current["delivery_count"] = int(current.get("delivery_count", 1)) + 1
            status = current.get("status")

            if status == "completed":
                outcome["decision"] = _Decision.REPLAY
                outcome["record"] = current
                return current

            if status == "failed":
                outcome["decision"] = _Decision.FAILED
                outcome["record"] = current
                return current

            if self._lease_expired(current):
                if action_type in NOT_SAFELY_REPEATABLE:
                    # The previous holder died between calling the payer and
                    # recording that it had. We cannot tell which side of that
                    # line it fell on, and guessing wrong files a second appeal.
                    current["status"] = "unsafe_to_retry"
                    outcome["decision"] = _Decision.UNSAFE
                    outcome["record"] = current
                    return current
                current["status"] = "claimed"
                current["claimed_at"] = utcnow().isoformat()
                current["taken_over"] = True
                current["error"] = "previous claim expired and was taken over"
                outcome["decision"] = _Decision.RUN
                outcome["record"] = current
                return current

            outcome["decision"] = _Decision.IN_FLIGHT
            outcome["record"] = current
            return current

        self._store.atomic_update(collection, key, mutate)
        return outcome["decision"], outcome["record"]

    def _lease_expired(self, record: dict[str, Any]) -> bool:
        raw = record.get("claimed_at")
        if not raw:
            return True
        try:
            claimed_at = datetime.fromisoformat(str(raw))
        except ValueError:
            return True
        return utcnow() - claimed_at > self._lease

    def _run_and_record(
        self,
        collection: str,
        key: str,
        fn: Callable[[], Any],
        delivery_count: int,
    ) -> ActionOutcome:
        try:
            result = fn()
        except Exception as exc:
            self._finalise(collection, key, {"status": "failed", "error": str(exc)[:1000]})
            raise

        self._finalise(collection, key, {"status": "completed", "result": _jsonable(result)})
        return ActionOutcome(
            result=result,
            replayed=False,
            delivery_count=delivery_count,
            action_key=key,
        )

    def _finalise(self, collection: str, key: str, fields: dict[str, Any]) -> None:
        """Record the outcome without clobbering a concurrent delivery counter."""

        def mutate(current: dict[str, Any] | None) -> dict[str, Any] | None:
            if current is None:
                return None
            current.update(fields)
            current["completed_at"] = utcnow().isoformat()
            return current

        self._store.atomic_update(collection, key, mutate)


def _jsonable(value: Any) -> Any:
    """Coerce an action result into something a datastore will accept."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if hasattr(value, "to_firestore"):
        return value.to_firestore()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return str(value)
