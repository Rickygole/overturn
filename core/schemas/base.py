"""Base model shared by every Overturn contract."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict


def utcnow() -> datetime:
    """Timezone-aware current time. Every timestamp in the system uses this."""
    return datetime.now(UTC)


class OverturnModel(BaseModel):
    """Strict base model.

    ``extra="forbid"`` is the point of this class. When one agent hands a payload
    to the next, an unexpected key means a contract drifted, and we want that to
    fail loudly at the boundary rather than silently downstream.
    """

    model_config = ConfigDict(
        extra="forbid",
        use_enum_values=False,
        validate_assignment=True,
        str_strip_whitespace=True,
    )

    def to_firestore(self) -> dict:
        """Serialise for Firestore: enums to strings, datetimes preserved."""
        return self.model_dump(mode="json", exclude_none=True)
