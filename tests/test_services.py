"""Tests for the two Cloud Run surfaces: the Pub/Sub push handler and the
scheduled sweep.

Both run here against a shared ``MemoryStore`` and the offline scripted
backend, so nothing calls out to GCS, Pub/Sub, or a model. The questions these
ask are the ones a redelivery-prone, at-least-once world actually raises: does
a redelivered push do the work once, does a malformed one get rejected rather
than retried forever, and does a case that has already been escalated once
stay put the second time the scheduler fires.
"""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agents.intake.documents import SourceDocument
from agents.offline.handlers import build_offline_llm
from agents.orchestrator.deps import build_fleet
from agents.orchestrator.pipeline import Pipeline
from core.schemas.base import utcnow
from core.schemas.case import ClinicianCosign, HumanDecision
from core.schemas.enums import AppealLevel, CaseStatus
from core.store import MemoryStore
from services.ingest_handler.app import create_app as create_ingest_app
from services.scheduler_job.app import create_app as create_scheduler_app

DENIALS = Path(__file__).resolve().parents[1] / "data" / "denials"


# --------------------------------------------------------------------------- #
# Envelope helpers
# --------------------------------------------------------------------------- #


def _notification(case_id: str, mime_type: str = "text/plain") -> dict:
    """A Cloud Storage notification, carrying the letter inline so no test needs GCS."""
    data = (DENIALS / f"{case_id}.txt").read_bytes()
    return {
        "bucket": "overturn-intake",
        "name": f"{case_id}.txt",
        "contentType": mime_type,
        "content_base64": base64.b64encode(data).decode("ascii"),
    }


def _push_envelope(case_id: str, message_id: str = "1") -> dict:
    """The real Pub/Sub push shape, wrapping the notification above."""
    notification = _notification(case_id)
    return {
        "message": {
            "data": base64.b64encode(json.dumps(notification).encode("utf-8")).decode("ascii"),
            "messageId": message_id,
            "attributes": {"bucketId": notification["bucket"], "objectId": notification["name"]},
        },
        "subscription": "projects/overturn-local/subscriptions/intake-push",
    }


def _actions_for(store: MemoryStore, case_id: str) -> list[dict]:
    return [row for _, row in store.query("actions") if row["case_id"] == case_id]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def ingest_client(store: MemoryStore) -> TestClient:
    return TestClient(create_ingest_app(store=store, llm=build_offline_llm()))


@pytest.fixture
def scheduler_client(store: MemoryStore) -> TestClient:
    return TestClient(create_scheduler_app(store=store, llm=build_offline_llm()))


@pytest.fixture
def pipeline(store: MemoryStore) -> Pipeline:
    """Direct access to the same store, for setup the HTTP surface has no route for."""
    return Pipeline(build_fleet(store=store, llm=build_offline_llm()))


# --------------------------------------------------------------------------- #
# ingest_handler
# --------------------------------------------------------------------------- #


class TestIngestHandler:
    def test_a_valid_push_runs_the_pipeline_and_acks(self, ingest_client, store):
        response = ingest_client.post("/pubsub/push", json=_push_envelope("CASE-001"))

        assert response.status_code == 204
        assert store.count("cases") == 1
        case = store.get("cases", "CASE-001")
        assert case["status"] != CaseStatus.RECEIVED.value

    def test_redelivery_produces_one_case_and_one_set_of_actions(self, ingest_client, store):
        envelope = _push_envelope("CASE-001")

        for _ in range(3):
            response = ingest_client.post("/pubsub/push", json=envelope)
            assert response.status_code == 204

        assert store.count("cases") == 1
        # CASE-001 reaches the human gate cleanly: one notification, nothing
        # else with a side effect. Three deliveries must not multiply it.
        assert len(_actions_for(store, "CASE-001")) == 1

    def test_a_malformed_envelope_is_rejected_not_retried_forever(self, ingest_client, store):
        response = ingest_client.post("/pubsub/push", json={"message": {}})

        assert response.status_code == 400
        assert store.count("cases") == 0

    def test_an_envelope_with_no_message_at_all_is_also_rejected(self, ingest_client):
        response = ingest_client.post("/pubsub/push", json={"subscription": "x"})
        assert response.status_code == 400

    def test_a_poisoned_letter_is_quarantined_and_acked(self, ingest_client, store):
        response = ingest_client.post("/pubsub/push", json=_push_envelope("CASE-002"))

        assert response.status_code == 204
        case = store.get("cases", "CASE-002")
        assert case["status"] == CaseStatus.QUARANTINED.value

    def test_healthz(self, ingest_client):
        response = ingest_client.get("/healthz")
        assert response.status_code == 200


# --------------------------------------------------------------------------- #
# scheduler_job
# --------------------------------------------------------------------------- #


def _to_submitted_at_peer_to_peer(pipeline: Pipeline, case_id: str = "CASE-001") -> None:
    """Run a case to a submitted appeal already on the peer-to-peer rung.

    Escalating off the *first* level requires a clinician on a call, so it ends
    in ``needs_human_review`` rather than ``escalated`` — see
    ``tests/test_lifecycle.py``. Starting one rung higher is what exercises an
    actual ladder advance here.
    """
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
    pipeline.try_submit(case_id)
    pipeline.fleet.cases.mutate(
        case_id, lambda c: setattr(c, "appeal_level", AppealLevel.PEER_TO_PEER)
    )


def _age(pipeline: Pipeline, case_id: str, days: int = 1) -> None:
    """Move a case's deadline into the past. This is how weeks pass."""
    pipeline.fleet.cases.mutate(
        case_id,
        lambda c: setattr(c, "response_deadline", utcnow() - timedelta(days=days)),
    )


class TestSchedulerJob:
    def test_a_tick_with_nothing_overdue_reports_zero_and_changes_nothing(
        self, scheduler_client, store
    ):
        before = store.count("cases")
        response = scheduler_client.post("/tick")

        assert response.status_code == 200
        body = response.json()
        assert body == {
            "cases_examined": 0,
            "cases_escalated": 0,
            "cases_needing_human": 0,
            "escalations": [],
        }
        assert store.count("cases") == before

    def test_an_overdue_case_is_escalated_exactly_once(self, scheduler_client, pipeline, store):
        _to_submitted_at_peer_to_peer(pipeline, "CASE-001")
        _age(pipeline, "CASE-001")

        first = scheduler_client.post("/tick")
        assert first.status_code == 200
        body = first.json()
        assert body["cases_examined"] == 1
        assert body["cases_escalated"] == 1
        assert body["cases_needing_human"] == 0
        assert body["escalations"] == [
            {
                "case_id": "CASE-001",
                "from_level": AppealLevel.PEER_TO_PEER.value,
                "to_level": AppealLevel.SECOND_LEVEL.value,
                "status": CaseStatus.ESCALATED.value,
            }
        ]

        case_after_first = pipeline.fleet.cases.load("CASE-001")
        assert case_after_first.escalation_count == 1

        # Fires again immediately: the case's new deadline is in the future,
        # so it must not be found overdue, let alone escalated a second time.
        second = scheduler_client.post("/tick")
        assert second.status_code == 200
        second_body = second.json()
        assert second_body["cases_examined"] == 0
        assert second_body["escalations"] == []

        assert pipeline.fleet.cases.load("CASE-001").escalation_count == 1

    def test_healthz(self, scheduler_client):
        response = scheduler_client.get("/healthz")
        assert response.status_code == 200
