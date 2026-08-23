"""The multi-week lifecycle.

This is the part of the system that is genuinely hard to fake, and the part
that is hardest to test, because the thing being tested is the passage of weeks.

The trick these tests use is the same one the product uses: ``is_overdue`` is a
pure function of stored state, so a case can be aged by writing a deadline into
the past. No sleeping, no mocking of the clock, and the code under test is the
same code that runs when a real month goes by.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from agents.intake.documents import SourceDocument
from agents.offline.handlers import build_offline_llm
from agents.orchestrator.deps import build_fleet
from agents.orchestrator.pipeline import Pipeline
from core.schemas.base import utcnow
from core.schemas.case import ClinicianCosign, HumanDecision, PayerResponse
from core.schemas.enums import ActionType, AppealLevel, CaseStatus
from core.store import MemoryStore
from pathlib import Path

DENIALS = Path(__file__).resolve().parents[1] / "data" / "denials"


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def pipeline(store: MemoryStore) -> Pipeline:
    return Pipeline(build_fleet(store=store, llm=build_offline_llm()))


def _to_submitted(pipeline: Pipeline, case_id: str = "CASE-001"):
    """Run a case through the gate to a submitted appeal."""
    pipeline.ingest(
        SourceDocument(
            uri=f"gs://overturn-intake/{case_id}.txt",
            data=(DENIALS / f"{case_id}.txt").read_bytes(),
            mime_type="text/plain",
        ),
        case_id=case_id,
    )

    def sign(case):
        case.human_decision = HumanDecision(
            decided_by="clerk@clinic.example",
            approved=True,
            draft_attempt_approved=case.latest_draft.attempt,
            citations_checked=True,
            quotes_checked=True,
            assertions_checked=True,
        )
        case.clinician_cosign = ClinicianCosign(
            clinician_name="M. Castellanos",
            credential="MD",
            attests_clinical_accuracy=True,
            draft_attempt_signed=case.latest_draft.attempt,
        )
        case.transition(CaseStatus.APPROVED, actor="clerk@clinic.example")

    pipeline.fleet.cases.mutate(case_id, sign)
    return pipeline.try_submit(case_id)


def _age(pipeline: Pipeline, case_id: str, days: int = 1) -> None:
    """Move a case's deadline into the past. This is how weeks pass."""
    pipeline.fleet.cases.mutate(
        case_id,
        lambda c: setattr(c, "response_deadline", utcnow() - timedelta(days=days)),
    )


class TestTheGate:
    def test_a_clerk_signature_alone_does_not_submit(self, pipeline, store):
        pipeline.ingest(
            SourceDocument(
                uri="gs://overturn-intake/CASE-001.txt",
                data=(DENIALS / "CASE-001.txt").read_bytes(),
                mime_type="text/plain",
            ),
            case_id="CASE-001",
        )

        def clerk_only(case):
            case.human_decision = HumanDecision(
                decided_by="clerk",
                approved=True,
                draft_attempt_approved=1,
                citations_checked=True,
                quotes_checked=True,
                assertions_checked=True,
            )
            case.transition(CaseStatus.APPROVED, actor="clerk")

        pipeline.fleet.cases.mutate("CASE-001", clerk_only)
        case = pipeline.try_submit("CASE-001")

        assert case.ready_to_submit is False
        assert case.status is not CaseStatus.SUBMITTED
        assert not [
            r for _, r in store.query("actions")
            if r["action_type"] == ActionType.SUBMIT_APPEAL.value
        ]

    def test_both_signatures_submit_once(self, pipeline, store):
        case = _to_submitted(pipeline)
        assert case.status is CaseStatus.SUBMITTED
        assert case.submitted_at is not None
        assert case.response_deadline is not None

        submissions = [
            r for _, r in store.query("actions")
            if r["action_type"] == ActionType.SUBMIT_APPEAL.value
        ]
        assert len(submissions) == 1
        assert submissions[0]["result"]["confirmation"].startswith("NBH-ACK-")

    def test_submitting_twice_transmits_once(self, pipeline, store):
        _to_submitted(pipeline)
        pipeline.try_submit("CASE-001")
        pipeline.try_submit("CASE-001")

        submissions = [
            r for _, r in store.query("actions")
            if r["action_type"] == ActionType.SUBMIT_APPEAL.value
        ]
        assert len(submissions) == 1


