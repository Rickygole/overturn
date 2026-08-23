"""Tests for the per-agent access policy.

The permission table in the README is generated from the same POLICY dict these
tests assert against, so a table that disagrees with the code is not possible.
"""

from __future__ import annotations

import pytest

from core.gateway import (
    POLICY,
    Access,
    GatewayHandle,
    PolicyViolation,
    all_collections,
    allows,
)
from core.schemas.enums import AgentName


class TestSeparationOfConcerns:
    def test_drafting_cannot_reach_the_policy_corpus(self):
        """Drafting works only from what Mapping hands it.

        This is the structural reason Drafting cannot wander off and find
        supporting material of its own.
        """
        assert not allows(AgentName.DRAFTING, "policy_sections", Access.READ)

    def test_verification_cannot_write_the_draft_it_judges(self):
        assert allows(AgentName.VERIFICATION, "cases", Access.READ)
        assert not allows(AgentName.VERIFICATION, "cases", Access.WRITE)

    def test_sentinel_has_the_smallest_surface(self):
        """Sentinel handles untrusted bytes, so it gets the least reach."""
        sentinel = GatewayHandle(AgentName.SENTINEL).writable()
        assert sentinel == {"quarantine", "audit_events"}
        assert not allows(AgentName.SENTINEL, "actions", Access.WRITE)
        assert not allows(AgentName.SENTINEL, "policy_sections", Access.READ)

    def test_only_lifecycle_and_orchestrator_may_claim_actions(self):
        permitted = {
            agent
            for agent in AgentName
            if allows(agent, "actions", Access.WRITE)
        }
        assert permitted == {AgentName.LIFECYCLE, AgentName.ORCHESTRATOR}

    def test_intake_cannot_see_policy_sections(self):
        """Extraction must not be able to consult the answer key."""
        assert not allows(AgentName.INTAKE, "policy_sections", Access.READ)


class TestAuditLogIsAppendOnly:
    def test_every_agent_can_append_audit_events(self):
        for agent in AgentName:
            assert allows(agent, "audit_events", Access.APPEND), agent

    def test_no_agent_can_overwrite_audit_events(self):
        for agent in AgentName:
            assert not allows(agent, "audit_events", Access.WRITE), agent


class TestAccessLevels:
    def test_write_implies_read_and_append(self):
        assert allows(AgentName.INTAKE, "cases", Access.READ)
        assert allows(AgentName.INTAKE, "cases", Access.APPEND)
        assert allows(AgentName.INTAKE, "cases", Access.WRITE)

    def test_read_does_not_imply_append(self):
        assert allows(AgentName.VERIFICATION, "policy_sections", Access.READ)
        assert not allows(AgentName.VERIFICATION, "policy_sections", Access.APPEND)

    def test_unlisted_collection_is_denied(self):
        assert not allows(AgentName.MAPPING, "nonexistent_collection", Access.READ)


class TestHandle:
    def test_authorize_returns_the_collection_when_permitted(self):
        handle = GatewayHandle(AgentName.MAPPING)
        assert handle.authorize("cases", Access.WRITE) == "cases"

    def test_authorize_raises_with_an_actionable_message(self):
        handle = GatewayHandle(AgentName.DRAFTING)
        with pytest.raises(PolicyViolation) as exc:
            handle.authorize("policy_sections", Access.READ)
        assert "drafting" in str(exc.value)
        assert "policy_sections" in str(exc.value)
        assert "core/gateway.py" in str(exc.value)


class TestPolicyCompleteness:
    def test_every_agent_has_a_policy_entry(self):
        assert set(POLICY) == set(AgentName)

    def test_no_agent_has_blanket_access(self):
        """If one agent could reach everything, the boundaries would be theatre."""
        every = set(all_collections())
        for agent, grants in POLICY.items():
            reachable = {c for c, a in grants.items() if a != Access.NONE}
            assert reachable != every, f"{agent} can reach every collection"
