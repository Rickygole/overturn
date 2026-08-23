"""The fleet as Google ADK agents.

ADK is load-bearing here rather than declared and unused: ``AdkBackend`` below
is a ``core.llm`` backend that executes every generative call through an ADK
``LlmAgent`` and ``Runner``. Selecting it puts ADK in the request path for all
seven agents.

The prompts and output schemas are imported from the agent packages rather than
restated, so an ADK definition cannot drift from the agent it represents. If a
prompt changes, both change.

Why the orchestration is still ours rather than an ADK ``SequentialAgent``: this
pipeline terminates at a human gate and resumes weeks later from a Firestore
document, driven by a scheduler. An ADK ``Runner`` drives a session, and a
session is exactly the thing that does not survive the multi-week gap this
product is built around. Two constraints in particular — every external effect
through the idempotency guard, and a fully offline end-to-end run — are things
a runner-owned control flow would route around. So ADK owns the model calls and
the agent definitions; durable state and effects stay where they can be made
idempotent.

``fleet()`` composes the seven into a ``SequentialAgent`` for ``adk web``, which
renders the same definitions the pipeline executes.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import BaseModel

from agents.drafting.prompts import DRAFTING_SYSTEM
from agents.intake.prompts import INTAKE_SYSTEM
from agents.lifecycle.prompts import LIFECYCLE_SYSTEM
from agents.mapping.prompts import MAPPING_SYSTEM
from agents.retrieval.prompts import RETRIEVAL_REFORMULATE_SYSTEM
from agents.sentinel.prompts import SENTINEL_SYSTEM
from agents.verification.prompts import ASSERTION_SYSTEM, CITATION_SYSTEM
from core.config import Settings, get_settings
from core.llm import LlmRequest, LlmResponse, ModelUnavailable
from core.schemas.criteria import CriteriaMatrix
from core.schemas.denial import DenialExtraction
from core.schemas.draft import AppealDraft
from core.schemas.lifecycle import EscalationDecision
from core.schemas.policy import RetrievalResult
from core.schemas.sentinel import ScreeningResult
from core.schemas.verification import VerificationResult

APP_NAME = "overturn"


def _agent(
    name: str,
    description: str,
    instruction: str,
    schema: type[BaseModel],
    model: str,
) -> LlmAgent:
    return LlmAgent(
        name=name,
        description=description,
        instruction=instruction,
        output_schema=schema,
        model=model,
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


def sentinel_agent(settings: Settings | None = None) -> LlmAgent:
    s = settings or get_settings()
    return _agent(
        "sentinel",
        "Screens untrusted inbound documents for prompt injection, instruction "
        "content, tool poisoning and unexpected PII. Can halt the pipeline.",
        SENTINEL_SYSTEM,
        ScreeningResult,
        s.model_guard,
    )


def intake_agent(settings: Settings | None = None) -> LlmAgent:
    s = settings or get_settings()
    return _agent(
        "intake",
        "Transcribes a denial letter, including a scan or fax, into typed fields.",
        INTAKE_SYSTEM,
        DenialExtraction,
        s.model_flash,
    )


def retrieval_agent(settings: Settings | None = None) -> LlmAgent:
    s = settings or get_settings()
    return _agent(
        "retrieval",
        "Reformulates a policy search query when the first retrieval scores poorly.",
        RETRIEVAL_REFORMULATE_SYSTEM,
        RetrievalResult,
        s.model_flash,
    )


def mapping_agent(settings: Settings | None = None) -> LlmAgent:
    s = settings or get_settings()
    return _agent(
        "mapping",
        "Decides, for each published policy criterion, whether the chart documents "
        "what it asks for, and cites where.",
        MAPPING_SYSTEM,
        CriteriaMatrix,
        s.model_flash,
    )


def drafting_agent(settings: Settings | None = None) -> LlmAgent:
    s = settings or get_settings()
    return _agent(
        "drafting",
        "Writes the appeal letter from satisfied criteria only. Has no retrieval "
        "access of any kind.",
        DRAFTING_SYSTEM,
        AppealDraft,
        s.model_heavy,
    )


def verification_agent(settings: Settings | None = None) -> LlmAgent:
    s = settings or get_settings()
    return _agent(
        "verification",
        "Checks every citation against the verbatim policy text and every clinical "
        "assertion against the criteria matrix.",
        CITATION_SYSTEM + "\n\n" + ASSERTION_SYSTEM,
        VerificationResult,
        s.model_flash,
    )


def lifecycle_agent(settings: Settings | None = None) -> LlmAgent:
    s = settings or get_settings()
    return _agent(
        "lifecycle",
        "Explains why an unanswered appeal is advancing to the next rung of the "
        "published appeal ladder. Runs only on a schedule.",
        LIFECYCLE_SYSTEM,
        EscalationDecision,
        s.model_flash,
    )


BUILDERS = {
    "sentinel": sentinel_agent,
    "intake": intake_agent,
    "retrieval": retrieval_agent,
    "mapping": mapping_agent,
    "drafting": drafting_agent,
    "verification": verification_agent,
    "lifecycle": lifecycle_agent,
}


def fleet(settings: Settings | None = None) -> SequentialAgent:
    """The seven agents in pipeline order, for ``adk web``."""
    s = settings or get_settings()
    return SequentialAgent(
        name="overturn_fleet",
        description=(
            "Turns an insurance denial letter into a policy-cited appeal, verifies "
            "every citation before a human sees it, and escalates on a schedule for "
            "weeks afterwards."
        ),
        sub_agents=[build(s) for build in BUILDERS.values()],
    )


# --------------------------------------------------------------------------- #
# The backend that puts ADK in the request path
# --------------------------------------------------------------------------- #

# Which ADK agent answers which (agent, operation) pair. Verification makes two
# distinct calls against the same definition, which is why this is keyed on the
# pair rather than on the agent alone.
_OPERATION_TO_AGENT = {
    ("sentinel", "guard_scan"): "sentinel",
    ("intake", "extract"): "intake",
    ("retrieval", "reformulate"): "retrieval",
    ("mapping", "map_section"): "mapping",
    ("drafting", "compose"): "drafting",
    ("verification", "verify_citation"): "verification",
    ("verification", "verify_assertions"): "verification",
    ("lifecycle", "decide"): "lifecycle",
}


class AdkBackend:
    """Executes generative calls through ADK agents and a Runner."""

    name = "adk"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._runners: dict[str, InMemoryRunner] = {}

    def _runner(self, agent_name: str) -> InMemoryRunner:
        if agent_name not in self._runners:
            builder = BUILDERS[agent_name]
            self._runners[agent_name] = InMemoryRunner(
                agent=builder(self.settings), app_name=f"{APP_NAME}_{agent_name}"
            )
        return self._runners[agent_name]

    def invoke(self, request: LlmRequest) -> LlmResponse:
        agent_name = _OPERATION_TO_AGENT.get((request.agent, request.operation))
        if agent_name is None:
            raise ModelUnavailable(f"no ADK agent mapped for {request.agent}.{request.operation}")
        return asyncio.run(self._invoke(agent_name, request))

    async def _invoke(self, agent_name: str, request: LlmRequest) -> LlmResponse:
        runner = self._runner(agent_name)
        session_id = uuid.uuid4().hex
        await runner.session_service.create_session(
            app_name=runner.app_name, user_id=APP_NAME, session_id=session_id
        )

        parts: list[types.Part] = [types.Part(text=request.prompt)]
        for part in request.parts:
            parts.append(types.Part.from_bytes(data=part["data"], mime_type=part["mime_type"]))

        text = ""
        input_tokens = output_tokens = 0
        try:
            async for event in runner.run_async(
                user_id=APP_NAME,
                session_id=session_id,
                new_message=types.Content(role="user", parts=parts),
            ):
                usage = getattr(event, "usage_metadata", None)
                if usage is not None:
                    input_tokens += getattr(usage, "prompt_token_count", 0) or 0
                    output_tokens += getattr(usage, "candidates_token_count", 0) or 0
                content = getattr(event, "content", None)
                if content and content.parts:
                    for part in content.parts:
                        if getattr(part, "text", None):
                            text = part.text
        except Exception as exc:
            raise ModelUnavailable(f"{agent_name}: {exc}") from exc

        parsed: BaseModel | None = None
        if request.schema is not None and text:
            try:
                parsed = request.schema.model_validate_json(text)
            except Exception as exc:
                raise ModelUnavailable(
                    f"{agent_name} returned output that is not a valid "
                    f"{request.schema.__name__}: {exc}"
                ) from exc

        return LlmResponse(
            text=text,
            parsed=parsed,
            model=request.model,
            input_tokens=input_tokens or None,
            output_tokens=output_tokens or None,
            backend=self.name,
        )

    def embed(self, texts: list[str], model: str) -> list[list[float]]:
        """Embeddings are not an agent operation; delegate to the SDK."""
        from core.llm import VertexBackend

        return VertexBackend().embed(texts, model)


def describe_fleet() -> list[dict[str, Any]]:
    """The fleet as data, for the agent registry and the README table."""
    settings = get_settings()
    rows = []
    for name, build in BUILDERS.items():
        agent = build(settings)
        rows.append(
            {
                "name": agent.name,
                "description": agent.description,
                "model": agent.model,
                "output_schema": agent.output_schema.__name__ if agent.output_schema else None,
            }
        )
    return rows
