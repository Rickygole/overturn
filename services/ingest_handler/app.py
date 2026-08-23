"""HTTP surface that receives a Cloud Storage notification for a landed letter.

A denial letter arrives in the intake bucket; GCS publishes a notification;
Pub/Sub pushes it here as an HTTP request. The one job of this module is to
turn that push into a :class:`~agents.intake.documents.SourceDocument` and hand
it to :meth:`Pipeline.ingest`, then translate whatever happens into the status
code that tells Pub/Sub whether to redeliver.

The status codes are the point, not an afterthought:

  * success, or a case already past ``received`` — nothing more to do, ack it.
  * :class:`ActionInFlight` — another worker holds the lease; nack so Pub/Sub
    tries again once it lets go.
  * a malformed envelope, or :class:`PayloadMismatch` — retrying will never fix
    either, so ack-with-rejection (400) rather than loop forever.
  * anything else — an unexpected failure is not made better by retrying it
    verbatim, so it goes to the dead-letter topic and gets acked. The
    alternative, redelivering it, only reproduces the same failure.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from agents.intake.documents import SourceDocument
from agents.orchestrator.deps import build_fleet
from agents.orchestrator.pipeline import Pipeline
from core.config import Settings, get_settings
from core.idempotency import ActionInFlight, PayloadMismatch
from core.llm import LlmClient
from core.store import DocumentStore

logger = logging.getLogger(__name__)


class EnvelopeError(ValueError):
    """The push body could not be turned into a document. Not worth a retry."""


def create_app(
    store: DocumentStore | None = None,
    llm: LlmClient | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    """Build the application. Pass a store (and an offline llm) to test it."""
    # /docs and /redoc are switched off for the same reason approval_ui switches
    # them off: both fetch Swagger assets from a CDN, and Pub/Sub is the only
    # caller this service will ever have.
    app = FastAPI(title="Overturn — ingest handler", docs_url=None, redoc_url=None)

    settings = settings or get_settings()
    pipeline = Pipeline(build_fleet(store=store, llm=llm, settings=settings))
    app.state.pipeline = pipeline
    app.state.settings = settings

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        # No datastore touch: a Firestore hiccup should not get the revision
        # killed for a check that was never asking about Firestore.
        return JSONResponse({"status": "ok", "service": "ingest_handler"})

    @app.post("/pubsub/push")
    async def pubsub_push(request: Request) -> Response:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise EnvelopeError("push body is not a JSON object")
            document, case_id = _document_from_envelope(body)
        except (EnvelopeError, ValueError) as exc:
            logger.warning("malformed push envelope: %s", exc)
            return Response(status_code=400)

        try:
            pipeline.ingest(document, case_id=case_id)
        except ActionInFlight:
            return Response(status_code=503)
        except PayloadMismatch as exc:
            logger.error("payload mismatch for case %s: %s", case_id, exc)
            return Response(status_code=400)
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see module docstring
            logger.exception("ingest failed for case %s; sending to the dead letter", case_id)
            _publish_dead_letter(settings, case_id, body, exc)
            return Response(status_code=200)

        return Response(status_code=204)

    return app


# --------------------------------------------------------------------------- #
# Envelope parsing
# --------------------------------------------------------------------------- #


def _document_from_envelope(body: dict[str, Any]) -> tuple[SourceDocument, str | None]:
    """Pull a document out of a Pub/Sub push envelope.

    ``message.data`` is base64 of a Cloud Storage object-finalized notification
    — ``{"bucket": ..., "name": ..., ...}``. Real traffic carries nothing else,
    and the object bytes are fetched from GCS. A test envelope adds
    ``content_base64`` to the same notification so a test can supply the letter
    inline and never touch GCS at all.
    """
    message = body.get("message")
    if not isinstance(message, dict):
        raise EnvelopeError("envelope has no 'message' object")

    data_b64 = message.get("data")
    if not isinstance(data_b64, str) or not data_b64:
        raise EnvelopeError("'message.data' is missing or not a string")

    try:
        raw = base64.b64decode(data_b64, validate=True)
        notification = json.loads(raw)
    except (binascii.Error, ValueError) as exc:
        raise EnvelopeError(f"'message.data' is not base64-encoded JSON: {exc}") from exc

    if not isinstance(notification, dict):
        raise EnvelopeError("the decoded Cloud Storage notification is not a JSON object")

    bucket = notification.get("bucket")
    name = notification.get("name")
    if not isinstance(bucket, str) or not bucket or not isinstance(name, str) or not name:
        raise EnvelopeError("the notification is missing 'bucket' or 'name'")

    mime_type = notification.get("contentType") or notification.get("mime_type") or (
        "application/octet-stream"
    )

    inline = notification.get("content_base64")
    if inline is not None:
        try:
            data = base64.b64decode(inline, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise EnvelopeError(f"'content_base64' is not valid base64: {exc}") from exc
    else:
        data = _fetch_object_bytes(bucket, name)

    uri = f"gs://{bucket}/{name}"
    return SourceDocument(uri=uri, data=data, mime_type=mime_type), _derive_case_id(name)


def _derive_case_id(name: str) -> str | None:
    """Read the case id off the object name; the pipeline derives one otherwise."""
    stem = name.rstrip("/").split("/")[-1].split(".")[0]
    return stem if stem.startswith("CASE-") else None


def _fetch_object_bytes(bucket: str, name: str) -> bytes:  # pragma: no cover - needs real GCS
    """Real Cloud Storage. Every test carries its content inline instead."""
    from google.cloud import storage

    client = storage.Client()
    return client.bucket(bucket).blob(name).download_as_bytes()


def _publish_dead_letter(
    settings: Settings, case_id: str | None, body: dict[str, Any], error: Exception
) -> None:
    """Best-effort. Losing the dead letter must not turn an ack into a crash."""
    if not settings.dead_letter_topic:
        return
    try:  # pragma: no cover - needs real Pub/Sub credentials
        from google.cloud import pubsub_v1

        publisher = pubsub_v1.PublisherClient()
        topic_path = publisher.topic_path(settings.project_id, settings.dead_letter_topic)
        payload = json.dumps(
            {"case_id": case_id, "error": f"{type(error).__name__}: {error}", "envelope": body},
            default=str,
        ).encode("utf-8")
        publisher.publish(topic_path, payload)
    except Exception:
        logger.exception("failed to publish case %s to the dead-letter topic", case_id)
