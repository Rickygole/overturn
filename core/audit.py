"""Append-only audit trail.

One event per agent invocation, written whether the agent succeeded, failed, or
refused to act. Nothing here is ever updated or deleted, and the gateway grants
every agent ``APPEND`` rather than ``WRITE`` so that is enforced rather than
merely intended.

Patient detail is hashed, not stored. The audit log is the collection with the
broadest read access in the system, and a denial letter contains a member id, a
name and a date of birth. The hash proves what was fed in without republishing
it into the one place everyone can look.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from core.gateway import Access, GatewayHandle
from core.schemas.audit import AuditEvent
from core.schemas.enums import AgentName
from core.store import DocumentStore
from core.telemetry import current_trace_ids

logger = logging.getLogger(__name__)

AUDIT_COLLECTION = "audit_events"


def content_digest(value: Any) -> str:
    """SHA-256 of any payload, stable across key ordering."""
    if isinstance(value, bytes):
        return hashlib.sha256(value).hexdigest()
    if isinstance(value, str):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    encoded = json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass
class Recording:
    """A mutable handle an agent fills in while it works.

    The agent sets ``decision`` and whatever telemetry it has; the context
    manager writes the event on the way out, including when the body raised.
    """

    decision: str = "no decision recorded"
    output: dict[str, Any] | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    input_summary: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class AuditLog:
    """Writer for the append-only event collection."""

    def __init__(self, store: DocumentStore, gateway: GatewayHandle) -> None:
        self._store = store
        self._gateway = gateway

    @property
    def agent(self) -> AgentName:
        return self._gateway.agent

    def write(self, event: AuditEvent) -> str:
        """Append one event. Never overwrites; a duplicate id is a bug."""
        collection = self._gateway.authorize(AUDIT_COLLECTION, Access.APPEND)
        self._store.create(collection, event.event_id, event.to_firestore())
        return event.event_id

    @contextmanager
    def record(
        self,
        case_id: str,
        operation: str,
        payload: Any,
        attempt: int = 1,
    ) -> Iterator[Recording]:
        """Wrap an agent invocation and emit exactly one audit event.

        Emits on the failure path too. An agent that crashed without leaving a
        trace is the case an auditor most wants to see.
        """
        recording = Recording()
        started = time.perf_counter()
        error: str | None = None
        try:
            yield recording
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:1000]
            recording.decision = f"failed: {error}"
            raise
        finally:
            trace_id, span_id = current_trace_ids()
            event = AuditEvent(
                event_id=str(uuid.uuid4()),
                case_id=case_id,
                agent=self.agent,
                operation=operation,
                input_sha256=content_digest(payload),
                input_summary=recording.input_summary,
                output=recording.output,
                decision=recording.decision,
                succeeded=error is None,
                error=error,
                model=recording.model,
                input_tokens=recording.input_tokens,
                output_tokens=recording.output_tokens,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                trace_id=trace_id,
                span_id=span_id,
                attempt=attempt,
            )
            try:
                self.write(event)
            except Exception:  # pragma: no cover - audit must never mask the real error
                logger.exception("failed to write audit event for case %s", case_id)


def read_case_trail(
    store: DocumentStore, gateway: GatewayHandle, case_id: str, limit: int | None = None
) -> list[AuditEvent]:
    """Every event for one case, oldest first.

    This is what the approval interface shows a clerk and what gets pointed at
    in the demo when the question is 'why did it do that'.

    Takes a ``GatewayHandle`` for the same reason every other reader of this
    store does, `core/state.py`'s `CaseRepository` included: this function
    used to query `AUDIT_COLLECTION` directly with no handle at all, which
    made `core/gateway.py`'s "no second door" claim false about the one
    collection every agent can read. Every agent holds at least `APPEND` on
    `audit_events`, and `APPEND` implies `READ`, so this needs no `POLICY`
    change -- callers pass whichever identity is already in scope.
    """
    collection = gateway.authorize(AUDIT_COLLECTION, Access.READ)
    rows = store.query(collection, where=[("case_id", "==", case_id)])
    events = [AuditEvent.model_validate(data) for _, data in rows]
    events.sort(key=lambda e: e.at)
    return events[:limit] if limit else events
