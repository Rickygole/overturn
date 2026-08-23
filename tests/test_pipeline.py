"""End-to-end tests over the whole fleet.

Every other test file checks a mechanism. This one checks the product: given a
denial letter and a chart, does the system reach the right conclusion, and does
it refuse to reach a wrong one.

These run offline against the scripted backend, so they are deterministic and
free, and they exercise the real orchestrator, the real retry loop, the real
idempotency guard, the real gateway and the real validation. Only the generative
calls are answered locally.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.intake.documents import SourceDocument
from agents.offline.handlers import build_offline_llm
from agents.orchestrator.deps import build_fleet
from agents.orchestrator.pipeline import Pipeline
from core.audit import read_case_trail
from core.schemas.enums import ActionType, AgentName, CaseStatus, CriterionVerdictValue
from core.store import MemoryStore

DENIALS = Path(__file__).resolve().parents[1] / "data" / "denials"


def _document(case_id: str) -> SourceDocument:
    return SourceDocument(
        uri=f"gs://overturn-intake/{case_id}.txt",
        data=(DENIALS / f"{case_id}.txt").read_bytes(),
        mime_type="text/plain",
    )


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def pipeline(store: MemoryStore) -> Pipeline:
    return Pipeline(build_fleet(store=store, llm=build_offline_llm()))


def run(pipeline: Pipeline, case_id: str):
    return pipeline.ingest(_document(case_id), case_id=case_id)


class TestCleanCase:
    """CASE-001: the chart plainly documents everything the policy asks for."""

    def test_reaches_the_human_gate(self, pipeline):
        case = run(pipeline, "CASE-001")
        assert case.status is CaseStatus.AWAITING_APPROVAL

    def test_retrieves_the_governing_policy(self, pipeline):
        case = run(pipeline, "CASE-001")
        assert {s.policy_id for s in case.retrieval.sections} == {"NBH-ENDO-031"}

    def test_every_satisfied_criterion_cites_a_real_chart_location(self, pipeline):
        case = run(pipeline, "CASE-001")
        from agents.mapping.charts import load_chart

        locators = load_chart("CASE-001").locators()
        for verdict in case.criteria.verdicts:
            for evidence in verdict.evidence:
                assert evidence.locator in locators, (
                    f"{verdict.criterion_id} cites {evidence.locator}, "
                    f"which is not in the chart"
                )

    def test_the_draft_cites_only_real_policy_sections(self, pipeline):
        case = run(pipeline, "CASE-001")
        known = case.retrieval.section_ids()
        assert case.latest_draft.cited_ids() <= known

    def test_verification_passed_on_the_first_attempt(self, pipeline):
        case = run(pipeline, "CASE-001")
        assert len(case.drafts) == 1
        assert case.latest_verification.passed is True

    def test_nothing_is_submitted_without_signatures(self, pipeline, store):
        """The whole point of the gate."""
        run(pipeline, "CASE-001")
        submissions = [
            row
            for _, row in store.query("actions")
            if row["action_type"] == ActionType.SUBMIT_APPEAL.value
        ]
        assert submissions == []


class TestInjectionCase:
    """CASE-002: the letter carries an instruction payload."""

    def test_the_document_is_quarantined(self, pipeline):
        case = run(pipeline, "CASE-002")
        assert case.status is CaseStatus.QUARANTINED

    def test_intake_never_runs(self, pipeline, store):
        """The pipeline must halt before anything extracts from the document."""
        run(pipeline, "CASE-002")
        agents_that_ran = {e.agent for e in read_case_trail(store, "CASE-002")}
        assert AgentName.SENTINEL in agents_that_ran
        assert AgentName.INTAKE not in agents_that_ran
        assert AgentName.DRAFTING not in agents_that_ran

    def test_no_denial_is_extracted(self, pipeline):
        case = run(pipeline, "CASE-002")
        assert case.denial is None
        assert case.drafts == []

    def test_the_quarantine_is_recorded_for_review(self, pipeline, store):
        case = run(pipeline, "CASE-002")
        record = store.get("quarantine", case.source_sha256)
        assert record is not None
        assert len(record["findings"]) >= 3

    def test_a_human_is_told(self, pipeline, store):
        run(pipeline, "CASE-002")
        notifications = [
            row
            for _, row in store.query("actions")
            if row["action_type"] == ActionType.NOTIFY_HUMAN.value
        ]
        assert len(notifications) == 1


class TestUndocumentedCriterion:
    """CASE-003: most criteria met, one genuinely not documented.

    The chart notes a pacemaker and never says whether it is MRI-conditional.
    The correct behaviour is to mark that criterion insufficient and write the
    appeal without it — not to argue around it.
    """

    def test_the_unresolved_criterion_is_not_claimed_as_satisfied(self, pipeline):
        case = run(pipeline, "CASE-003")
        contraindication = next(
            v for v in case.criteria.verdicts if v.criterion_id == "NBH-CARD-014-3.5"
        )
        assert contraindication.verdict is CriterionVerdictValue.INSUFFICIENT_DOCUMENTATION

    def test_drafting_never_sees_the_unmet_criterion(self, pipeline):
        case = run(pipeline, "CASE-003")
        appealable = {v.criterion_id for v in case.criteria.appealable_verdicts()}
        assert "NBH-CARD-014-3.5" not in appealable

    def test_the_letter_does_not_cite_it(self, pipeline):
        case = run(pipeline, "CASE-003")
        assert "NBH-CARD-014-3.5" not in case.latest_draft.cited_ids()

    def test_the_appeal_still_goes_forward_on_what_is_documented(self, pipeline):
        case = run(pipeline, "CASE-003")
        assert case.status is CaseStatus.AWAITING_APPROVAL
        assert len(case.latest_draft.citations) >= 4


class TestFaultInjection:
    """Verification catching a fabricated citation, on purpose."""

    def test_a_transient_fabrication_is_caught_and_the_retry_is_clean(
        self, pipeline, monkeypatch
    ):
        monkeypatch.setenv("OVERTURN_SABOTAGE_DRAFTING", "first")
        case = run(pipeline, "CASE-003")

        assert len(case.drafts) == 2, "expected one rejection and one clean retry"
        assert case.verifications[0].passed is False
        assert "NBH-CARD-014-9.9" in case.verifications[0].citations_nonexistent
        assert case.verifications[1].passed is True
        assert "NBH-CARD-014-9.9" not in case.latest_draft.cited_ids()
        assert case.status is CaseStatus.AWAITING_APPROVAL

    def test_a_persistent_fabrication_exhausts_the_budget_and_stops(
        self, pipeline, monkeypatch
    ):
        monkeypatch.setenv("OVERTURN_SABOTAGE_DRAFTING", "always")
        case = run(pipeline, "CASE-003")

        assert len(case.drafts) == 3
        assert all(v.passed is False for v in case.verifications)
        assert case.status is CaseStatus.NEEDS_HUMAN_REVIEW
        assert case.needs_human_reason is not None

    def test_nothing_is_ever_submitted_on_a_rejected_draft(
        self, pipeline, store, monkeypatch
    ):
        monkeypatch.setenv("OVERTURN_SABOTAGE_DRAFTING", "always")
        run(pipeline, "CASE-003")
        submissions = [
            row
            for _, row in store.query("actions")
            if row["action_type"] == ActionType.SUBMIT_APPEAL.value
        ]
        assert submissions == []

    def test_the_rejection_reason_is_specific_enough_to_act_on(
        self, pipeline, monkeypatch
    ):
        monkeypatch.setenv("OVERTURN_SABOTAGE_DRAFTING", "first")
        case = run(pipeline, "CASE-003")
        instructions = case.verifications[0].revision_instructions()
        assert any("NBH-CARD-014-9.9" in i for i in instructions)
        assert any("does not exist" in i for i in instructions)


class TestRedelivery:
    """Pub/Sub delivers at least once. The pipeline is invoked twice."""

    def test_a_redelivered_document_does_not_produce_a_second_case(
        self, pipeline, store
    ):
        first = run(pipeline, "CASE-001")
        second = run(pipeline, "CASE-001")

        assert first.case_id == second.case_id
        assert store.count("cases") == 1

    def test_a_redelivery_does_not_redo_the_work(self, pipeline, store):
        run(pipeline, "CASE-001")
        drafts_after_first = len(pipeline.fleet.cases.load("CASE-001").drafts)
        run(pipeline, "CASE-001")

        assert len(pipeline.fleet.cases.load("CASE-001").drafts) == drafts_after_first

    def test_a_redelivered_quarantine_does_not_notify_twice(self, pipeline, store):
        run(pipeline, "CASE-002")
        run(pipeline, "CASE-002")
        notifications = [
            row
            for _, row in store.query("actions")
            if row["action_type"] == ActionType.NOTIFY_HUMAN.value
        ]
        assert len(notifications) == 1


class TestAuditTrail:
    def test_every_agent_that_ran_left_an_event(self, pipeline, store):
        run(pipeline, "CASE-001")
        agents = [e.agent for e in read_case_trail(store, "CASE-001")]
        for expected in (
            AgentName.SENTINEL,
            AgentName.INTAKE,
            AgentName.RETRIEVAL,
            AgentName.MAPPING,
            AgentName.DRAFTING,
            AgentName.VERIFICATION,
        ):
            assert expected in agents, f"{expected.value} left no audit event"

    def test_events_are_ordered_and_carry_a_decision(self, pipeline, store):
        run(pipeline, "CASE-001")
        events = read_case_trail(store, "CASE-001")
        assert events == sorted(events, key=lambda e: e.at)
        for event in events:
            assert event.decision
            assert event.decision != "no decision recorded"

    def test_the_trail_never_contains_the_patient_name(self, pipeline, store):
        """The audit log is the broadest-read collection in the system."""
        run(pipeline, "CASE-001")
        for event in read_case_trail(store, "CASE-001"):
            blob = f"{event.decision} {event.input_summary or ''}"
            assert "Jeromy" not in blob
            assert "Upton" not in blob


class TestOrchestratorHasNoBrain:
    """The orchestrator holds the broadest grant in the fleet.

    What earns it is that it makes no model call and consumes no untrusted
    input — it is deterministic Python routing typed contracts. If that ever
    stops being true, the grant needs revisiting, so it is a test rather than
    a claim in a comment.
    """

    def test_the_orchestrator_never_calls_a_model(self, pipeline, store):
        for case_id in ("CASE-001", "CASE-002", "CASE-003"):
            run(pipeline, case_id)

        for case_id in ("CASE-001", "CASE-002", "CASE-003"):
            for event in read_case_trail(store, case_id):
                if event.agent is AgentName.ORCHESTRATOR:
                    assert event.model is None

        backend = pipeline.fleet.llm.backend
        assert all(call.agent != "orchestrator" for call in backend.calls)
