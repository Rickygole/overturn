"""Tests for the agent catalogue.

The value of a registry is entirely in whether it is true. A catalogue that
drifts from the running system is worse than none, because people trust it —
and here the thing being trusted is a statement about which agents may touch
patient data.

So these tests check derivation, not content: the registry must agree with the
gateway, with the IAM roster, and with the ADK definitions, because those are
what actually run.
"""

from __future__ import annotations

import pytest

from core.gateway import POLICY, Access, GatewayHandle, PolicyViolation
from core.registry import (
    REGISTRY_COLLECTION,
    build_catalogue,
    discover,
    render_table,
    seed,
)
from core.schemas.enums import AgentName
from core.store import MemoryStore


@pytest.fixture
def store() -> MemoryStore:
    store = MemoryStore()
    seed(store)
    return store


class TestDerivation:
    def test_permissions_match_the_gateway_exactly(self):
        """A catalogue that disagrees with the enforcer is a lie."""
        for entry in build_catalogue():
            agent = AgentName(entry.agent_id)
            grants = POLICY[agent]
            expected_writes = sorted(
                c for c, a in grants.items() if a in (Access.APPEND, Access.WRITE)
            )
            expected_reads = sorted(c for c, a in grants.items() if a is not Access.NONE)
            assert entry.writes == expected_writes, entry.agent_id
            assert entry.reads == expected_reads, entry.agent_id

    def test_every_agent_has_a_purpose_from_the_iam_roster(self):
        """The same file the service accounts are created from."""
        for entry in build_catalogue():
            assert entry.purpose, f"{entry.agent_id} has no purpose"
            assert len(entry.purpose) > 20

    def test_models_match_the_adk_definitions(self):
        from agents.adk_fleet import BUILDERS
        from core.config import get_settings

        settings = get_settings()
        for entry in build_catalogue():
            expected = BUILDERS[entry.agent_id](settings)
            assert entry.model == expected.model
            schema = getattr(expected, "output_schema", None)
            assert entry.output_contract == (schema.__name__ if schema else None)

    def test_only_sentinel_has_no_bound_output_contract(self):
        """Every agent is schema-bound except the one whose model ignores it.

        Gemma accepts JSON mode and disregards a bound schema, so binding one
        produced valid JSON that failed validation and took the whole screening
        layer down with it. That is a real property of the fleet and the
        catalogue should state it.
        """
        unbound = {e.agent_id for e in build_catalogue() if e.output_contract is None}
        assert unbound == {"sentinel"}

    def test_the_orchestrator_is_not_published(self):
        """It is the router, not a service anyone should call directly."""
        assert "orchestrator" not in {e.agent_id for e in build_catalogue()}

    def test_all_seven_agents_are_published(self):
        assert len(build_catalogue()) == 7


class TestDiscovery:
    def test_find_agents_that_write_a_collection(self, store):
        """The first question a security review asks."""
        writers = {a.agent_id for a in discover(store, writes_to="cases")}
        assert "verification" not in writers, "verification must not write cases"
        assert "intake" in writers

    def test_find_agents_handling_untrusted_input(self, store):
        """The second question a security review asks."""
        exposed = {a.agent_id for a in discover(store, handles_untrusted_input=True)}
        assert exposed == {"sentinel", "intake"}

    def test_nobody_but_lifecycle_writes_actions(self, store):
        """Only Lifecycle and the unpublished orchestrator may claim actions."""
        assert {a.agent_id for a in discover(store, writes_to="actions")} == {"lifecycle"}

    def test_discovery_returns_typed_entries(self, store):
        for entry in discover(store):
            assert entry.definition_sha256
            assert entry.service_account.endswith(".iam.gserviceaccount.com")


class TestVersioning:
    def test_the_fingerprint_changes_when_permissions_change(self):
        """A caller needs to know when an agent's contract moved."""
        before = {e.agent_id: e.definition_sha256 for e in build_catalogue()}

        original = POLICY[AgentName.MAPPING].copy()
        POLICY[AgentName.MAPPING]["policy_sections"] = Access.READ
        try:
            after = {e.agent_id: e.definition_sha256 for e in build_catalogue()}
        finally:
            POLICY[AgentName.MAPPING] = original

        assert before["mapping"] != after["mapping"]
        assert before["drafting"] == after["drafting"], "unrelated agents should not move"

    def test_the_fingerprint_is_stable_across_rebuilds(self):
        assert [e.definition_sha256 for e in build_catalogue()] == [
            e.definition_sha256 for e in build_catalogue()
        ]


class TestSeeding:
    def test_seeding_is_idempotent(self, store):
        seed(store)
        seed(store)
        assert store.count(REGISTRY_COLLECTION) == 7

    def test_the_rendered_table_lists_every_agent(self):
        table = render_table(build_catalogue())
        for agent in AgentName:
            if agent is AgentName.ORCHESTRATOR:
                continue
            assert f"`{agent.value}`" in table


class TestNoSecondDoor:
    """`seed()` and `discover()` used to call `store.set`/`store.query` on
    `agent_registry` directly, no `GatewayHandle` in the path at all -- a
    second door into a collection `core/gateway.py`'s own docstring claims
    has none. Both default to the orchestrator identity now, which is what
    `scripts/seed_registry.py` -- the only caller outside this suite -- has
    always run as administratively. `POLICY` moved the orchestrator's
    `agent_registry` grant from `READ` to `WRITE` to make `seed()`'s write
    legal under that identity; `discover()` needed nothing new, since `WRITE`
    implies `READ`.
    """

    def test_seed_and_discover_are_authorized_through_the_gateway(self, monkeypatch):
        calls: list[tuple[AgentName, str, Access]] = []
        original = GatewayHandle.authorize

        def spy(self, collection, access):
            calls.append((self.agent, collection, access))
            return original(self, collection, access)

        monkeypatch.setattr(GatewayHandle, "authorize", spy)
        store = MemoryStore()
        seed(store)
        discover(store)

        assert (AgentName.ORCHESTRATOR, "agent_registry", Access.WRITE) in calls
        assert (AgentName.ORCHESTRATOR, "agent_registry", Access.READ) in calls

    def test_seed_is_refused_under_an_identity_without_the_grant(self):
        """Sentinel holds no grant at all on `agent_registry`
        (`core/gateway.py`) -- routed through its handle instead of the
        default, the write must raise rather than land.
        """
        store = MemoryStore()
        with pytest.raises(PolicyViolation):
            seed(store, gateway=GatewayHandle(AgentName.SENTINEL))
        assert store.count(REGISTRY_COLLECTION) == 0

    def test_discover_is_refused_under_an_identity_without_the_grant(self):
        store = MemoryStore()
        seed(store)  # under the default (orchestrator) identity, which is allowed
        with pytest.raises(PolicyViolation):
            discover(store, gateway=GatewayHandle(AgentName.SENTINEL))
