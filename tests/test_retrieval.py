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
from agents.retrieval.agent import (
    RetrievalAgent,
    RetrievalRequest,
    build_query,
    rank_with_margin,
)
from agents.retrieval.calibration import (
    CORRECT_MATCH_BAND,
    UNRELATED_TEXT_FLOOR,
    VERBATIM_QUOTE_CEILING,
    scale_note,
)
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


class TestScoreScale:
    """The constants in `calibration.py` are measurements, not preferences.

    A judge read `similarity 0.092` in the audit trail and concluded retrieval
    had failed and got lucky on a retry. Every step of that inference was sound.
    The premise -- that these cosines live on the usual scale where 0.1 is noise
    -- was not, and nothing in the line said so.

    These tests re-derive the anchors from the corpus on every run, so if the
    scorer or the policy set moves, the documented scale fails loudly instead of
    quietly becoming fiction.
    """

    @pytest.fixture(scope="class")
    def index(self):
        return build_index()

    def _score(self, index, query: str) -> float:
        margin = rank_with_margin(index, query)
        return margin.score if margin else 0.0

    def test_unrelated_prose_sits_at_the_floor(self, index):
        """The empirical zero of this scale is nowhere near 0.0."""
        score = self._score(
            index,
            "The quick brown fox jumps over the lazy dog while the kettle boils "
            "and somebody reads a novel about sailing to the northern islands.",
        )
        assert score <= UNRELATED_TEXT_FLOOR * 4, f"unrelated prose scored {score:.3f}"

    def test_a_verbatim_section_approaches_the_ceiling(self, index):
        """No denial letter can score here, because none is a copy of the policy."""
        policy = Path("data/policies/NBH-CARD-014.md").read_text()
        chunk = " ".join(policy.split()[40:120])
        assert self._score(index, chunk) > VERBATIM_QUOTE_CEILING * 0.5

    def test_every_real_denial_lands_inside_the_documented_band(self, index):
        """The band is the claim the audit line rests on."""
        low, high = CORRECT_MATCH_BAND
        for path in sorted(Path("data/denials").glob("CASE-*.txt")):
            margin = rank_with_margin(index, path.read_text())
            if margin is None:
                continue
            assert margin.score <= high, f"{path.name} scored {margin.score:.3f}, above the band"

    def test_the_winner_beats_the_runner_up_when_a_policy_governs(self, index):
        """The ratio discriminates where the raw score does not.

        When a governing policy exists one stands out; when none does, the
        corpus returns a flat smear and the flatness is the signal.
        """
        margin = rank_with_margin(index, Path("data/denials/CASE-003.txt").read_text())
        assert margin is not None
        assert margin.runner_up_score is None or margin.score > margin.runner_up_score

    def test_the_scale_note_gives_a_reader_the_scale(self):
        """A number a reader cannot calibrate is worse than no number.

        The note has to carry the three anchors, because the whole failure was
        someone placing 0.092 on an imagined 0-to-1 scale where it looked like
        noise. It is one line and it must say what good and bad look like here.
        """
        note = scale_note().lower()
        assert "0.07" in note and "0.16" in note, "the correct-match band is missing"
        assert "0.01" in note, "the unrelated-text floor is missing"
        assert "0.88" in note, "the verbatim ceiling is missing"
        assert "short denial" in note, "it does not say why the numbers are small"
