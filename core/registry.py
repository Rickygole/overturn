"""The agent registry: how an organisation discovers this fleet.

The Fortified Enterprise Fleet track asks how agents are catalogued for
cross-department use — how another team finds them, knows what they are for,
knows what version they are on, and knows what they are allowed to touch before
deciding whether to call one.

There is no public REST surface for the managed Agent Registry on this project
(see `docs/PLATFORM_PROBE.md`), so this implements the same contract on
primitives: a Firestore collection of versioned agent definitions, seeded from
the same sources the running code uses.

The important property is that a registry entry cannot drift from the agent it
describes. Every field is derived:

  * identity and purpose from `infra/agents.env`, the same file the IAM script
    reads to create the service accounts
  * model and output contract from `agents/adk_fleet.py`, the same ADK
    definitions the pipeline executes
  * permissions from `core.gateway.POLICY`, the dict the gateway actually
    enforces at runtime

A catalogue that is maintained by hand is a catalogue that is wrong, and a
wrong catalogue about who may touch patient data is worse than no catalogue.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from pydantic import Field

from core.gateway import POLICY, Access
from core.schemas.base import OverturnModel, utcnow
from core.schemas.enums import AgentName
from core.store import DocumentStore

REGISTRY_COLLECTION = "agent_registry"
AGENTS_ENV = Path(__file__).resolve().parents[1] / "infra" / "agents.env"


class RegisteredAgent(OverturnModel):
    """One catalogue entry.

    This is what another team sees when they go looking for an agent to call.
    """

    agent_id: str
    display_name: str
    version: str = "0.1.0"
    purpose: str = Field(description="One line, written for someone outside this team.")

    service_account: str = Field(description="The identity it runs as.")
    model: str | None = Field(default=None, description="Which model it invokes, if any.")
    output_contract: str | None = Field(default=None, description="The typed contract it returns.")

    reads: list[str] = Field(default_factory=list)
    writes: list[str] = Field(default_factory=list)

    invocation: str = Field(
        default="orchestrated",
        description="'orchestrated' runs only inside the pipeline; 'scheduled' runs "
        "on a timer; 'callable' can be invoked directly by another team.",
    )
    handles_untrusted_input: bool = Field(
        default=False,
        description="Whether this agent reads data from outside the organisation. "
        "Surfaced because it changes the review a caller should do.",
    )

    registered_at: datetime = Field(default_factory=utcnow)
    definition_sha256: str = Field(
        description="Hash of the derived definition. A change here means the agent's "
        "contract or permissions moved, which is the thing a caller needs to know."
    )


def _purposes() -> dict[str, tuple[str, str]]:
    """Read `(service_account, purpose)` per agent from the identity roster."""
    rows: dict[str, tuple[str, str]] = {}
    for line in AGENTS_ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        agent_id, service_account, purpose = line.split("|", 2)
        rows[agent_id.strip()] = (service_account.strip(), purpose.strip())
    return rows


# Agents that read data originating outside the organisation. Sentinel screens
# the inbound document and Intake transcribes it; everything downstream works
# from typed contracts produced inside the system.
UNTRUSTED_INPUT_AGENTS = frozenset({AgentName.SENTINEL, AgentName.INTAKE})

SCHEDULED_AGENTS = frozenset({AgentName.LIFECYCLE})


def build_catalogue() -> list[RegisteredAgent]:
    """Derive the whole catalogue from the sources of truth."""
    from agents.adk_fleet import BUILDERS
    from core.config import get_settings

    settings = get_settings()
    purposes = _purposes()
    entries: list[RegisteredAgent] = []

    for agent in AgentName:
        if agent is AgentName.ORCHESTRATOR:
            # Not an agent anyone calls. It is the deterministic router, it makes
            # no model call, and publishing it as callable would invite exactly
            # the coupling the gateway exists to prevent.
            continue

        service_account, purpose = purposes.get(agent.value, (f"overturn-{agent.value}", ""))
        grants = POLICY.get(agent, {})
        reads = sorted(c for c, a in grants.items() if a is not Access.NONE)
        writes = sorted(c for c, a in grants.items() if a in (Access.APPEND, Access.WRITE))

        model = output_contract = None
        if (builder := BUILDERS.get(agent.value)) is not None:
            definition = builder(settings)
            model = definition.model
            output_contract = (
                definition.output_schema.__name__ if definition.output_schema else None
            )

        fingerprint = hashlib.sha256(
            "|".join([agent.value, str(model), str(output_contract), *reads, *writes]).encode()
        ).hexdigest()

        entries.append(
            RegisteredAgent(
                agent_id=agent.value,
                display_name=f"Overturn {agent.value.title()}",
                purpose=purpose,
                service_account=f"{service_account}@{settings.project_id}.iam.gserviceaccount.com",
                model=model,
                output_contract=output_contract,
                reads=reads,
                writes=writes,
                invocation="scheduled" if agent in SCHEDULED_AGENTS else "orchestrated",
                handles_untrusted_input=agent in UNTRUSTED_INPUT_AGENTS,
                definition_sha256=fingerprint,
            )
        )

    return entries


def seed(store: DocumentStore) -> list[RegisteredAgent]:
    """Publish the catalogue. Safe to re-run; entries are replaced wholesale."""
    catalogue = build_catalogue()
    for entry in catalogue:
        store.set(REGISTRY_COLLECTION, entry.agent_id, entry.to_firestore())
    return catalogue


def discover(
    store: DocumentStore,
    writes_to: str | None = None,
    handles_untrusted_input: bool | None = None,
) -> list[RegisteredAgent]:
    """Find agents by capability, which is how a catalogue is actually used.

    "Which agents can write to the cases collection" and "which agents touch
    data from outside the organisation" are the two questions a security review
    asks first, so they are the two the registry answers directly.
    """
    rows = store.query(REGISTRY_COLLECTION)
    found = [RegisteredAgent.model_validate(data) for _, data in rows]

    if writes_to is not None:
        found = [a for a in found if writes_to in a.writes]
    if handles_untrusted_input is not None:
        found = [a for a in found if a.handles_untrusted_input is handles_untrusted_input]

    found.sort(key=lambda a: a.agent_id)
    return found


def render_table(catalogue: list[RegisteredAgent]) -> str:
    """The catalogue as a markdown table, for the README permission section."""
    lines = [
        "| Agent | Service account | Model | Returns | Reads | Writes |",
        "|---|---|---|---|---|---|",
    ]
    for entry in catalogue:
        account = entry.service_account.split("@")[0]
        lines.append(
            f"| `{entry.agent_id}` | `{account}` | `{entry.model or '—'}` | "
            f"`{entry.output_contract or '—'}` | {', '.join(f'`{r}`' for r in entry.reads) or '—'} | "
            f"{', '.join(f'`{w}`' for w in entry.writes) or '—'} |"
        )
    return "\n".join(lines)
