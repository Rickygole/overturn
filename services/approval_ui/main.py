"""Entry point for Cloud Run.

Kept separate from :mod:`services.approval_ui.app` so that importing the factory
does not build a datastore client as a side effect of the import.
"""

from __future__ import annotations

import os

from services.approval_ui.app import create_app

app = create_app()


if __name__ == "__main__":  # pragma: no cover - process entry point
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
