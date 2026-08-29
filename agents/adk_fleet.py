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
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
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
from agents.sentinel.prompts import GEMMA_GUARD_SYSTEM
from agents.verification.prompts import ASSERTION_SYSTEM, CITATION_SYSTEM
from core.config import Settings, get_settings
from core.llm import LlmRequest, LlmResponse, ModelUnavailable
from core.schemas.criteria import CriteriaMatrix
from core.schemas.denial import DenialExtraction
from core.schemas.draft import AppealDraft
from core.schemas.lifecycle import EscalationDecision
from core.schemas.policy import RetrievalResult
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
    """Sentinel's guard layer, which is the one agent NOT bound to a schema.

    Gemma accepts JSON mode but does not honour a bound `output_schema` the way
    Gemini does — Gemini enforces the shape on the wire, Gemma ignores it. Bound
    to `ScreeningResult` it returns valid JSON containing only an excerpt, and
    the response then fails validation and the whole layer is recorded as
    unavailable.

    It is also asked for fields it cannot possibly know: `document_uri` and
    `content_sha256` are facts about the request, and the model is never shown
    them. So the shape is described in the instruction, which Gemma does follow,
    and parsed tolerantly on the way back — see
    `agents/sentinel/agent.py::_parse_guard_response`, which the non-ADK path
    already uses.
    """
    s = settings or get_settings()
    return LlmAgent(
        name="sentinel",
        description=(
            "Screens untrusted inbound documents for prompt injection, instruction "
            "content, tool poisoning and unexpected PII. Can halt the pipeline."
        ),
        instruction=GEMMA_GUARD_SYSTEM,
        model=s.model_guard,
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
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

        # ADK builds its own model client internally rather than accepting one,
        # and it decides which API to talk to from the environment. Without
        # these it takes the Gemini Developer API path and fails with "No API
        # key was provided" — on a machine holding perfectly good Application
        # Default Credentials for Vertex AI.
        #
        # Set here rather than only in infra/deploy.sh so the behaviour does not
        # depend on how the process happened to be launched. Existing values are
        # respected: an operator who set them deliberately outranks this.
        os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", self.settings.project_id)
        os.environ.setdefault("GOOGLE_CLOUD_LOCATION", self.settings.model_location)

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

        # ADK's Runner is async; the pipeline calling it is synchronous by
        # design, because the orchestration has to stay readable and every stage
        # commits before the next begins. Bridging with asyncio.run works from a
        # script and blows up under a web server, which already has a loop:
        # "asyncio.run() cannot be called from a running event loop".
        #
        # This only appeared in deployment. Locally the pipeline runs from a
        # plain main(), so there is no loop to collide with — the failure needed
        # FastAPI underneath it, and no test had that.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._invoke(agent_name, request))

        # Already inside a loop: hand the coroutine to a worker thread that owns
        # its own, and block. Blocking a request handler is acceptable and
        # honest here — this pipeline is sequential by design and nothing else
        # on this request is making progress meanwhile.
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(self._invoke(agent_name, request))).result()

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
