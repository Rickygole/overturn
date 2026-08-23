"""Entry point that doubles as a Cloud Run service and a Cloud Run job.

As a **service**, Cloud Scheduler POSTs ``/tick`` on a fixed interval; deploy
this module under an ASGI server (``uvicorn services.scheduler_job.main:app``)
and it serves that route.

As a **job**, the same image is invoked with no HTTP server at all
(``python -m services.scheduler_job.main``): it runs exactly one sweep, prints
the summary as JSON, and exits. Keeping both entry points on one image, calling
the same :func:`~services.scheduler_job.app.run_tick`, means there is only one
place the sweep logic can drift from what actually ran.
"""

from __future__ import annotations

from core.telemetry import init_telemetry
from services.scheduler_job.app import create_app, run_tick

init_telemetry("scheduler_job")
app = create_app()


if __name__ == "__main__":  # pragma: no cover - process entry point
    summary = run_tick(app.state.pipeline)
    print(summary.model_dump_json(indent=2))
