"""Runtime configuration for the Overturn fleet.

Every value here is environment-driven so the same code runs against the
Firestore emulator locally and against real Google Cloud in deployment.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide settings, read once from the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="OVERTURN_",
        extra="ignore",
    )

    # --- Google Cloud placement -------------------------------------------------
    project_id: str = Field(default="overturn-local", description="Google Cloud project id.")
    location: str = Field(default="us-central1", description="Vertex AI / Cloud Run region.")

    # --- Models -----------------------------------------------------------------
    # Flash carries the extraction and screening work; Pro is reserved for the one
    # genuinely hard generation step. See docs/MODEL_CHOICES.md for the rationale.
    model_flash: str = Field(
        default="gemini-3.5-flash",
        description="Workhorse. Extraction, retrieval reformulation, verification.",
    )
    model_heavy: str = Field(
        default="gemini-3.7-flash",
        description="The one genuinely hard generation step: drafting the appeal. "
        "Newest GA model in the catalog; the 3.x Pro tiers are preview-only and "
        "the preview ids sit below the 3.5 floor this project targets.",
    )
    model_guard: str = Field(
        default="gemma-4-26b-a4b-it-maas",
        description="Open-weights model backing the Sentinel screening pass, "
        "alongside Model Armor and a deterministic rule layer.",
    )
    embedding_model: str = Field(default="gemini-embedding-001")

    # --- Storage ----------------------------------------------------------------
    intake_bucket: str = Field(default="overturn-intake")
    policy_bucket: str = Field(default="overturn-policies")
    intake_topic: str = Field(default="overturn-denial-received")
    dead_letter_topic: str = Field(default="overturn-dead-letter")

    # --- Firestore collections --------------------------------------------------
    collection_cases: str = "cases"
    collection_actions: str = "actions"
    collection_audit: str = "audit_events"
    collection_quarantine: str = "quarantine"
    collection_registry: str = "agent_registry"
    collection_memory: str = "case_memory"

    # --- Behaviour --------------------------------------------------------------
    max_verification_attempts: int = Field(
        default=3,
        description="Drafting retries permitted before a case is escalated to a human.",
    )
    retrieval_score_floor: float = Field(
        default=0.55,
        description="Below this similarity the Retrieval agent reformulates its query once.",
    )

    # Demo acceleration compresses payer response windows from days to seconds so a
    # multi-week lifecycle is observable in a four-minute video. Disclosed in the
    # README and stated out loud in the demo.
    demo_time_acceleration: bool = Field(default=False)
    demo_seconds_per_day: float = Field(default=1.0)

    # --- Local development ------------------------------------------------------
    use_emulator: bool = Field(default=False)
    trace_to_console: bool = Field(
        default=False,
        description="Print spans to stdout in local mode. Useful for the demo, "
        "noisy during tests, so it is off unless asked for.",
    )
    firestore_emulator_host: str = Field(default="localhost:8080")
    runtime_mode: Literal["local", "cloud"] = Field(default="local")

    @property
    def sabotage_drafting(self) -> bool:
        """Deliberate-hallucination switch used only to prove Verification works.

        Never read from settings by default; the demo script sets the env var
        explicitly for a single run and unsets it afterwards.
        """
        import os

        return os.getenv("OVERTURN_SABOTAGE_DRAFTING", "").lower() in {"1", "true", "yes"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
