"""Tests for policy retrieval.

Retrieval decides which policy the whole case is argued against. If it picks
the wrong policy, every downstream agent does careful, well-cited, completely
irrelevant work — and Verification will not catch it, because the citations
will all resolve correctly against the wrong document.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.base import build_deps
from agents.retrieval.agent import RetrievalAgent, RetrievalRequest, build_query
from agents.retrieval.lexical import build_index, features, tokenize
from core.config import get_settings
from core.llm import LlmClient, ScriptedBackend
from core.schemas.denial import DenialExtraction, DeniedService
from core.schemas.enums import AgentName
from core.schemas.policy import RetrievalResult
from core.store import MemoryStore

REPO = Path(__file__).resolve().parents[1]
CASES = {c["case_id"]: c for c in json.loads((REPO / "data" / "cases.json").read_text())["cases"]}


def _denial(description: str, code: str, reason: str) -> DenialExtraction:
    return DenialExtraction(
        payer_name="Northbeck Health Plan",
        denial_reason_text=reason,
        services=[DeniedService(description=description, procedure_code=code)],
    )


def _agent(backend: ScriptedBackend | None = None) -> RetrievalAgent:
    backend = backend or ScriptedBackend()
    backend.register(
        "retrieval",
        "reformulate",
        lambda req: RetrievalResult(query="x", reformulated_query=None),
    )
    return RetrievalAgent(build_deps(MemoryStore(), AgentName.RETRIEVAL, LlmClient(backend)))


class TestPolicyDiscrimination:
    @pytest.mark.parametrize(
        "description,code,reason,expected",
        [
            (
                "Continuous glucose monitoring system, receiver and transmitter",
                "E2103",
                "Documentation does not establish an intensive insulin regimen.",
                "NBH-ENDO-031",
            ),
            (
                "Magnetic resonance imaging, lumbar spine, without contrast",
                "72148",
                "Documentation does not establish a six week trial of conservative management.",
                "NBH-MSK-022",
            ),
            (
                "Magnetic resonance imaging, cardiac, with contrast",
                "75561",
                "Documentation does not establish the initial evaluation was inconclusive.",
                "NBH-CARD-014",
            ),
            (
                "Continuous positive airway pressure device and supplies",
                "E0601",
                "Documentation does not establish a qualifying apnea-hypopnea index.",
                "NBH-PULM-008",
            ),
            (
                "Intensive outpatient programme, behavioral health",
                "S9480",
                "Documentation does not establish that a less intensive level of care failed.",
                "NBH-BEHV-045",
            ),
            (
                "Comprehensive genomic profiling, solid tumour, 500 genes",
                "0037U",
                "Documentation does not establish advanced or metastatic disease.",
                "NBH-ONCO-063",
            ),
        ],
    )
    def test_each_service_retrieves_its_own_policy(self, description, code, reason, expected):
        result = _agent().run("t", RetrievalRequest(denial=_denial(description, code, reason)))
        assert {s.policy_id for s in result.sections} == {expected}

    def test_two_mri_policies_are_not_confused(self):
        """The hard case: shared vocabulary, different anatomy.

        'Magnetic resonance imaging' appears in both the cardiac and the lumbar
        policy. Only the qualifier distinguishes them, and an earlier version of
        the ranking got this wrong.
        """
        cardiac = _agent().run(
            "t",
            RetrievalRequest(
                denial=_denial(
                    "Magnetic resonance imaging, cardiac, with contrast", "75561", "denied"
                )
            ),
        )
        lumbar = _agent().run(
            "t",
            RetrievalRequest(
                denial=_denial(
                    "Magnetic resonance imaging, lumbar spine, without contrast",
                    "72148",
                    "denied",
                )
            ),
        )
        assert {s.policy_id for s in cardiac.sections} == {"NBH-CARD-014"}
        assert {s.policy_id for s in lumbar.sections} == {"NBH-MSK-022"}


class TestClosedWorld:
    def test_every_section_of_the_policy_is_returned(self):
        """Mapping needs the complete criteria set, not the top-scoring slice."""
        result = _agent().run(
            "t",
            RetrievalRequest(
                denial=_denial("Continuous glucose monitoring system", "E2103", "denied")
            ),
        )
        from agents.retrieval.corpus import load_corpus

        expected = {s.section_id for s in load_corpus() if s.policy_id == "NBH-ENDO-031"}
        assert {s.section_id for s in result.sections} == expected

    def test_criteria_survive_retrieval(self):
        result = _agent().run(
            "t",
            RetrievalRequest(
                denial=_denial("Continuous glucose monitoring system", "E2103", "denied")
            ),
        )
        assert sum(len(s.criteria) for s in result.sections) >= 8
        assert "NBH-ENDO-031-3.4" in result.section_ids()

    def test_verbatim_text_is_preserved(self):
        result = _agent().run(
            "t",
            RetrievalRequest(
                denial=_denial("Continuous positive airway pressure device", "E0601", "denied")
            ),
        )
        section = next(s for s in result.sections if s.section_id == "NBH-PULM-008-3")
        # The stored text keeps the document's own line wrapping, which is what
        # "verbatim" has to mean if Verification is going to quote from it.
        assert "apnea-hypopnea index of 15 or more events per" in section.text


class TestDeclining:
    def test_a_denial_with_no_governing_policy_is_declined(self):
        """The honest outcome is to decline, not to appeal weakly."""
        result = _agent().run(
            "t",
            RetrievalRequest(
                denial=_denial(
                    "Cosmetic dermabrasion", "15780", "Cosmetic procedures are not covered."
                )
            ),
        )
        assert result.no_applicable_policy is True
        assert result.sections == []
        assert result.top_similarity < get_settings().retrieval_no_policy_floor


class TestReformulation:
    def test_reformulation_runs_only_when_the_first_search_is_weak(self):
        backend = ScriptedBackend()
        backend.register(
            "retrieval",
            "reformulate",
            lambda req: RetrievalResult(query="x", reformulated_query="rewritten"),
        )
        agent = RetrievalAgent(build_deps(MemoryStore(), AgentName.RETRIEVAL, LlmClient(backend)))
        denial = _denial(
            "CPAP continuous positive airway pressure device and supplies",
            "E0601",
            "obstructive sleep apnea apnea-hypopnea index sleep study "
            "documentation does not establish a qualifying index",
        )
        # Assert the premise before asserting the behaviour. A test that says
        # "a strong match does not reformulate" is worthless if the query it
        # uses is not actually a strong match.
        score = build_index().best_policy(build_query(denial))
        assert score is not None
        assert score[1] > get_settings().retrieval_score_floor, (
            f"test premise broken: this query scores {score[1]:.3f}, at or below the "
            f"floor of {get_settings().retrieval_score_floor}"
        )

        agent.run("t", RetrievalRequest(denial=denial))
        assert backend.calls == [], "reformulated a query that already scored well"

    def test_reformulation_runs_when_the_first_search_is_weak(self):
        """The other half. A path that never fires is untested code."""
        backend = ScriptedBackend()
        backend.register(
            "retrieval",
            "reformulate",
            lambda req: RetrievalResult(query="x", reformulated_query="rewritten"),
        )
        agent = RetrievalAgent(build_deps(MemoryStore(), AgentName.RETRIEVAL, LlmClient(backend)))
        agent.run(
            "t",
            RetrievalRequest(
                denial=_denial(
                    "Magnetic resonance imaging, cardiac, with contrast", "75561", "denied"
                )
            ),
        )
        assert len(backend.calls) == 1

    def test_a_failed_reformulation_does_not_fail_retrieval(self):
        backend = ScriptedBackend()  # no handler registered -> raises
        agent = RetrievalAgent(build_deps(MemoryStore(), AgentName.RETRIEVAL, LlmClient(backend)))
        result = agent.run(
            "t",
            RetrievalRequest(denial=_denial("Cardiac MRI", "75561", "denied as not necessary")),
        )
        assert result is not None

    def test_only_the_query_is_read_from_the_model_response(self):
        """A model that fabricates a section list must have no effect."""
        from core.schemas.policy import RetrievedSection

        fabricated = RetrievedSection(
            section_id="NBH-FAKE-999-1",
            policy_id="NBH-FAKE-999",
            policy_title="Invented",
            section_heading="Invented",
            text="This section does not exist anywhere in the corpus at all.",
            similarity=1.0,
            matched_query="q",
        )
        backend = ScriptedBackend()
        backend.register(
            "retrieval",
            "reformulate",
            lambda req: RetrievalResult(
                query="x",
                reformulated_query="cardiac magnetic resonance imaging",
                sections=[fabricated],
                top_similarity=1.0,
            ),
        )
        agent = RetrievalAgent(build_deps(MemoryStore(), AgentName.RETRIEVAL, LlmClient(backend)))
        result = agent.run("t", RetrievalRequest(denial=_denial("Cardiac MRI", "75561", "denied")))
        assert "NBH-FAKE-999-1" not in result.section_ids()


class TestTokenizer:
    def test_bigrams_are_produced(self):
        assert "cardiac|magnetic" in features("cardiac magnetic resonance")

    def test_pairs_are_order_independent(self):
        """A CPT descriptor and a policy title word the same service differently."""
        assert "cardiac|imaging" in features("magnetic resonance imaging, cardiac")
        assert "cardiac|imaging" in features("cardiac magnetic resonance imaging")

    def test_meaningful_short_words_survive_the_stoplist(self):
        """'All of the following' and 'any of the following' differ by one word."""
        assert "all" in tokenize("all of the following criteria")
        assert "any" in tokenize("any of the following criteria")
        assert "not" in tokenize("not medically necessary")


class TestIndexIsShared:
    def test_index_is_built_once(self):
        assert build_index() is build_index()
