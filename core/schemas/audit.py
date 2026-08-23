"""Append-only audit events.

Nothing in this collection is ever updated or deleted. Every agent invocation
writes one of these, whether it succeeded, failed, or refused to act.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from core.schemas.base import OverturnModel, utcnow
from core.schemas.enums import AgentName


class AuditEvent(OverturnModel):
    """One agent invocation, recorded for a human who was not there.

    Inputs are hashed rather than stored: the chart and the denial letter contain
    patient information, and the audit log is the one collection with the
    broadest read access. The hash is enough to prove what was fed in.
    """

    event_id: str
    case_id: str
    at: datetime = Field(default_factory=utcnow)

    agent: AgentName
    agent_version: str = Field(default="0.1.0")
    operation: str = Field(description="What the agent was asked to do.")

    input_sha256: str = Field(description="Hash of the serialised input payload.")
    input_summary: str | None = Field(
        default=None, description="Short non-identifying description of the input."
    )
    output: dict[str, Any] | None = Field(
        default=None, description="Structured agent output, minus free-text patient detail."
    )

    decision: str = Field(description="The call the agent made, in one line.")
    succeeded: bool = True
    error: str | None = None

    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float | None = None

    trace_id: str | None = Field(default=None, description="Cloud Trace id for this invocation.")
    span_id: str | None = None
    attempt: int = Field(default=1, description="Retry counter, so loops are visible in the log.")
