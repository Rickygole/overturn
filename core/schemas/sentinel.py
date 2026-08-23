"""Sentinel screening output.

A denial letter arrives from an outside party. Treating its contents as data
rather than as instructions is the whole job of this agent, and the screening
verdict is written to the audit log whether or not anything is found.
"""

from __future__ import annotations

from pydantic import Field, computed_field

from core.schemas.base import OverturnModel
from core.schemas.enums import ThreatCategory


class ThreatFinding(OverturnModel):
    """One thing Sentinel objected to."""

    category: ThreatCategory
    excerpt: str = Field(description="The offending span, truncated. Never re-executed.")
    detector: str = Field(
        description="Which layer caught it: 'model_armor', 'gemma', or a named rule."
    )
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


class ScreeningResult(OverturnModel):
    """Whether an inbound document is safe to hand to the rest of the fleet."""

    document_uri: str
    content_sha256: str = Field(description="Hash of the screened bytes, for the audit trail.")

    findings: list[ThreatFinding] = Field(default_factory=list)
    quarantine: bool = Field(
        default=False, description="True halts the pipeline before Intake ever sees the text."
    )

    pii_categories_found: list[str] = Field(
        default_factory=list,
        description="Expected in a denial letter; recorded, not blocked, unless unexpected.",
    )
    layers_run: list[str] = Field(
        default_factory=list, description="Which detectors actually executed."
    )
    sanitized_text: str | None = Field(
        default=None,
        description="Text with any instruction-like spans neutralised, passed to Intake "
        "when the document is suspicious but not disqualifying.",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def clean(self) -> bool:
        return not self.findings and not self.quarantine

    @computed_field  # type: ignore[prop-decorator]
    @property
    def highest_confidence(self) -> float:
        return max((f.confidence for f in self.findings), default=0.0)
