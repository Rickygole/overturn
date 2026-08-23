"""Case state: read, mutate, write back safely.

The design constraint that shapes this file: between a submitted appeal and a
payer's response weeks later, no process is running. There is no in-memory
object holding the case together. Everything a future worker needs is in one
Firestore document, and any worker must be able to pick it up cold.

Two consequences:

  * Writes are optimistic-locked on ``revision``. Two workers that both read a
    case and both write it back would otherwise silently lose one of the
    updates, and the lost one might be the approval.
  * ``load`` reconstructs the full typed :class:`CaseRecord`, not a dictionary.
    A worker resuming a case gets the same object the worker that dropped it had.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator

from core.gateway import Access, GatewayHandle
from core.schemas.base import utcnow
from core.schemas.case import AWAITING_PAYER_STATUSES, CaseRecord
from core.schemas.enums import CaseStatus
from core.store import AlreadyExists, DocumentStore
from core.telemetry import agent_span

logger = logging.getLogger(__name__)

CASES_COLLECTION = "cases"
MAX_WRITE_RETRIES = 5


class CaseNotFound(KeyError):
    """No case with that id."""


class ConcurrentModification(Exception):
    """The case changed underneath us between read and write."""


class CaseRepository:
    """Scoped access to the case collection for one agent."""

    def __init__(self, store: DocumentStore, gateway: GatewayHandle) -> None:
        self._store = store
        self._gateway = gateway

    # -- reads -------------------------------------------------------------- #

    def load(self, case_id: str) -> CaseRecord:
        collection = self._gateway.authorize(CASES_COLLECTION, Access.READ)
        data = self._store.get(collection, case_id)
        if data is None:
            raise CaseNotFound(case_id)
        return CaseRecord.model_validate(data)

    def try_load(self, case_id: str) -> CaseRecord | None:
        try:
            return self.load(case_id)
        except CaseNotFound:
            return None

    def find_by_status(self, status: CaseStatus, limit: int | None = None) -> list[CaseRecord]:
        collection = self._gateway.authorize(CASES_COLLECTION, Access.READ)
        rows = self._store.query(collection, where=[("status", "==", status.value)], limit=limit)
        return [CaseRecord.model_validate(data) for _, data in rows]

    def find_overdue(self, limit: int | None = None) -> list[CaseRecord]:
        """Cases awaiting a payer response whose window has elapsed.

        Covers both ``submitted`` and ``escalated``: a case on the second rung
        of the ladder is waiting on the payer exactly as a freshly submitted one
        is, and querying only ``submitted`` would let the ladder stall after one
        escalation with nothing watching the new deadline.

        The deadline filter is applied in Python rather than in the query so that
        the in-memory and Firestore backends agree exactly, and so the demo-mode
        accelerated clock is honoured without a second index.
        """
        waiting: list[CaseRecord] = []
        for status in AWAITING_PAYER_STATUSES:
            waiting.extend(self.find_by_status(status))
        overdue = [case for case in waiting if case.is_overdue]
        overdue.sort(key=lambda c: c.response_deadline or utcnow())
        return overdue[:limit] if limit else overdue

    def iter_all(self) -> Iterator[CaseRecord]:
        collection = self._gateway.authorize(CASES_COLLECTION, Access.READ)
        for _, data in self._store.stream(collection):
            yield CaseRecord.model_validate(data)

    # -- writes ------------------------------------------------------------- #

    def create(self, case: CaseRecord) -> CaseRecord:
        """Create a new case. Fails if the id is taken.

        Intake derives the case id deterministically from the source document,
        so a redelivered notification hits this and is recognised as a duplicate
        rather than producing a second case for the same letter.
        """
        collection = self._gateway.authorize(CASES_COLLECTION, Access.WRITE)
        self._store.create(collection, case.case_id, case.to_firestore())
        return case

    def create_or_get(self, case: CaseRecord) -> tuple[CaseRecord, bool]:
        """Create the case, or return the existing one. ``True`` means created."""
        try:
            return self.create(case), True
        except AlreadyExists:
            return self.load(case.case_id), False

    def save(self, case: CaseRecord, expected_revision: int | None = None) -> CaseRecord:
        """Write the case back, refusing to clobber a concurrent update."""
        collection = self._gateway.authorize(CASES_COLLECTION, Access.WRITE)
        current = self._store.get(collection, case.case_id)
        if current is None:
            raise CaseNotFound(case.case_id)

        if expected_revision is not None:
            actual = int(current.get("revision", 0))
            if actual != expected_revision:
                raise ConcurrentModification(
                    f"case {case.case_id} is at revision {actual}, expected "
                    f"{expected_revision}; reload and reapply"
                )

        case.updated_at = utcnow()
        self._store.set(collection, case.case_id, case.to_firestore())
        return case

    def mutate(self, case_id: str, change: Callable[[CaseRecord], None]) -> CaseRecord:
        """Read-modify-write with retry on conflict.

        ``change`` may be called more than once, so it must be a pure mutation of
        the record it is given and must not have side effects of its own.
        """
        collection = self._gateway.authorize(CASES_COLLECTION, Access.WRITE)

        for retry in range(MAX_WRITE_RETRIES):
            case = self.load(case_id)
            before = case.revision
            change(case)
            if case.revision == before:
                case.revision += 1
            case.updated_at = utcnow()

            current = self._store.get(collection, case_id)
            if current is not None and int(current.get("revision", 0)) != before:
                logger.info(
                    "case %s changed during mutate (retry %d/%d)",
                    case_id,
                    retry + 1,
                    MAX_WRITE_RETRIES,
                )
                continue

            self._store.set(collection, case_id, case.to_firestore())
            return case

        raise ConcurrentModification(
            f"case {case_id} kept changing across {MAX_WRITE_RETRIES} attempts"
        )

    def transition(
        self,
        case_id: str,
        to: CaseStatus,
        note: str | None = None,
    ) -> CaseRecord:
        """Advance a case's status, recording who moved it and why."""
        actor = self._gateway.agent.value
        with agent_span(actor, case_id, f"transition.{to.value}"):
            return self.mutate(case_id, lambda c: c.transition(to, actor=actor, note=note))
