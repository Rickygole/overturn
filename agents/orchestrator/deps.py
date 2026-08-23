"""Assembling the fleet.

One place where a raw store becomes eight scoped handles. If a second place
appears, the gateway has a second door.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

from agents.base import AgentDeps, build_deps
from agents.drafting.agent import DraftingAgent
from agents.intake.agent import IntakeAgent
from agents.lifecycle.agent import LifecycleAgent
from agents.mapping.agent import MappingAgent
from agents.retrieval.agent import RetrievalAgent
from agents.sentinel.agent import SentinelAgent
from agents.verification.agent import VerificationAgent
from core.config import Settings, get_settings
from core.gateway import GatewayHandle
from core.idempotency import IdempotencyGuard
from core.llm import LlmClient, build_llm
from core.schemas.enums import AgentName
from core.state import CaseRepository
from core.store import DocumentStore, build_store


@dataclass
class Fleet:
    """Every agent, plus the orchestrator's own scoped access."""

    store: DocumentStore
    llm: LlmClient
    settings: Settings
    orchestrator: AgentDeps

    sentinel: SentinelAgent
    intake: IntakeAgent
    retrieval: RetrievalAgent
    mapping: MappingAgent
    drafting: DraftingAgent
    verification: VerificationAgent
    lifecycle: LifecycleAgent

    @cached_property
    def cases(self) -> CaseRepository:
        return CaseRepository(self.store, GatewayHandle(AgentName.ORCHESTRATOR))

    @cached_property
    def guard(self) -> IdempotencyGuard:
        return IdempotencyGuard(self.store, GatewayHandle(AgentName.ORCHESTRATOR))


def build_fleet(
    store: DocumentStore | None = None,
    llm: LlmClient | None = None,
    settings: Settings | None = None,
) -> Fleet:
    """Construct the whole fleet against one datastore."""
    settings = settings or get_settings()
    store = store or build_store()
    llm = llm or build_llm()

    def deps(agent: AgentName) -> AgentDeps:
        return build_deps(store, agent, llm, settings)

    return Fleet(
        store=store,
        llm=llm,
        settings=settings,
        orchestrator=deps(AgentName.ORCHESTRATOR),
        sentinel=SentinelAgent(deps(AgentName.SENTINEL)),
        intake=IntakeAgent(deps(AgentName.INTAKE)),
        retrieval=RetrievalAgent(deps(AgentName.RETRIEVAL)),
        mapping=MappingAgent(deps(AgentName.MAPPING)),
        drafting=DraftingAgent(deps(AgentName.DRAFTING)),
        verification=VerificationAgent(deps(AgentName.VERIFICATION)),
        lifecycle=LifecycleAgent(deps(AgentName.LIFECYCLE)),
    )
