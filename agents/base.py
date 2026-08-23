"""The base every agent is built on.

One class, and its job is to make three things impossible to forget:

  * an agent invocation always opens a trace span
  * an agent invocation always writes exactly one audit event, including when
    it raises
  * an agent never holds a raw datastore handle

The third is the important one. :class:`AgentDeps` carries a ``GatewayHandle``
and never a ``DocumentStore``, so an agent cannot reach a collection outside its
policy by spelling the name correctly — there is no object in scope that would
accept the call.

Agents are pure functions of their inputs. They compute and return a contract;
the orchestrator persists it. That is not a style preference — it is what
``core.gateway.POLICY`` permits, since Sentinel and Verification have read-only
access to ``cases`` and only the orchestrator and Lifecycle may claim actions.
The separation of concerns is therefore structural rather than asserted.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar, Generic, TypeVar

from core.audit import AuditLog, Recording
from core.config import Settings, get_settings
from core.gateway import GatewayHandle
from core.llm import LlmClient, build_llm
from core.schemas.enums import AgentName
from core.state import CaseRepository
from core.store import DocumentStore
from core.telemetry import agent_span

TIn = TypeVar("TIn")
TOut = TypeVar("TOut")


@dataclass(frozen=True)
class AgentDeps:
    """What an agent is handed.

    Deliberately contains no ``DocumentStore``. Anything an agent needs from the
    datastore arrives through a scoped handle or as an argument.
    """

    gateway: GatewayHandle
    audit: AuditLog
    llm: LlmClient
    settings: Settings
    cases: CaseRepository | None = None


def build_deps(
    store: DocumentStore,
    agent: AgentName,
    llm: LlmClient | None = None,
    settings: Settings | None = None,
) -> AgentDeps:
    """The one place a raw store is converted into scoped handles.

    Every other module takes ``AgentDeps``. If a second function like this
    appears, the gateway has a second door.
    """
    settings = settings or get_settings()
    handle = GatewayHandle(agent)
    return AgentDeps(
        gateway=handle,
        audit=AuditLog(store, handle),
        llm=llm or build_llm(),
        settings=settings,
        cases=CaseRepository(store, handle) if "cases" in handle.readable() else None,
    )


class OverturnAgent(ABC, Generic[TIn, TOut]):
    """Base class for all seven agents."""

    name: ClassVar[AgentName]
    operation: ClassVar[str]
    version: ClassVar[str] = "0.1.0"

    def __init__(self, deps: AgentDeps) -> None:
        if deps.gateway.agent is not self.name:
            raise ValueError(
                f"{type(self).__name__} requires deps scoped to {self.name.value!r}, "
                f"got {deps.gateway.agent.value!r}"
            )
        self.deps = deps

    # -- the invocation wrapper; subclasses implement _execute, not this ----- #

    def run(self, case_id: str, request: TIn, attempt: int = 1) -> TOut:
        """Invoke the agent, traced and audited.

        Not intended to be overridden. If a subclass needs different behaviour
        it belongs in ``_execute``, so that the span and the audit event cannot
        be skipped by accident.
        """
        with agent_span(
            self.name.value,
            case_id,
            self.operation,
            attempt=attempt,
            agent_version=self.version,
        ):
            with self.deps.audit.record(
                case_id, self.operation, self._digest_payload(request), attempt
            ) as rec:
                rec.input_summary = self._summarise(request)
                output = self._execute(case_id, request, rec, attempt)
                rec.output = self._render_output(output)
                return output

    @abstractmethod
    def _execute(self, case_id: str, request: TIn, rec: Recording, attempt: int) -> TOut:
        """Do the work. Set ``rec.decision`` before returning."""

    # -- hooks with sensible defaults ---------------------------------------- #

    def _summarise(self, request: TIn) -> str | None:
        """A short, non-identifying description of the input for the audit log.

        Returning ``None`` is fine and is safer than returning something that
        might carry a patient name into the one broadly-readable collection.
        """
        return None

    def _digest_payload(self, request: TIn) -> Any:
        """What gets hashed into ``AuditEvent.input_sha256``."""
        if hasattr(request, "to_firestore"):
            return request.to_firestore()
        if hasattr(request, "model_dump"):
            return request.model_dump(mode="json")
        return repr(request)

    def _render_output(self, output: TOut) -> dict[str, Any] | None:
        if hasattr(output, "to_firestore"):
            return output.to_firestore()
        if hasattr(output, "model_dump"):
            return output.model_dump(mode="json")
        return None

    # -- convenience --------------------------------------------------------- #

    @property
    def llm(self) -> LlmClient:
        return self.deps.llm

    @property
    def settings(self) -> Settings:
        return self.deps.settings
