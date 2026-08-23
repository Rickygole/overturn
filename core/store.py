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

import json
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
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

    def atomic_update(
        self,
        collection: str,
        doc_id: str,
        mutate: Callable[[dict[str, Any] | None], dict[str, Any] | None],
    ) -> dict[str, Any] | None:
        """Read-modify-write with nothing able to interleave.

        ``mutate`` receives the current document, or ``None`` if absent, and
        returns the document to store, or ``None`` to abort without writing.
        It may be called more than once and must therefore be free of side
        effects.

        This is the primitive that read-then-write cannot be built without.
        A ``get`` followed by a ``set``, however carefully the values are
        compared in between, is a check-then-act race: two callers both read
        revision N, both see no conflict, and both write, and one update is
        gone with no error raised. That is a lost approval.
        """
        ...


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

    def atomic_update(
        self,
        collection: str,
        doc_id: str,
        mutate: Callable[[dict[str, Any] | None], dict[str, Any] | None],
    ) -> dict[str, Any] | None:
        """Atomic under the store lock, which is why the lock is reentrant.

        ``mutate`` runs while the lock is held, so it must not call back into
        this store for a different document or it will serialise the process.
        """
        with self._lock:
            col = self._col(collection)
            current = dict(col[doc_id]) if doc_id in col else None
            updated = mutate(current)
            if updated is None:
                return None
            col[doc_id] = dict(updated)
            return dict(updated)

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
# File backend
# --------------------------------------------------------------------------- #


class FileStore(MemoryStore):
    """A JSON-file store, so local processes can share state.

    ``MemoryStore`` lives inside one process, which is fine for tests and wrong
    for everything a person does. Running the pipeline in one terminal and then
    approving the case from another — or from the web interface — needs the two
    to be looking at the same thing, and without this they are not.

    Deliberately simple: the whole store is one JSON document, rewritten on
    every mutation under a lock. That is the wrong design for anything with
    volume and the right one for a corpus of eight cases on a laptop. Firestore
    is what runs where volume exists.

    The write is atomic via a temporary file and a rename, because a crash
    partway through a rewrite would otherwise leave a truncated file where the
    case state used to be — and losing state is the one failure this project has
    no answer for.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        super().__init__()
        self.path = Path(path or Path.cwd() / "local_state" / "store.json")
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            self._data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"{self.path} exists but could not be read: {exc}. Delete it to "
                f"start from empty, or restore it from a copy."
            ) from exc

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self._data, indent=2, default=str))
        temporary.replace(self.path)

    # Every mutation reloads first, so a change another process made is not
    # silently overwritten, then writes the whole document back.
    def _mutate(self, fn, *args, **kwargs):
        with self._lock:
            self._load()
            result = fn(*args, **kwargs)
            self._flush()
            return result

    def create(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        self._mutate(super().create, collection, doc_id, data)

    def set(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        self._mutate(super().set, collection, doc_id, data)

    def update(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        self._mutate(super().update, collection, doc_id, data)

    def delete(self, collection: str, doc_id: str) -> None:
        self._mutate(super().delete, collection, doc_id)

    def atomic_update(
        self,
        collection: str,
        doc_id: str,
        mutate: Callable[[dict[str, Any] | None], dict[str, Any] | None],
    ) -> dict[str, Any] | None:
        return self._mutate(super().atomic_update, collection, doc_id, mutate)

    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._load()
        return super().get(collection, doc_id)

    def query(self, collection: str, where=None, limit=None, order_by=None):
        with self._lock:
            self._load()
        return super().query(collection, where=where, limit=limit, order_by=order_by)


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

    def atomic_update(
        self,
        collection: str,
        doc_id: str,
        mutate: Callable[[dict[str, Any] | None], dict[str, Any] | None],
    ) -> dict[str, Any] | None:
        """Real Firestore transaction.

        Firestore retries the transactional function on contention, which is
        why ``mutate`` is required to be free of side effects.
        """
        from google.cloud import firestore

        ref = self._ref(collection, doc_id)
        transaction = self._client.transaction()

        @firestore.transactional
        def _run(tx) -> dict[str, Any] | None:
            snapshot = ref.get(transaction=tx)
            current = snapshot.to_dict() if snapshot.exists else None
            updated = mutate(current)
            if updated is None:
                return None
            tx.set(ref, updated)
            return updated

        return _run(transaction)


def build_store() -> DocumentStore:
    """Pick a backend from configuration.

    Local runs default to memory so that nothing in this repository requires a
    cloud project to exercise. Set ``OVERTURN_RUNTIME_MODE=cloud`` for Firestore,
    or point ``FIRESTORE_EMULATOR_HOST`` at the emulator and use cloud mode.
    """
    settings = get_settings()
    if settings.runtime_mode == "cloud" or settings.use_emulator:
        return FirestoreStore()
    if settings.local_state_path:
        return FileStore(settings.local_state_path)
    return MemoryStore()
