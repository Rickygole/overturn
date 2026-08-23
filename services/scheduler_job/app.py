"""HTTP surface for the scheduled lifecycle sweep.

Cloud Scheduler calls ``POST /tick`` on a fixed interval; this module's only
job is to run :meth:`Pipeline.escalate_overdue` and shape the result into
something worth reading. All of the actual multi-week logic — which cases are
overdue, what happens to each one, and making sure two overlapping sweeps do
not escalate the same case twice — lives in the orchestrator and the
idempotency guard behind it. The handler adds nothing on top of that: no extra
locking, no retry-on-conflict, nothing that could fight the guard it is built
on. See ``agents/orchestrator/pipeline.py`` and ``core/idempotency.py``.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from agents.orchestrator.deps import build_fleet
from agents.orchestrator.pipeline import Pipeline
from core.config import Settings, get_settings
from core.llm import LlmClient
from core.schemas.base import OverturnModel
from core.schemas.enums import AppealLevel, CaseStatus
from core.store import DocumentStore


class EscalationRecord(OverturnModel):
    """One case the sweep looked at, before and after."""

    case_id: str
    from_level: AppealLevel
    to_level: AppealLevel
    status: CaseStatus


class TickSummary(OverturnModel):
    """What one sweep did. Returned to Cloud Scheduler and printed by the job."""

    cases_examined: int
    cases_escalated: int
    cases_needing_human: int
    escalations: list[EscalationRecord]


def run_tick(pipeline: Pipeline, limit: int = 50) -> TickSummary:
    """Run one sweep and shape the result.

    ``from_level`` has to be read before the sweep runs: a case Lifecycle sends
    to ``needs_human_review`` (the peer-to-peer rung, which needs a clinician on
    a call) never changes level at all, so the only way to report "still at
    peer-to-peer, now waiting on a person" is to have looked before
    ``escalate_overdue`` touched anything.
    """
    overdue = pipeline.fleet.cases.find_overdue(limit)
    before_level = {case.case_id: case.appeal_level for case in overdue}

    updated = pipeline.escalate_overdue(limit)

    escalations = [
        EscalationRecord(
            case_id=case.case_id,
            from_level=before_level.get(case.case_id, case.appeal_level),
            to_level=case.appeal_level,
            status=case.status,
        )
        for case in updated
    ]
    return TickSummary(
        cases_examined=len(updated),
        cases_escalated=sum(1 for c in updated if c.status is CaseStatus.ESCALATED),
        cases_needing_human=sum(1 for c in updated if c.status is CaseStatus.NEEDS_HUMAN_REVIEW),
        escalations=escalations,
    )


def create_app(
    store: DocumentStore | None = None,
    llm: LlmClient | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    """Build the application. Pass a store (and an offline llm) to test it."""
    # /docs and /redoc fetch Swagger from a CDN; Cloud Scheduler is the only
    # caller this service will ever have.
    app = FastAPI(title="Overturn — scheduler", docs_url=None, redoc_url=None)

    settings = settings or get_settings()
    pipeline = Pipeline(build_fleet(store=store, llm=llm, settings=settings))
    app.state.pipeline = pipeline

    @app.post("/tick", response_model=TickSummary)
    def tick() -> TickSummary:
        return run_tick(pipeline)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        # No datastore touch: a Firestore hiccup should not get the revision
        # killed for a check that was never asking about Firestore.
        return JSONResponse({"status": "ok", "service": "scheduler_job"})

    return app
