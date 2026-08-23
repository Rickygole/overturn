"""OpenTelemetry setup and the span helpers every agent uses.

The point of instrumenting this system is not dashboards. It is that a judge,
or an auditor, or a billing clerk's manager, can open one trace and see the
entire reasoning chain for a case: which agent ran, in what order, what it
decided, and how many times Verification sent a draft back.

Exports to Cloud Trace in cloud mode and to the console in local mode, so the
same spans are visible either way.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)

from core.config import get_settings

logger = logging.getLogger(__name__)

_INITIALISED = False
SERVICE_NAME = "overturn"


def init_telemetry(component: str = "overturn") -> None:
    """Install the tracer provider once per process."""
    global _INITIALISED
    if _INITIALISED:
        return

    settings = get_settings()
    resource = Resource.create(
        {
            "service.name": SERVICE_NAME,
            "service.namespace": "overturn",
            "service.version": "0.1.0",
            "overturn.component": component,
            "cloud.provider": "gcp",
            "cloud.account.id": settings.project_id,
        }
    )
    provider = TracerProvider(resource=resource)

    if settings.runtime_mode == "cloud":
        try:
            from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

            provider.add_span_processor(
                BatchSpanProcessor(CloudTraceSpanExporter(project_id=settings.project_id))
            )
        except Exception as exc:  # pragma: no cover - depends on cloud credentials
            logger.warning("Cloud Trace exporter unavailable, falling back to console: %s", exc)
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    else:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    _INITIALISED = True


def get_tracer(name: str = SERVICE_NAME) -> trace.Tracer:
    if not _INITIALISED:
        init_telemetry()
    return trace.get_tracer(name)


@contextmanager
def agent_span(
    agent: str,
    case_id: str,
    operation: str,
    attempt: int = 1,
    **attributes: Any,
) -> Iterator[trace.Span]:
    """Wrap one agent invocation in a span.

    ``attempt`` is an attribute rather than part of the span name so that the
    three drafting attempts of a rejected letter nest under one parent and read
    as a retry loop, which is exactly what the trace should show.
    """
    tracer = get_tracer()
    with tracer.start_as_current_span(f"{agent}.{operation}") as span:
        span.set_attribute("overturn.agent", agent)
        span.set_attribute("overturn.case_id", case_id)
        span.set_attribute("overturn.operation", operation)
        span.set_attribute("overturn.attempt", attempt)
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(f"overturn.{key}", value)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
            raise


def current_trace_ids() -> tuple[str | None, str | None]:
    """Return ``(trace_id, span_id)`` as hex, for stamping onto audit events."""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if not ctx.is_valid:
        return None, None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
