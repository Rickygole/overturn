"""Entry point for Cloud Run.

Kept separate from :mod:`services.ingest_handler.app` so that importing the
factory does not build a datastore client as a side effect of the import.
"""

from __future__ import annotations

import os

from core.telemetry import init_telemetry
from services.ingest_handler.app import create_app

init_telemetry("ingest_handler")
app = create_app()


if __name__ == "__main__":  # pragma: no cover - process entry point
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
