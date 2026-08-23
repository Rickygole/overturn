"""Model access for every agent, with a backend that needs no network.

Three reasons this indirection earns its place:

  * **Structured output is mandatory here.** Every generative call is bound to a
    Pydantic contract from ``core.schemas``. A response that does not validate is
    a failure at the boundary, not a surprise three agents later.
  * **The pipeline has to be runnable without a bill.** ``ScriptedBackend``
    answers from registered handlers, so the full seven-agent chain, the
    scheduler, the retry loop and the tests all run offline and deterministically.
  * **Retries belong in one place.** A transient 429 from Vertex should not be
    handled seven different ways in seven agents.

Switching between them is one environment variable. Nothing in an agent knows
which backend it is talking to.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core.config import get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class ModelUnavailable(RuntimeError):
    """The backend could not answer, after retries."""


class NoScriptedResponse(RuntimeError):
    """Offline backend was asked something no handler covers.

    Deliberately loud. A silent default here would let a test pass while
    exercising nothing.
    """


@dataclass
class LlmRequest:
    """One call to a model."""

    agent: str
    operation: str
    system: str
    prompt: str
    model: str
    schema: type[BaseModel] | None = None
    temperature: float = 0.0
    max_output_tokens: int = 8192
    parts: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class LlmResponse:
    """What came back, plus what it cost."""

    text: str
    parsed: BaseModel | None
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    backend: str = "unknown"


class LlmBackend(Protocol):
    name: str

    def invoke(self, request: LlmRequest) -> LlmResponse: ...

    def embed(self, texts: list[str], model: str) -> list[list[float]]: ...


# --------------------------------------------------------------------------- #
# Offline backend
# --------------------------------------------------------------------------- #

Handler = Callable[[LlmRequest], BaseModel | str]


class ScriptedBackend:
    """Deterministic offline responses, registered per ``agent.operation``.

    Handlers receive the full request, so they can respond to the actual input
    rather than returning a fixed blob. That matters: the offline run of the
    hallucination demo has to genuinely produce a bad citation and the real
    Verification agent has to genuinely catch it, or the demo proves nothing.
    """

    name = "scripted"

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}
        self.calls: list[LlmRequest] = []

    def register(self, agent: str, operation: str, handler: Handler) -> None:
        self._handlers[f"{agent}.{operation}"] = handler

    def invoke(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        key = f"{request.agent}.{request.operation}"
        handler = self._handlers.get(key)
        if handler is None:
            raise NoScriptedResponse(
                f"no offline handler registered for {key!r}; register one with "
                f"ScriptedBackend.register({request.agent!r}, {request.operation!r}, fn)"
            )

        result = handler(request)
        if isinstance(result, BaseModel):
            return LlmResponse(
                text=result.model_dump_json(),
                parsed=result,
                model=request.model,
                input_tokens=_rough_tokens(request.prompt) + _rough_tokens(request.system),
                output_tokens=_rough_tokens(result.model_dump_json()),
                backend=self.name,
            )
        return LlmResponse(
            text=str(result),
            parsed=None,
            model=request.model,
            input_tokens=_rough_tokens(request.prompt),
            output_tokens=_rough_tokens(str(result)),
            backend=self.name,
        )

    def embed(self, texts: list[str], model: str) -> list[list[float]]:
        """Deterministic hashed embeddings.

        Not semantically meaningful, and not pretending to be. They exist so the
        indexing and storage paths can be exercised offline; the lexical
        retriever in ``agents/retrieval`` is what actually finds policy sections
        when there is no vector index.
        """
        import hashlib
        import struct

        vectors: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            floats = struct.unpack("8f", digest[:32])
            norm = sum(f * f for f in floats) ** 0.5 or 1.0
            vectors.append([f / norm for f in floats])
        return vectors


def _rough_tokens(text: str) -> int:
    """Approximate token count for offline accounting. Four characters a token."""
    return max(1, len(text) // 4)


# --------------------------------------------------------------------------- #
# Vertex backend
# --------------------------------------------------------------------------- #


class VertexBackend:
    """Real Gemini and Gemma access through Vertex AI."""

    name = "vertex"

    def __init__(self, project_id: str | None = None, location: str | None = None) -> None:
        from google import genai

        settings = get_settings()
        self._client = genai.Client(
            vertexai=True,
            project=project_id or settings.project_id,
            location=location or settings.location,
        )

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=20),
        retry=retry_if_exception_type(ModelUnavailable),
        reraise=True,
    )
    def invoke(self, request: LlmRequest) -> LlmResponse:
        from google.genai import types

        config: dict[str, Any] = {
            "temperature": request.temperature,
            "max_output_tokens": request.max_output_tokens,
        }
        if request.system:
            config["system_instruction"] = request.system
        if request.schema is not None:
            config["response_mime_type"] = "application/json"
            config["response_schema"] = request.schema

        contents: list[Any] = [request.prompt]
        for part in request.parts:
            contents.append(types.Part.from_bytes(data=part["data"], mime_type=part["mime_type"]))

        try:
            response = self._client.models.generate_content(
                model=request.model,
                contents=contents,
                config=types.GenerateContentConfig(**config),
            )
        except Exception as exc:
            raise ModelUnavailable(f"{request.model}: {exc}") from exc

        parsed: BaseModel | None = None
        if request.schema is not None:
            candidate = getattr(response, "parsed", None)
            if isinstance(candidate, BaseModel):
                parsed = candidate
            elif response.text:
                parsed = request.schema.model_validate_json(response.text)

        usage = getattr(response, "usage_metadata", None)
        return LlmResponse(
            text=response.text or "",
            parsed=parsed,
            model=request.model,
            input_tokens=getattr(usage, "prompt_token_count", None),
            output_tokens=getattr(usage, "candidates_token_count", None),
            backend=self.name,
        )

    def embed(self, texts: list[str], model: str) -> list[list[float]]:
        result = self._client.models.embed_content(model=model, contents=texts)
        return [list(e.values) for e in result.embeddings]


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class LlmClient:
    """What agents actually hold."""

    def __init__(self, backend: LlmBackend) -> None:
        self.backend = backend

    @property
    def offline(self) -> bool:
        return self.backend.name == "scripted"

    def structured(
        self,
        agent: str,
        operation: str,
        system: str,
        prompt: str,
        schema: type[T],
        model: str,
        temperature: float = 0.0,
        parts: list[dict[str, Any]] | None = None,
    ) -> tuple[T, LlmResponse]:
        """Call a model and get back a validated contract instance."""
        request = LlmRequest(
            agent=agent,
            operation=operation,
            system=system,
            prompt=prompt,
            model=model,
            schema=schema,
            temperature=temperature,
            parts=parts or [],
        )
        response = self.backend.invoke(request)
        if response.parsed is None:
            raise ModelUnavailable(f"{agent}.{operation} returned no parsable {schema.__name__}")
        if not isinstance(response.parsed, schema):
            raise ModelUnavailable(
                f"{agent}.{operation} returned {type(response.parsed).__name__}, "
                f"expected {schema.__name__}"
            )
        return response.parsed, response

    def text(
        self,
        agent: str,
        operation: str,
        system: str,
        prompt: str,
        model: str,
        temperature: float = 0.0,
    ) -> LlmResponse:
        return self.backend.invoke(
            LlmRequest(
                agent=agent,
                operation=operation,
                system=system,
                prompt=prompt,
                model=model,
                temperature=temperature,
            )
        )

    def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        return self.backend.embed(texts, model or get_settings().embedding_model)


def build_llm(backend: LlmBackend | None = None) -> LlmClient:
    """Pick a backend from configuration unless one is supplied."""
    if backend is not None:
        return LlmClient(backend)
    settings = get_settings()
    if settings.runtime_mode == "cloud":
        return LlmClient(VertexBackend())
    return LlmClient(ScriptedBackend())
