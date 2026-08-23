"""Idempotency records for actions with effects outside this system.

Pub/Sub delivers at least once. A handler will be invoked twice. This record is
the thing that stops the second invocation from filing a second appeal.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from core.schemas.base import OverturnModel, utcnow
from core.schemas.enums import ActionType


class ActionRecord(OverturnModel):
    """A claim on an action, written before the action is performed.

    The document id is ``{case_id}:{action_type}:{attempt}`` and the write is a
    create-only transaction. If the create fails, someone already did this work
    and the stored ``result`` is returned instead of re-executing.
    """

    action_key: str = Field(description="case_id:action_type:attempt")
    case_id: str
    action_type: ActionType
    attempt: int = Field(default=1, ge=1)

    claimed_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None

    status: str = Field(default="claimed", description="'claimed', 'completed', or 'failed'.")
    payload_sha256: str | None = Field(
        default=None,
        description="Hash of the action input. A mismatch on replay means the caller "
        "changed the payload under the same key, which is a bug worth surfacing.",
    )
    result: dict[str, Any] | None = Field(
        default=None, description="What the action returned, replayed on duplicate delivery."
    )
    error: str | None = None

    delivery_count: int = Field(
        default=1, description="How many times this action was attempted. Proof the guard fired."
    )

    @staticmethod
    def make_key(case_id: str, action_type: ActionType, attempt: int = 1) -> str:
        return f"{case_id}:{action_type.value}:{attempt}"
