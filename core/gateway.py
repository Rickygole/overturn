"""Deterministic policy enforcement for datastore access.

Why this file exists, stated plainly: Google Cloud IAM is per-resource, and
Firestore has no collection-level IAM. Buckets, topics and secrets are genuinely
scoped per service account by `infra/iam_setup.sh`, and an agent without the
grant cannot read them. Collections are not, so the scoping happens here
instead, in code, deterministically, with no model in the path.

This is a real enforcement boundary rather than a decorative one only because
every agent's datastore access is routed through it and there is no second door.
`core/state.py` and `core/audit.py` take a `GatewayHandle`, not a raw Firestore
client, and neither will accept one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from core.schemas.enums import AgentName


class Access(StrEnum):
    """What an agent may do with a collection."""

    NONE = "none"
    READ = "read"
    APPEND = "append"  # create new documents, never modify existing ones
    WRITE = "write"    # create and update


class PolicyViolation(PermissionError):
    """Raised when an agent reaches for something outside its scope.

    This is deliberately an exception and not a log line. A policy violation is
    a bug in the fleet, and the correct response is to stop, not to continue
    with less data than expected.
    """

    def __init__(self, agent: AgentName, collection: str, requested: Access) -> None:
        self.agent = agent
        self.collection = collection
        self.requested = requested
        super().__init__(
            f"agent {agent.value!r} has no {requested.value} access to collection "
            f"{collection!r}; check core/gateway.py POLICY"
        )


# The access matrix. Read it as a security document, because that is what it is.
#
# Two rules shape it:
#   1. No agent can write a collection it does not produce. Verification cannot
#      touch the draft it is judging; Drafting cannot touch the verdict.
#   2. Every agent appends to the audit log and nobody can rewrite it, which is
#      what makes the audit log worth reading.
POLICY: dict[AgentName, dict[str, Access]] = {
    AgentName.SENTINEL: {
        "quarantine": Access.WRITE,
        "audit_events": Access.APPEND,
        "cases": Access.READ,
        # No access to policy corpus, drafts, or actions. Sentinel handles
        # untrusted bytes; it gets the smallest surface in the fleet.
    },
    AgentName.INTAKE: {
        "cases": Access.WRITE,
        "audit_events": Access.APPEND,
    },
    AgentName.RETRIEVAL: {
        "cases": Access.WRITE,
        "audit_events": Access.APPEND,
        "policy_sections": Access.READ,
    },
    AgentName.MAPPING: {
        "cases": Access.WRITE,
        "audit_events": Access.APPEND,
    },
    AgentName.DRAFTING: {
        # Reads the case to get the criteria matrix it was handed, writes only
        # its draft back. No policy_sections: Drafting cannot go looking for
        # supporting material Mapping did not give it.
        "cases": Access.WRITE,
        "audit_events": Access.APPEND,
    },
    AgentName.VERIFICATION: {
        # Read-only on cases. Verification records its verdict through the
        # orchestrator, so it structurally cannot edit the draft it judges.
        "cases": Access.READ,
        "policy_sections": Access.READ,
        "audit_events": Access.APPEND,
    },
    AgentName.LIFECYCLE: {
        "cases": Access.WRITE,
        "actions": Access.WRITE,
        "audit_events": Access.APPEND,
        "case_memory": Access.WRITE,
    },
    AgentName.ORCHESTRATOR: {
        "cases": Access.WRITE,
        "actions": Access.WRITE,
        "audit_events": Access.APPEND,
        "quarantine": Access.READ,
        "case_memory": Access.WRITE,
        "agent_registry": Access.READ,
    },
}


# Which weaker levels each level implies.
_IMPLIES: dict[Access, set[Access]] = {
    Access.NONE: set(),
    Access.READ: {Access.READ},
    Access.APPEND: {Access.READ, Access.APPEND},
    Access.WRITE: {Access.READ, Access.APPEND, Access.WRITE},
}


def check(agent: AgentName, collection: str, requested: Access) -> None:
    """Raise unless ``agent`` may perform ``requested`` on ``collection``."""
    granted = POLICY.get(agent, {}).get(collection, Access.NONE)
    if requested not in _IMPLIES[granted]:
        raise PolicyViolation(agent, collection, requested)


def allows(agent: AgentName, collection: str, requested: Access) -> bool:
    """Non-raising form, for building the permission table in the README."""
    granted = POLICY.get(agent, {}).get(collection, Access.NONE)
    return requested in _IMPLIES[granted]


@dataclass(frozen=True)
class GatewayHandle:
    """An agent's scoped view of the datastore.

    Agents receive one of these instead of a Firestore client. The handle knows
    who it belongs to and refuses anything the policy does not allow, so an
    agent cannot reach a collection by spelling its name correctly.
    """

    agent: AgentName

    def authorize(self, collection: str, requested: Access) -> str:
        """Check policy and return the collection name if it passes."""
        check(self.agent, collection, requested)
        return collection

    def readable(self) -> set[str]:
        return {c for c, a in POLICY.get(self.agent, {}).items() if a != Access.NONE}

    def writable(self) -> set[str]:
        return {
            c
            for c, a in POLICY.get(self.agent, {}).items()
            if a in (Access.APPEND, Access.WRITE)
        }


def all_collections() -> list[str]:
    """Every collection any agent touches, sorted. Used to render the table."""
    names: set[str] = set()
    for grants in POLICY.values():
        names.update(grants)
    return sorted(names)