class TestNothingRunsInBetween:
    def test_a_submitted_case_is_not_overdue_before_its_deadline(self, pipeline):
        case = _to_submitted(pipeline)
        assert case.is_overdue is False
        assert pipeline.fleet.cases.find_overdue() == []

    def test_a_cold_worker_finds_the_overdue_case(self, pipeline, store):
        """The scenario the whole architecture exists for.

        One process submits and disappears. Weeks later a different Pipeline,
        sharing nothing but the datastore, finds the case and continues it.
        """
        _to_submitted(pipeline)
        _age(pipeline, "CASE-001", days=1)

        cold = Pipeline(build_fleet(store=store, llm=build_offline_llm()))
        overdue = cold.fleet.cases.find_overdue()

        assert [c.case_id for c in overdue] == ["CASE-001"]


class TestClimbingTheLadder:
    def test_a_silent_payer_drives_the_case_up_the_ladder(self, pipeline, store):
        _to_submitted(pipeline)

        # First window closes. Peer-to-peer needs a clinician on a call, so the
        # correct behaviour is to stop and ask for one rather than proceed.
        _age(pipeline, "CASE-001")
        pipeline.escalate_overdue()
        case = pipeline.fleet.cases.load("CASE-001")
        assert case.status is CaseStatus.NEEDS_HUMAN_REVIEW

    def test_the_ladder_advances_through_every_rung(self, pipeline, store):
        """Four rungs, driven only by deadlines passing."""
        _to_submitted(pipeline)
        levels = [pipeline.fleet.cases.load("CASE-001").appeal_level]

        for _ in range(4):
            case = pipeline.fleet.cases.load("CASE-001")
            if case.status is CaseStatus.NEEDS_HUMAN_REVIEW:
                # A person handles the peer-to-peer call and puts the case back
                # in flight. That hand-off is the design, not a workaround.
                pipeline.fleet.cases.mutate(
                    "CASE-001",
                    lambda c: (
                        setattr(c, "appeal_level", AppealLevel.PEER_TO_PEER),
                        c.transition(CaseStatus.SUBMITTED, actor="clerk"),
                    ),
                )
            _age(pipeline, "CASE-001")
            pipeline.escalate_overdue()
            levels.append(pipeline.fleet.cases.load("CASE-001").appeal_level)

        assert AppealLevel.SECOND_LEVEL in levels
        assert AppealLevel.EXTERNAL_REVIEW in levels

    def test_each_escalation_is_recorded_once(self, pipeline, store):
        _to_submitted(pipeline)
        pipeline.fleet.cases.mutate(
            "CASE-001", lambda c: setattr(c, "appeal_level", AppealLevel.PEER_TO_PEER)
        )
        _age(pipeline, "CASE-001")

        # The scheduler fires twice for the same overdue case, which it will.
        pipeline.escalate_overdue()
        before = pipeline.fleet.cases.load("CASE-001").escalation_count
        pipeline.escalate_overdue()
        after = pipeline.fleet.cases.load("CASE-001").escalation_count

        assert before == 1
        assert after == before, "the same escalation was performed twice"

    def test_a_case_still_in_its_window_is_left_alone(self, pipeline):
        _to_submitted(pipeline)
        pipeline.escalate_overdue()
        case = pipeline.fleet.cases.load("CASE-001")
        assert case.status is CaseStatus.SUBMITTED
        assert case.escalation_count == 0


class TestDemoAcceleration:
    def test_compressed_windows_are_shorter_but_real(self, pipeline, monkeypatch):
        """Demo mode compresses a day to a second. Disclosed, not hidden."""
        from core.config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("OVERTURN_DEMO_TIME_ACCELERATION", "true")
        monkeypatch.setenv("OVERTURN_DEMO_SECONDS_PER_DAY", "0.5")

        fresh = Pipeline(build_fleet(store=MemoryStore(), llm=build_offline_llm()))
        case = _to_submitted(fresh)

        elapsed = (case.response_deadline - case.submitted_at).total_seconds()
        assert elapsed == pytest.approx(30 * 0.5, abs=1)
        get_settings.cache_clear()


class TestPayerResponses:
    def test_a_recorded_response_is_kept_on_the_case(self, pipeline):
        _to_submitted(pipeline)
        pipeline.fleet.cases.mutate(
            "CASE-001",
            lambda c: c.payer_responses.append(
                PayerResponse(outcome="upheld", rationale="Criteria still not met.")
            ),
        )
        case = pipeline.fleet.cases.load("CASE-001")
        assert case.payer_responses[0].outcome == "upheld"
