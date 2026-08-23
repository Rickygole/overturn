"""Tests for the guard that stands between us and a duplicate appeal.

These are the tests that matter most in this repository. Google ran a whole
webinar on this failure mode, framed around a resumable agent ordering two
laptops. The equivalent here is filing two appeals on one claim, which is worse
than doing nothing.
"""

from __future__ import annotations

import threading

import pytest

from core.gateway import GatewayHandle, PolicyViolation
from core.idempotency import (
    ActionInFlight,
    IdempotencyGuard,
    PayloadMismatch,
    payload_digest,
)
from core.schemas.base import utcnow
from core.schemas.enums import ActionType, AgentName
from core.store import MemoryStore


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def guard(store: MemoryStore) -> IdempotencyGuard:
    return IdempotencyGuard(store, GatewayHandle(AgentName.ORCHESTRATOR))


class TestExactlyOnce:
    def test_repeated_delivery_executes_once(self, guard, store):
        """The core promise: Pub/Sub can deliver five times, we act once."""
        calls: list[int] = []

        def submit() -> dict:
            calls.append(1)
            return {"confirmation": "MHP-ACK-88123"}

        outcomes = [
            guard.execute("case-1", ActionType.SUBMIT_APPEAL, {"draft": 2}, submit)
            for _ in range(5)
        ]

        assert len(calls) == 1
        assert outcomes[0].executed is True
        assert all(o.replayed for o in outcomes[1:])
        assert all(o.result == {"confirmation": "MHP-ACK-88123"} for o in outcomes)
        assert store.count("actions") == 1

    def test_delivery_count_is_recorded_as_proof(self, guard, store):
        """The record shows the guard fired, not just that nothing broke."""
        for _ in range(4):
            guard.execute("case-1", ActionType.NOTIFY_HUMAN, {}, lambda: "sent")

        key = "case-1:notify_human:1"
        assert store.get("actions", key)["delivery_count"] == 4

    def test_distinct_attempts_are_distinct_actions(self, guard):
        """Escalating to a second appeal is a new attempt, not a duplicate."""
        calls: list[int] = []

        def escalate() -> str:
            calls.append(1)
            return "escalated"

        guard.execute("case-1", ActionType.ESCALATE, {"level": 1}, escalate, attempt=1)
        guard.execute("case-1", ActionType.ESCALATE, {"level": 2}, escalate, attempt=2)

        assert len(calls) == 2

    def test_different_cases_do_not_collide(self, guard):
        calls: list[str] = []
        for case in ("case-a", "case-b", "case-c"):
            guard.execute(case, ActionType.SUBMIT_APPEAL, {}, lambda c=case: calls.append(c))
        assert calls == ["case-a", "case-b", "case-c"]


class TestConcurrency:
    def test_racing_threads_execute_once(self, store):
        """Two Cloud Run instances receiving the same message at the same moment.

        The loser of the race must not execute. It either replays the winner's
        result or reports the action in flight, and both are correct.
        """
        guard = IdempotencyGuard(store, GatewayHandle(AgentName.ORCHESTRATOR))
        executions: list[int] = []
        barrier = threading.Barrier(8)
        results: list[str] = []

        def worker() -> None:
            barrier.wait()
            try:
                guard.execute(
                    "case-race",
                    ActionType.SUBMIT_APPEAL,
                    {"draft": 1},
                    lambda: executions.append(1) or "done",
                )
                results.append("ok")
            except ActionInFlight:
                results.append("in_flight")

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(executions) == 1, "the action ran more than once under contention"
        assert len(results) == 8
        assert store.count("actions") == 1


class TestFailureHandling:
    def test_failure_is_recorded_and_reraised(self, guard, store):
        def boom() -> None:
            raise RuntimeError("payer endpoint refused the connection")

        with pytest.raises(RuntimeError, match="payer endpoint"):
            guard.execute("case-1", ActionType.SUBMIT_APPEAL, {}, boom)

        record = store.get("actions", "case-1:submit_appeal:1")
        assert record["status"] == "failed"
        assert "payer endpoint" in record["error"]

    def test_failed_action_is_not_silently_retried_under_the_same_key(self, guard):
        """A failure is a decision, not an invitation to loop.

        Retrying the same key would re-run a side effect that may have partially
        landed. A genuine retry is a new attempt number, made deliberately.
        """
        calls: list[int] = []

        def boom() -> None:
            calls.append(1)
            raise RuntimeError("nope")

        with pytest.raises(RuntimeError):
            guard.execute("case-1", ActionType.SUBMIT_APPEAL, {}, boom)

        outcome = guard.execute("case-1", ActionType.SUBMIT_APPEAL, {}, boom)
        assert len(calls) == 1
        assert outcome.replayed is True


class TestClaimLeases:
    def test_expired_claim_is_taken_over(self, store):
        """A worker that crashed mid-action must not block the action forever."""
        guard = IdempotencyGuard(store, GatewayHandle(AgentName.ORCHESTRATOR), lease_seconds=0)
        key = "case-1:submit_appeal:1"
        store.create(
            "actions",
            key,
            {
                "action_key": key,
                "case_id": "case-1",
                "action_type": "submit_appeal",
                "attempt": 1,
                "status": "claimed",
                "claimed_at": utcnow().isoformat(),
                "payload_sha256": payload_digest({"draft": 1}),
                "delivery_count": 1,
            },
        )

        outcome = guard.execute(
            "case-1", ActionType.SUBMIT_APPEAL, {"draft": 1}, lambda: "recovered"
        )
        assert outcome.executed is True
        assert outcome.result == "recovered"

    def test_live_claim_blocks_and_asks_for_redelivery(self, store):
        guard = IdempotencyGuard(store, GatewayHandle(AgentName.ORCHESTRATOR), lease_seconds=3600)
        key = "case-1:submit_appeal:1"
        store.create(
            "actions",
            key,
            {
                "action_key": key,
                "case_id": "case-1",
                "action_type": "submit_appeal",
                "attempt": 1,
                "status": "claimed",
                "claimed_at": utcnow().isoformat(),
                "payload_sha256": payload_digest({"draft": 1}),
                "delivery_count": 1,
            },
        )

        with pytest.raises(ActionInFlight):
            guard.execute(
                "case-1", ActionType.SUBMIT_APPEAL, {"draft": 1}, lambda: "should not run"
            )


class TestPayloadIntegrity:
    def test_same_key_different_payload_is_surfaced(self, guard):
        """Reusing an action key for different work is a bug worth shouting about."""
        guard.execute("case-1", ActionType.SUBMIT_APPEAL, {"draft": 1}, lambda: "ok")

        with pytest.raises(PayloadMismatch):
            guard.execute("case-1", ActionType.SUBMIT_APPEAL, {"draft": 2}, lambda: "ok")

    def test_digest_ignores_key_order(self):
        assert payload_digest({"a": 1, "b": 2}) == payload_digest({"b": 2, "a": 1})

    def test_digest_distinguishes_values(self):
        assert payload_digest({"a": 1}) != payload_digest({"a": 2})


class TestGatewayEnforcement:
    def test_agent_without_actions_access_cannot_use_the_guard(self, store):
        """Drafting has no business claiming actions, and the guard says so."""
        guard = IdempotencyGuard(store, GatewayHandle(AgentName.DRAFTING))
        with pytest.raises(PolicyViolation):
            guard.execute("case-1", ActionType.SUBMIT_APPEAL, {}, lambda: "no")
