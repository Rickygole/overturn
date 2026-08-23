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
from datetime import timedelta
from typing import Any, TypeVar

from core.gateway import Access, GatewayHandle
from core.schemas.action import ActionRecord
from core.schemas.base import utcnow
from core.schemas.enums import ActionType
from core.store import AlreadyExists, DocumentStore
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
            claimed = self._claim(collection, key, case_id, action_type, attempt, digest)

            if not claimed:
                outcome = self._resolve_existing(collection, key, digest)
                if outcome is not None:
                    span.set_attribute("overturn.idempotent_replay", True)
                    span.set_attribute("overturn.delivery_count", outcome.delivery_count)
                    logger.info(
                        "idempotent replay: %s already completed, returning stored result", key
                    )
                    return outcome
                # Claim was expired and has been taken over; fall through and run.
                span.set_attribute("overturn.claim_taken_over", True)

            span.set_attribute("overturn.idempotent_replay", False)
            return self._run_and_record(collection, key, fn)

    # -- internals ---------------------------------------------------------- #

    def _claim(
        self,
        collection: str,
        key: str,
        case_id: str,
        action_type: ActionType,
        attempt: int,
        digest: str,
    ) -> bool:
        """Try to create the claim. Returns False if one already exists."""
        record = ActionRecord(
            action_key=key,
            case_id=case_id,
            action_type=action_type,
            attempt=attempt,
            payload_sha256=digest,
            status="claimed",
        )
        try:
            self._store.create(collection, key, record.to_firestore())
            return True
        except AlreadyExists:
            return False

    def _resolve_existing(self, collection: str, key: str, digest: str) -> ActionOutcome | None:
        """Interpret an existing claim.

        Returns an outcome to replay, or ``None`` if the caller should proceed
        because a dead claim was taken over.
        """
        existing = self._store.get(collection, key)
        if existing is None:
            # Vanishingly rare: deleted between the failed create and this read.
            return None

        stored_digest = existing.get("payload_sha256")
        if stored_digest and stored_digest != digest:
            raise PayloadMismatch(
                f"action {key} was previously claimed with a different payload "
                f"({stored_digest[:12]} vs {digest[:12]}); the caller is reusing an "
                f"action key for different work"
            )

        delivery_count = int(existing.get("delivery_count", 1)) + 1
        status = existing.get("status")

        if status == "completed":
            self._store.update(collection, key, {"delivery_count": delivery_count})
            return ActionOutcome(
                result=existing.get("result"),
                replayed=True,
                delivery_count=delivery_count,
                action_key=key,
            )

        if status == "failed":
            self._store.update(collection, key, {"delivery_count": delivery_count})
            return ActionOutcome(
                result=existing.get("result"),
                replayed=True,
                delivery_count=delivery_count,
                action_key=key,
            )

        # status == "claimed": someone is working on it, or died trying.
        if self._lease_expired(existing):
            logger.warning("taking over expired claim on %s", key)
            self._store.update(
                collection,
                key,
                {
                    "status": "claimed",
                    "claimed_at": utcnow().isoformat(),
                    "delivery_count": delivery_count,
                    "error": "previous claim expired and was taken over",
                },
            )
            return None

        self._store.update(collection, key, {"delivery_count": delivery_count})
        raise ActionInFlight(
            f"action {key} is claimed by another worker; let the message redeliver"
        )

    def _lease_expired(self, record: dict[str, Any]) -> bool:
        raw = record.get("claimed_at")
        if not raw:
            return True
        try:
            from datetime import datetime

            claimed_at = datetime.fromisoformat(str(raw))
        except ValueError:
            return True
        return utcnow() - claimed_at > self._lease

    def _run_and_record(self, collection: str, key: str, fn: Callable[[], Any]) -> ActionOutcome:
        try:
            result = fn()
        except Exception as exc:
            self._store.update(
                collection,
                key,
                {
                    "status": "failed",
                    "error": str(exc)[:1000],
                    "completed_at": utcnow().isoformat(),
                },
            )
            raise

        self._store.update(
            collection,
            key,
            {
                "status": "completed",
                "result": _jsonable(result),
                "completed_at": utcnow().isoformat(),
            },
        )
        return ActionOutcome(result=result, replayed=False, delivery_count=1, action_key=key)


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
