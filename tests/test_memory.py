"""Tests for cross-case memory.

Two things are being defended. That the memory is *useful* — it has to learn
something a single case cannot show. And that its scope holds — it accumulates
leverage against an insurer, and must never quietly become a longitudinal
profile of a sick person.

The second is the one worth writing tests for, because it is the one that
degrades silently.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from core.gateway import GatewayHandle, PolicyViolation
from core.memory import (
    MEMORY_COLLECTION,
    MemoryBank,
    contains_no_patient_identifiers,
    observation_key,
)
from core.schemas.base import utcnow
from core.schemas.case import CaseRecord
from core.schemas.denial import DenialExtraction
from core.schemas.enums import AgentName
from core.store import MemoryStore


def _case(case_id: str, days_ago: int = 40, escalations: int = 0) -> CaseRecord:
    case = CaseRecord(case_id=case_id, source_document_uri="gs://intake/x.txt")
    case.denial = DenialExtraction(
        payer_name="Northbeck Health Plan",
        denial_reason_text="Not medically necessary.",
        denial_reason_code="NBH-NMN-04",
        member_id="NBH-4417-99083",
        patient_name="Jeromy156 Upton904",
    )
    case.submitted_at = utcnow() - timedelta(days=days_ago)
    case.escalation_count = escalations
    return case


@pytest.fixture
def bank() -> MemoryBank:
    return MemoryBank(MemoryStore(), GatewayHandle(AgentName.LIFECYCLE))


class TestScope:
    """The constraint that matters most."""

    def test_memory_is_keyed_on_the_payer_not_the_patient(self, bank):
        case = _case("C1")
        observation = bank.record_submission(case)
        assert "NBH-4417-99083" not in observation.key
        assert "Jeromy" not in observation.key
        assert observation.key == "Northbeck Health Plan|any|NBH-NMN-04"

    def test_no_patient_identifier_reaches_the_stored_record(self, bank):
        case = _case("C1")
        bank.record_submission(case)
        observation = bank.record_resolution(case, "overturned")
        assert contains_no_patient_identifiers(observation)

    def test_two_patients_with_one_payer_share_a_single_observation(self, bank):
        """Because what is being learned is about the payer, not the person."""
        first = _case("C1")
        second = _case("C2")
        second.denial.member_id = "NBH-4417-00000"
        second.denial.patient_name = "Someone Else"

        bank.record_submission(first)
        bank.record_submission(second)

        observation = bank.recall("Northbeck Health Plan", None, "NBH-NMN-04")
        assert observation.appeals_submitted == 2


class TestLearning:
    def test_it_measures_what_the_payer_actually_does(self, bank):
        """The published window is a claim; this is the measurement."""
        for index, days in enumerate((44, 46, 51)):
            case = _case(f"C{index}", days_ago=days)
            bank.record_submission(case)
            bank.record_resolution(case, "upheld")

        observation = bank.recall("Northbeck Health Plan", None, "NBH-NMN-04")
        assert observation.median_response_days == pytest.approx(46, abs=1)

    def test_overturn_rate_is_withheld_until_there_is_evidence(self, bank):
        """Two cases is an anecdote. Reporting a rate from it is worse than silence."""
        for index in range(2):
            case = _case(f"C{index}")
            bank.record_submission(case)
            bank.record_resolution(case, "overturned")

        observation = bank.recall("Northbeck Health Plan", None, "NBH-NMN-04")
        assert observation.overturn_rate is None

        case = _case("C3")
        bank.record_submission(case)
        bank.record_resolution(case, "upheld")
        observation = bank.recall("Northbeck Health Plan", None, "NBH-NMN-04")
        assert observation.overturn_rate == pytest.approx(2 / 3)

    def test_it_remembers_which_sections_actually_won(self, bank):
        from core.schemas.case import ClinicianCosign, HumanDecision
        from core.schemas.draft import AppealDraft, Citation

        case = _case("C1")
        case.drafts = [
            AppealDraft(
                case_id="C1",
                attempt=1,
                subject_line="Appeal",
                body="b" * 60,
                citations=[Citation(section_id="NBH-ENDO-031-3.4", claim="requires x")],
            )
        ]
        case.human_decision = HumanDecision(
            decided_by="clerk", approved=True, draft_attempt_approved=1
        )
        case.clinician_cosign = ClinicianCosign(
            clinician_name="Dr X", credential="MD", attests_clinical_accuracy=True
        )

        bank.record_submission(case)
        observation = bank.record_resolution(case, "overturned")
        assert "NBH-ENDO-031-3.4" in observation.sections_successfully_cited

    def test_a_summary_says_nothing_until_it_can_say_something_true(self, bank):
        case = _case("C1")
        observation = bank.record_submission(case)
        assert "Not enough resolved cases" in observation.summarise()


class TestInformingWithoutDeciding:
    def test_expected_days_falls_back_to_the_published_window(self, bank):
        """With no history, the published figure is the only honest answer."""
        assert bank.expected_response_days(_case("C1"), published_window=30) == 30

    def test_observed_turnaround_widens_but_never_narrows_the_expectation(self, bank):
        """A payer that has been slow before may still answer on time.

        Shortening the window on the basis of history would mean escalating a
        case the payer is still entitled to be considering.
        """
        for index in range(4):
            case = _case(f"C{index}", days_ago=48)
            bank.record_submission(case)
            bank.record_resolution(case, "upheld")

        case = _case("C-new")
        case.denial.payer_name = "Northbeck Health Plan"
        assert bank.expected_response_days(case, published_window=30) >= 30

    def test_a_fast_payer_does_not_shorten_the_window(self, bank):
        for index in range(4):
            case = _case(f"C{index}", days_ago=5)
            bank.record_submission(case)
            bank.record_resolution(case, "overturned")

        assert bank.expected_response_days(_case("C-new"), published_window=30) == 30


class TestScoping:
    def test_an_agent_without_memory_access_is_refused(self):
        bank = MemoryBank(MemoryStore(), GatewayHandle(AgentName.DRAFTING))
        with pytest.raises(PolicyViolation):
            bank.recall("Northbeck Health Plan", None, "NBH-NMN-04")

    def test_the_collection_name_matches_the_gateway_policy(self):
        from core.gateway import POLICY

        assert MEMORY_COLLECTION in POLICY[AgentName.LIFECYCLE]


class TestKeying:
    def test_missing_facets_collapse_to_any(self):
        assert observation_key("P", None, None) == "P|any|any"

    def test_different_reason_codes_are_different_memories(self):
        assert observation_key("P", "POL", "A") != observation_key("P", "POL", "B")
