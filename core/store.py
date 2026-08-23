"""Document store abstraction with a real and an in-memory backend.

Two backends, one interface:

  * ``FirestoreStore`` talks to real Firestore. The same class also covers the
    local emulator, because the Google client library switches on
    ``FIRESTORE_EMULATOR_HOST`` by itself.
  * ``MemoryStore`` is a faithful in-process implementation used by tests and by
    the offline pipeline runner, so the whole system can be exercised end to end
    without a network or a bill.

``create`` is the important method. It must fail if the document already exists,
because that failure is what the idempotency guard is built on. Both backends
implement it with that exact semantic and there is a test that holds them to it.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any, Protocol, runtime_checkable

from core.config import get_settings


class AlreadyExists(Exception):
    """Raised by ``create`` when the document id is taken."""


@runtime_checkable
class DocumentStore(Protocol):
    """The only datastore surface the rest of the system knows about."""

    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None: ...

    def create(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        """Write only if absent. Raises ``AlreadyExists`` otherwise."""
        ...

    def set(self, collection: str, doc_id: str, data: dict[str, Any]) -> None: ...

    def update(self, collection: str, doc_id: str, data: dict[str, Any]) -> None: ...

    def delete(self, collection: str, doc_id: str) -> None: ...

    def query(
        self,
        collection: str,
        where: list[tuple[str, str, Any]] | None = None,
        limit: int | None = None,
        order_by: str | None = None,
    ) -> list[tuple[str, dict[str, Any]]]: ...

    def stream(self, collection: str) -> Iterator[tuple[str, dict[str, Any]]]: ...


# --------------------------------------------------------------------------- #
# In-memory backend
# --------------------------------------------------------------------------- #


class MemoryStore:
    """Thread-safe in-process store.

    The lock is not decoration. The duplicate-delivery test spawns real threads
    to race the idempotency guard, and without a lock this backend would report
    success where Firestore would report a conflict, which would make the test
    prove nothing.
    """

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict[str, Any]]] = {}
        self._lock = threading.RLock()

    def _col(self, collection: str) -> dict[str, dict[str, Any]]:
        return self._data.setdefault(collection, {})

    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        with self._lock:
            doc = self._col(collection).get(doc_id)
            return dict(doc) if doc is not None else None

    def create(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        with self._lock:
            col = self._col(collection)
            if doc_id in col:
                raise AlreadyExists(f"{collection}/{doc_id}")
            col[doc_id] = dict(data)

    def set(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._col(collection)[doc_id] = dict(data)

    def update(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        with self._lock:
            col = self._col(collection)
            if doc_id not in col:
                raise KeyError(f"{collection}/{doc_id} does not exist")
            col[doc_id].update(data)

    def delete(self, collection: str, doc_id: str) -> None:
        with self._lock:
            self._col(collection).pop(doc_id, None)

    def query(
        self,
        collection: str,
        where: list[tuple[str, str, Any]] | None = None,
        limit: int | None = None,
        order_by: str | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        with self._lock:
            rows = [(k, dict(v)) for k, v in self._col(collection).items()]

        for field, op, value in where or []:
            rows = [r for r in rows if _matches(r[1].get(field), op, value)]
        if order_by:
            rows.sort(key=lambda r: (r[1].get(order_by) is None, r[1].get(order_by)))
        if limit is not None:
            rows = rows[:limit]
        return rows

    def stream(self, collection: str) -> Iterator[tuple[str, dict[str, Any]]]:
        with self._lock:
            snapshot = [(k, dict(v)) for k, v in self._col(collection).items()]
        yield from snapshot

    # Test and demo affordances, not part of the Protocol.
    def count(self, collection: str) -> int:
        with self._lock:
            return len(self._col(collection))

    def collections(self) -> list[str]:
        with self._lock:
            return sorted(self._data)


def _matches(actual: Any, op: str, expected: Any) -> bool:
    if actual is None and op not in ("==", "!="):
        return False
    match op:
        case "==":
            return actual == expected
        case "!=":
            return actual != expected
        case "<":
            return actual < expected
        case "<=":
            return actual <= expected
        case ">":
            return actual > expected
        case ">=":
            return actual >= expected
        case "in":
            return actual in expected
        case "not-in":
            return actual not in expected
        case _:
            raise ValueError(f"unsupported query operator {op!r}")


# --------------------------------------------------------------------------- #
# Firestore backend
# --------------------------------------------------------------------------- #


class FirestoreStore:
    """Real Firestore. Also covers the emulator via ``FIRESTORE_EMULATOR_HOST``."""

    def __init__(self, project_id: str | None = None) -> None:
        from google.cloud import firestore  # imported lazily; tests never need it

        settings = get_settings()
        self._client = firestore.Client(project=project_id or settings.project_id)

    def _ref(self, collection: str, doc_id: str):
        return self._client.collection(collection).document(doc_id)

    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        snap = self._ref(collection, doc_id).get()
        return snap.to_dict() if snap.exists else None

    def create(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        from google.api_core import exceptions as gexc

        try:
            self._ref(collection, doc_id).create(data)
        except gexc.AlreadyExists as exc:
            raise AlreadyExists(f"{collection}/{doc_id}") from exc

    def set(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        self._ref(collection, doc_id).set(data)

    def update(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        self._ref(collection, doc_id).update(data)

    def delete(self, collection: str, doc_id: str) -> None:
        self._ref(collection, doc_id).delete()

    def query(
        self,
        collection: str,
        where: list[tuple[str, str, Any]] | None = None,
        limit: int | None = None,
        order_by: str | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        q = self._client.collection(collection)
        for field, op, value in where or []:
            q = q.where(filter=FieldFilter(field, op, value))
        if order_by:
            q = q.order_by(order_by)
        if limit is not None:
            q = q.limit(limit)
        return [(d.id, d.to_dict()) for d in q.stream()]

    def stream(self, collection: str) -> Iterator[tuple[str, dict[str, Any]]]:
        for doc in self._client.collection(collection).stream():
            yield doc.id, doc.to_dict()


def build_store() -> DocumentStore:
    """Pick a backend from configuration.

    Local runs default to memory so that nothing in this repository requires a
    cloud project to exercise. Set ``OVERTURN_RUNTIME_MODE=cloud`` for Firestore,
    or point ``FIRESTORE_EMULATOR_HOST`` at the emulator and use cloud mode.
    """
    settings = get_settings()
    if settings.runtime_mode == "cloud" or settings.use_emulator:
        return FirestoreStore()
    return MemoryStore()
