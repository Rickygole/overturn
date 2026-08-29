# syntax=docker/dockerfile:1
#
# One image, three Cloud Run surfaces (approval UI, ingest handler, scheduler
# job). They share every dependency and most of the code, so three near-identical
# images would just be three things to keep in sync during a hackathon. Which
# process runs is picked at container start by $OVERTURN_SERVICE, not baked in
# at build time — see the CMD at the bottom.
#
# Layer ordering, deliberately:
#   1. Install `uv` from its own published image (no curl/pip bootstrap layer).
#   2. Copy only the dependency manifests and sync deps *before* the source
#      code is copied in. Application code changes constantly during a
#      hackathon; the dependency set does not. This keeps the expensive layer
#      (resolving and installing ~20 Google Cloud + ADK packages) cached across
#      rebuilds that only touch agents/core/services.
#   3. Copy the source and sync the project itself as a second, cheap step.
#   4. Runtime stage starts fresh from python:3.12-slim with nothing but the
#      built virtualenv and the source copied over — no uv binary, no build
#      tools, no package cache left behind.

FROM python:3.12-slim AS builder

# `latest` rather than a pinned tag deliberately: this build runs rarely (once
# credits land, then occasionally after), and a pinned uv version from whenever
# this file was written is more likely to have gone stale than to have broken
# anything. uv.lock is what actually pins the dependency versions that matter.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# Dependencies first, pinned exactly to the lock file, dev tools excluded.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Now the rest. README/LICENSE/NOTICE are pulled in because pyproject.toml
# points hatchling at them for the project's own metadata build.
COPY core ./core
COPY agents ./agents
COPY services ./services
COPY data ./data
# The public site is served by the approval process itself so the product has a
# single address. Same files GitHub Pages publishes, so that stays an exact
# mirror rather than a second copy to keep in step. Only the web assets are
# copied; the markdown beside them is for humans and has no business in here.
COPY docs/*.html docs/*.css docs/*.js docs/*.svg ./docs/
COPY README.md LICENSE NOTICE ./

RUN uv sync --frozen --no-dev

# --- Runtime stage -----------------------------------------------------------
FROM python:3.12-slim

RUN groupadd --system overturn && \
    useradd --system --gid overturn --home-dir /app --no-create-home overturn

WORKDIR /app

COPY --from=builder --chown=overturn:overturn /app /app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OVERTURN_SERVICE=approval

USER overturn

EXPOSE 8080

# $OVERTURN_SERVICE picks the ASGI app; $PORT is injected by Cloud Run.
# Kept as a case statement rather than three CMDs so the mapping from short
# name -> module path lives in exactly one place.
CMD ["/bin/sh", "-c", "case \"$OVERTURN_SERVICE\" in \
  approval)  MODULE=services.approval_ui.main:app ;; \
  ingest)    MODULE=services.ingest_handler.main:app ;; \
  scheduler) MODULE=services.scheduler_job.main:app ;; \
  *) echo \"Unknown OVERTURN_SERVICE: '$OVERTURN_SERVICE' (expected approval|ingest|scheduler)\" >&2; exit 1 ;; \
esac; exec python -m uvicorn \"$MODULE\" --host 0.0.0.0 --port \"${PORT:-8080}\""]
