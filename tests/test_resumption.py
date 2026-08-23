"""A worker dies mid-pipeline. Redelivery has to pick the case back up.

The bug this guards against: nine of eighteen ``CaseStatus`` values are
reachable mid-pipeline, invisible to ``find_overdue``, in no approval queue and
no needs-human queue, and -- before this fix -- explicitly untouched by
``Pipeline.ingest`` on redelivery (``if not created and case.status is not
CaseStatus.RECEIVED: return case``). A case whose worker was evicted while it
sat in one of those statuses was a silent graveyard: its outputs so far were
committed, as the README promises, but nothing ever read them again.

These tests exercise the fix from the outside, the same way the real failure
would be observed: kill a stage, redeliver the same document, and check what
comes back.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta
from pathlib import Path

import pytest

from agents.intake.documents import SourceDocument
from agents.offline.handlers import build_offline_llm
from agents.orchestrator.deps import build_fleet
from agents.orchestrator.pipeline import (
    AWAITING_HUMAN_STATUSES,
    IN_FLIGHT_STATUSES,
    MAX_RESUME_ATTEMPTS,
    MAX_TRANSMISSION_ATTEMPTS,
    Pipeline,
)
from core.schemas.base import utcnow
from core.schemas.case import AWAITING_PAYER_STATUSES, CaseRecord
from core.schemas.enums import TERMINAL_STATUSES, ActionType, CaseStatus
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


class TestStatusCoverage:
    """The invariant item 4 asks for: every status is exactly one thing.

    A status is terminal, or the payer owes a response, or a person is already
    the next actor, or the pipeline itself owes the next move (and is
    therefore something ``find_stalled`` must be able to see). If someone adds
    a tenth ``CaseStatus`` and forgets to place it, this fails instead of
    quietly reproducing the bug.
    """

    def test_every_status_is_in_exactly_one_group(self):
        groups = {
            "terminal": TERMINAL_STATUSES,
            "awaiting_payer": AWAITING_PAYER_STATUSES,
            "awaiting_human": AWAITING_HUMAN_STATUSES,
            "in_flight": IN_FLIGHT_STATUSES,
        }

        seen: dict[CaseStatus, str] = {}
        for name, group in groups.items():
            for status in group:
                assert status not in seen, (
                    f"{status.value} is claimed by both {seen[status]!r} and {name!r}"
                )
                seen[status] = name

        missing = set(CaseStatus) - seen.keys()
        assert not missing, f"uncategorised status(es): {[s.value for s in missing]}"


class TestResumeMidMapping:
    """The exact scenario in the bug report: evicted during Mapping."""

    def test_redelivery_completes_without_rerunning_intake(self, pipeline, store, monkeypatch):
        case_id = "CASE-001"
        document = _document(case_id)

        real_mapping_run = pipeline.fleet.mapping.run
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("worker evicted mid-mapping")
            return real_mapping_run(*args, **kwargs)

        monkeypatch.setattr(pipeline.fleet.mapping, "run", flaky)

        with pytest.raises(RuntimeError):
            pipeline.ingest(document, case_id=case_id)

        # Exactly what the bug report describes: parked at `mapping`, with
        # screening, denial and retrieval already committed.
        crashed = pipeline.fleet.cases.load(case_id)
        assert crashed.status is CaseStatus.MAPPING
        assert crashed.screening is not None
        assert crashed.denial is not None
        assert crashed.retrieval is not None
        assert crashed.criteria is None

        result = pipeline.ingest(document, case_id=case_id)

        assert result.status is CaseStatus.AWAITING_APPROVAL
        assert calls["n"] == 2

        backend = pipeline.fleet.llm.backend
        intake_calls = [c for c in backend.calls if c.agent == "intake"]
        assert len(intake_calls) == 1, "Intake reran on redelivery and burned a second model call"


class TestResumeMidDrafting:
    """A crash between a rejected draft and the next attempt must not restart at attempt one."""

    def test_redelivery_does_not_duplicate_or_restart_an_attempt(
        self, pipeline, store, monkeypatch
    ):
        case_id = "CASE-003"
        document = _document(case_id)
        monkeypatch.setenv("OVERTURN_SABOTAGE_DRAFTING", "first")

        real_drafting_run = pipeline.fleet.drafting.run
        calls = {"n": 0}
        attempts_seen: list[int] = []

        def flaky(case_id_arg, brief, attempt=1):
            calls["n"] += 1
            attempts_seen.append(attempt)
            if calls["n"] == 2:
                raise RuntimeError("worker evicted mid-drafting")
            return real_drafting_run(case_id_arg, brief, attempt=attempt)

        monkeypatch.setattr(pipeline.fleet.drafting, "run", flaky)

        with pytest.raises(RuntimeError):
            pipeline.ingest(document, case_id=case_id)

        # Attempt 1 was written and rejected (the sabotage flag on "first"
        # guarantees that); the crash happened starting attempt 2.
        crashed = pipeline.fleet.cases.load(case_id)
        assert crashed.status is CaseStatus.DRAFTING
        assert len(crashed.drafts) == 1
        assert crashed.drafts[0].attempt == 1
        assert len(crashed.verifications) == 1
        assert crashed.verifications[0].passed is False

        result = pipeline.ingest(document, case_id=case_id)

        assert result.status is CaseStatus.AWAITING_APPROVAL
        assert [d.attempt for d in result.drafts] == [1, 2], (
            "resuming from `drafting` must continue at the next attempt, not append a duplicate"
        )
        # Attempt 1 was written exactly once, ever -- the resumed run picked up
        # at attempt 2 rather than restarting the loop from scratch.
        assert attempts_seen == [1, 2, 2]


class TestPoisonMessage:
    """A case that crashes the same way every time must stop, not loop forever."""

    def test_repeated_failure_ends_at_needs_human_review(self, pipeline, store, monkeypatch):
        case_id = "CASE-001"
        document = _document(case_id)

        def always_raises(*args, **kwargs):
            raise RuntimeError("this chart will never load, ever")

        monkeypatch.setattr(pipeline.fleet.mapping, "run", always_raises)

        for _ in range(MAX_RESUME_ATTEMPTS - 1):
            with pytest.raises(RuntimeError):
                pipeline.ingest(document, case_id=case_id)

        result = pipeline.ingest(document, case_id=case_id)

        assert result.status is CaseStatus.NEEDS_HUMAN_REVIEW
        assert result.failure_count == MAX_RESUME_ATTEMPTS
        assert result.needs_human_reason is not None
        assert "mapping" in result.needs_human_reason

        # `needs_human_review` is not in the resume map. A further redelivery
        # must be a no-op, not another attempt (which would raise again) and
        # not silent progress past a human who hasn't looked yet.
        again = pipeline.ingest(document, case_id=case_id)
        assert again.status is CaseStatus.NEEDS_HUMAN_REVIEW
        assert again.failure_count == MAX_RESUME_ATTEMPTS


class TestFindStalled:
    """Nothing redelivers a message whose subscription already acked. Something has to sweep."""

    def _seed(self, pipeline: Pipeline, case_id: str, status: CaseStatus, updated_at) -> None:
        pipeline.fleet.cases.create(
            CaseRecord(
                case_id=case_id,
                source_document_uri=f"gs://overturn-intake/{case_id}.txt",
                status=status,
                updated_at=updated_at,
            )
        )

    def test_finds_a_stuck_case_and_ignores_a_healthy_one(self, pipeline, store):
        old = utcnow() - timedelta(hours=2)
        self._seed(pipeline, "CASE-STUCK", CaseStatus.MAPPING, old)
        # Old by the clock too, to prove it is status membership that excludes
        # this one, not merely that it looks recent.
        self._seed(pipeline, "CASE-HEALTHY", CaseStatus.SUBMITTED, old)

        stalled_ids = {c.case_id for c in pipeline.find_stalled(older_than_minutes=60)}

        assert "CASE-STUCK" in stalled_ids
        assert "CASE-HEALTHY" not in stalled_ids

    def test_ignores_an_in_flight_case_that_is_merely_recent(self, pipeline, store):
        self._seed(pipeline, "CASE-FRESH", CaseStatus.DRAFTING, utcnow())

        stalled_ids = {c.case_id for c in pipeline.find_stalled(older_than_minutes=60)}

        assert "CASE-FRESH" not in stalled_ids


class TestRetryTransmission:
    """A network blip on ``try_submit`` must not weld every exit shut.

    ``escalation_count`` only advances through ``_escalate_one``, which is
    only reachable via ``find_overdue``, which never looks at ``approved``.
    Before ``transmission_attempts`` existed, a case bounced to
    ``needs_human_review`` by a failed send had no way to earn a new
    idempotency key: every subsequent ``try_submit`` either early-returned
    (wrong status) or replayed the same permanently-``failed`` action.
    """

    def _service(self, pipeline: Pipeline):
        from services.approval_ui.service import ApprovalService

        return ApprovalService(pipeline.fleet.store)

    def _signed_and_approved(self, pipeline: Pipeline, case_id: str = "CASE-001") -> CaseRecord:
        run_result = pipeline.ingest(_document(case_id), case_id=case_id)
        assert run_result.status is CaseStatus.AWAITING_APPROVAL

        service = self._service(pipeline)
        attempt = pipeline.fleet.cases.load(case_id).latest_draft.attempt
        service.approve(
            case_id=case_id,
            decided_by="clerk@clinic.example",
            draft_attempt=attempt,
            citations_checked=True,
            quotes_checked=True,
            assertions_checked=True,
        )
        service.cosign(
            case_id=case_id,
            clinician_name="M. Castellanos",
            credential="MD",
            attests_clinical_accuracy=True,
            draft_attempt=attempt,
        )
        return pipeline.fleet.cases.load(case_id)

    def _submit_actions(self, store: MemoryStore) -> list[dict]:
        return [
            row
            for _, row in store.query("actions")
            if row["action_type"] == ActionType.SUBMIT_APPEAL.value
        ]

    def test_a_transient_failure_recovers_on_retry(self, pipeline, store, monkeypatch):
        from services.payer_sim import PayerUnavailable

        case_id = "CASE-001"
        self._signed_and_approved(pipeline, case_id)

        monkeypatch.setenv("OVERTURN_PAYER_BEHAVIOUR", "error")
        with pytest.raises(PayerUnavailable):
            pipeline.try_submit(case_id)

        failed = pipeline.fleet.cases.load(case_id)
        assert failed.status is CaseStatus.NEEDS_HUMAN_REVIEW
        assert failed.transmission_attempts == 1

        monkeypatch.setenv("OVERTURN_PAYER_BEHAVIOUR", "accept")
        result = pipeline.retry_transmission(case_id)

        assert result.status is CaseStatus.SUBMITTED
        assert result.response_deadline is not None

        completed = [a for a in self._submit_actions(store) if a["status"] == "completed"]
        assert len(completed) == 1, "the retry should be the only transmission that succeeded"

    def test_the_retry_uses_a_different_action_attempt(self, pipeline, store, monkeypatch):
        from services.payer_sim import PayerUnavailable

        case_id = "CASE-001"
        self._signed_and_approved(pipeline, case_id)

        monkeypatch.setenv("OVERTURN_PAYER_BEHAVIOUR", "error")
        with pytest.raises(PayerUnavailable):
            pipeline.try_submit(case_id)

        monkeypatch.setenv("OVERTURN_PAYER_BEHAVIOUR", "accept")
        pipeline.retry_transmission(case_id)

        attempts = sorted(a["attempt"] for a in self._submit_actions(store))
        assert len(attempts) == 2
        assert attempts[0] != attempts[1], "the retry replayed the failed attempt's key"

        failed_action = next(a for a in self._submit_actions(store) if a["status"] == "failed")
        completed_action = next(
            a for a in self._submit_actions(store) if a["status"] == "completed"
        )
        assert failed_action["attempt"] != completed_action["attempt"]

    def test_repeated_failure_stops_at_the_cap_instead_of_looping(
        self, pipeline, store, monkeypatch
    ):
        from services.payer_sim import PayerUnavailable

        case_id = "CASE-001"
        self._signed_and_approved(pipeline, case_id)
        monkeypatch.setenv("OVERTURN_PAYER_BEHAVIOUR", "error")

        with pytest.raises(PayerUnavailable):
            pipeline.try_submit(case_id)

        for _ in range(5):
            case = pipeline.fleet.cases.load(case_id)
            if case.status is CaseStatus.NEEDS_HUMAN_REVIEW and not case.ready_to_submit:
                break
            with contextlib.suppress(PayerUnavailable):
                pipeline.retry_transmission(case_id)

        result = pipeline.fleet.cases.load(case_id)
        assert result.status is CaseStatus.NEEDS_HUMAN_REVIEW
        assert result.transmission_attempts == MAX_TRANSMISSION_ATTEMPTS
        assert str(MAX_TRANSMISSION_ATTEMPTS) in result.needs_human_reason
        assert len(result.transmission_errors) == MAX_TRANSMISSION_ATTEMPTS

        # One further call must be a no-op: no new action, no new attempt.
        again = pipeline.retry_transmission(case_id)
        assert again.status is CaseStatus.NEEDS_HUMAN_REVIEW
        assert again.transmission_attempts == MAX_TRANSMISSION_ATTEMPTS

    def test_a_case_that_is_not_ready_is_refused_rather_than_pushed_to_approved(
        self, pipeline, store
    ):
        from core.schemas.case import HumanDecision

        case_id = "CASE-STUCK-NO-COSIGN"
        stuck = CaseRecord(
            case_id=case_id,
            source_document_uri=f"gs://overturn-intake/{case_id}.txt",
            status=CaseStatus.NEEDS_HUMAN_REVIEW,
            human_decision=HumanDecision(
                decided_by="clerk@clinic.example",
                approved=True,
                citations_checked=True,
                quotes_checked=True,
                assertions_checked=True,
            ),
            needs_human_reason="transmission failed",
            transmission_attempts=1,
        )
        pipeline.fleet.cases.create(stuck)
        assert stuck.ready_to_submit is False, "missing the clinician cosign this case requires"

        result = pipeline.retry_transmission(case_id)

        assert result.status is CaseStatus.NEEDS_HUMAN_REVIEW
        assert result.transmission_attempts == 1
        assert not self._submit_actions(store)
