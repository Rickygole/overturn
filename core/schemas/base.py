"""Base model shared by every Overturn contract."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

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

    def to_firestore(self) -> dict[str, Any]:
        """Serialise for storage.

        Computed properties are stripped, recursively. They are derived state:
        writing them would bloat the document, and reading them back would fail
        validation against ``extra="forbid"``. A case record that cannot survive
        a write-then-read is useless in a system whose whole premise is picking
        a case back up weeks later, so this is enforced by a round-trip test
        over every contract in the package.
        """
        return _strip_computed(self, self.model_dump(mode="json", exclude_none=True))


def _strip_computed(model: BaseModel, dumped: dict[str, Any]) -> dict[str, Any]:
    """Remove computed-field keys from a dumped model, at every depth."""
    computed = set(type(model).model_computed_fields)
    result: dict[str, Any] = {}
    for key, value in dumped.items():
        if key in computed:
            continue
        result[key] = _strip_nested(getattr(model, key, None), value)
    return result


def _strip_nested(source: Any, dumped: Any) -> Any:
    """Walk a dumped value alongside the live object it came from."""
    if isinstance(source, BaseModel) and isinstance(dumped, dict):
        return _strip_computed(source, dumped)
    if isinstance(source, list | tuple) and isinstance(dumped, list):
        return [_strip_nested(s, d) for s, d in zip(source, dumped, strict=False)]
    if isinstance(source, dict) and isinstance(dumped, dict):
        return {k: _strip_nested(source.get(k), v) for k, v in dumped.items()}
    return dumped
