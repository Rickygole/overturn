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
    model_armor_template: str = Field(
        default="",
        description="Model Armor template id backing Sentinel's second layer. Empty "
        "disables it, and the audit log then records the layer as skipped rather "
        "than clean — 'we did not look' and 'we looked and found nothing' are "
        "different facts.",
    )

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
    # Both thresholds are measured, not guessed. See the calibration table in
    # docs/MODEL_CHOICES.md and re-run scripts/calibrate_retrieval.py after any
    # change to the corpus or the scorer.
    retrieval_score_floor: float = Field(
        default=0.11,
        description="Below this similarity the Retrieval agent reformulates its query once. "
        "Set just above the weakest correct match so the reformulation path is "
        "genuinely exercised rather than being dead code.",
    )
    retrieval_no_policy_floor: float = Field(
        default=0.06,
        description="Below this, nothing in the corpus governs the denial and the correct "
        "action is to decline to appeal. Set above every no-policy case and below "
        "every real one, with the margin recorded in docs/MODEL_CHOICES.md.",
    )

    # Demo acceleration compresses payer response windows from days to seconds so a
    # multi-week lifecycle is observable in a four-minute video. Disclosed in the
    # README and stated out loud in the demo.
    demo_time_acceleration: bool = Field(default=False)
    demo_seconds_per_day: float = Field(default=1.0)

    # --- Local development ------------------------------------------------------
    llm_backend: Literal["auto", "scripted", "vertex", "adk"] = Field(
        default="auto",
        description="Which model backend to use. 'auto' picks ADK in cloud mode and "
        "the offline scripted backend locally. 'adk' routes every generative call "
        "through a Google ADK agent and Runner; 'vertex' calls the Gen AI SDK "
        "directly, which is the faster path when the ADK session layer adds nothing.",
    )

    use_emulator: bool = Field(default=False)
    trace_to_console: bool = Field(
        default=False,
        description="Print spans to stdout in local mode. Useful for the demo, "
        "noisy during tests, so it is off unless asked for.",
    )
    firestore_emulator_host: str = Field(default="localhost:8080")
    runtime_mode: Literal["local", "cloud"] = Field(default="local")

    def sabotage_drafting_on(self, attempt: int) -> bool:
        """Deliberate-fault switch, used only to prove Verification works.

        Off unless the environment variable is set for a single deliberate run.
        Two modes, because they demonstrate different things:

        ``first``  fabricate on attempt 1 only. A transient fault: Verification
                   rejects it, the specific reason is fed back, and attempt 2 is
                   clean. This shows the loop recovering.
        ``always`` fabricate on every attempt. A persistent fault: all three
                   attempts are rejected and the case goes to a person with
                   nothing sent. This shows the cap holding.

        A safety net nobody has ever dropped into is not a demonstrated safety
        net, which is why this exists at all. It is disclosed in the README and
        stated out loud in the demo.
        """
        import os

        mode = os.getenv("OVERTURN_SABOTAGE_DRAFTING", "").lower()
        if mode in {"1", "true", "yes", "always"}:
            return True
        if mode == "first":
            return attempt == 1
        return False

    @property
    def sabotage_configured(self) -> bool:
        """Whether fault injection is enabled at all, for the run banner."""
        import os

        return bool(os.getenv("OVERTURN_SABOTAGE_DRAFTING", "").strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
