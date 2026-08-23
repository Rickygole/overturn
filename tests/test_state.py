"""Tests for durable case state.

The premise of the product is that a case survives weeks with no process
running. These tests are about whether a worker that has never seen a case can
pick it up correctly and whether two workers can step on each other.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from core.gateway import GatewayHandle, PolicyViolation
from core.schemas.base import utcnow
from core.schemas.case import CaseRecord
from core.schemas.enums import AgentName, CaseStatus
from core.state import CaseNotFound, CaseRepository, ConcurrentModification
from core.store import MemoryStore


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def repo(store: MemoryStore) -> CaseRepository:
    return CaseRepository(store, GatewayHandle(AgentName.ORCHESTRATOR))


def _case(case_id: str = "case-1") -> CaseRecord:
    return CaseRecord(case_id=case_id, source_document_uri=f"gs://intake/{case_id}.pdf")


class TestCreation:
    def test_create_or_get_is_idempotent(self, repo):
        """A redelivered bucket notification must not produce a second case."""
        first, created_first = repo.create_or_get(_case())
        second, created_second = repo.create_or_get(_case())

        assert created_first is True
        assert created_second is False
        assert first.case_id == second.case_id

    def test_load_missing_case_raises(self, repo):
        with pytest.raises(CaseNotFound):
            repo.load("no-such-case")

    def test_try_load_returns_none_instead(self, repo):
        assert repo.try_load("no-such-case") is None


class TestResumption:
    def test_a_cold_worker_reconstructs_the_full_typed_record(self, store):
        """The scenario the whole design exists for.

        One worker writes a case and disappears. Weeks later a different process,
        sharing nothing but the datastore, loads it and finds everything intact.
        """
        writer = CaseRepository(store, GatewayHandle(AgentName.ORCHESTRATOR))
        case = _case()
        case.transition(CaseStatus.SUBMITTED, actor="orchestrator")
        case.submitted_at = utcnow()
        case.set_response_deadline(days=30, accelerated_seconds_per_day=None)
        writer.create(case)

        del writer, case

        cold_worker = CaseRepository(store, GatewayHandle(AgentName.LIFECYCLE))
        resumed = cold_worker.load("case-1")

        assert resumed.status == CaseStatus.SUBMITTED
        assert resumed.response_deadline is not None
        assert resumed.history[-1].actor == "orchestrator"


class TestOverdueQuery:
    def test_finds_only_submitted_cases_past_their_deadline(self, repo):
        overdue = _case("overdue")
        overdue.status = CaseStatus.SUBMITTED
        overdue.submitted_at = utcnow() - timedelta(days=40)
        overdue.response_deadline = utcnow() - timedelta(days=10)
        repo.create(overdue)

        waiting = _case("waiting")
        waiting.status = CaseStatus.SUBMITTED
        waiting.response_deadline = utcnow() + timedelta(days=10)
        repo.create(waiting)

        not_yet_sent = _case("drafting")
        not_yet_sent.status = CaseStatus.DRAFTING
        not_yet_sent.response_deadline = utcnow() - timedelta(days=99)
        repo.create(not_yet_sent)

        found = [c.case_id for c in repo.find_overdue()]
        assert found == ["overdue"]

    def test_overdue_results_are_oldest_deadline_first(self, repo):
        for name, days in (("recent", 1), ("ancient", 60), ("middling", 10)):
            case = _case(name)
            case.status = CaseStatus.SUBMITTED
            case.response_deadline = utcnow() - timedelta(days=days)
            repo.create(case)

        assert [c.case_id for c in repo.find_overdue()] == [
            "ancient",
            "middling",
            "recent",
        ]

    def test_find_by_status(self, repo):
        for name, status in (
            ("a", CaseStatus.AWAITING_APPROVAL),
            ("b", CaseStatus.AWAITING_APPROVAL),
            ("c", CaseStatus.SUBMITTED),
        ):
            case = _case(name)
            case.status = status
            repo.create(case)

        waiting = repo.find_by_status(CaseStatus.AWAITING_APPROVAL)
        assert {c.case_id for c in waiting} == {"a", "b"}


class TestConcurrency:
    def test_stale_write_is_refused(self, repo):
        repo.create(_case())
        stale = repo.load("case-1")
        repo.transition("case-1", CaseStatus.MAPPING)

        with pytest.raises(ConcurrentModification):
            repo.save(stale, expected_revision=stale.revision)

    def test_concurrent_mutations_all_land(self, store):
        """Read-modify-write must not lose an update under contention.

        Eight threads each append one transition. If the retry loop is wrong,
        some of those transitions vanish and the history lies about what
        happened to the case.
        """
        repo = CaseRepository(store, GatewayHandle(AgentName.ORCHESTRATOR))
        repo.create(_case())
        barrier = threading.Barrier(8)

        def append(n: int) -> None:
            from core.schemas.case import StatusTransition

            def add_note(case: CaseRecord) -> None:
                case.history.append(
                    StatusTransition(
                        to_status=case.status, actor=f"worker-{n}", note=f"append {n}"
                    )
                )

            barrier.wait()
            repo.mutate("case-1", add_note)

        threads = [threading.Thread(target=append, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        final = repo.load("case-1")
        assert len(final.history) == 8, "an update was lost under contention"


class TestScoping:
    def test_verification_cannot_write_a_case(self, store):
        repo = CaseRepository(store, GatewayHandle(AgentName.VERIFICATION))
        with pytest.raises(PolicyViolation):
            repo.create(_case())

    def test_verification_can_still_read(self, store):
        CaseRepository(store, GatewayHandle(AgentName.ORCHESTRATOR)).create(_case())
        reader = CaseRepository(store, GatewayHandle(AgentName.VERIFICATION))
        assert reader.load("case-1").case_id == "case-1"
