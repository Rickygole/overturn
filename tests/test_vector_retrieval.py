"""Tests for the hybrid retriever.

Two properties are being defended, and they pull against each other. The fusion
has to let a second retriever contribute something the first missed. It must not
let a scale mismatch hand the decision to whichever retriever reports bigger
numbers, which is what taking the better of two scores did — it routed a glucose
monitoring denial to the behavioural health policy.
"""

from __future__ import annotations

from agents.retrieval.lexical import build_index
from agents.retrieval.vector import (
    HybridRetriever,
    VectorRetriever,
    build_retriever,
)
from core.config import Settings
from core.llm import LlmClient
from core.schemas.policy import PolicySection

QUERY = (
    "continuous glucose monitoring system denied not medically necessary intensive insulin regimen"
)


class _Ranker:
    """A stand-in retriever returning a fixed policy's sections."""

    def __init__(self, policy_id: str, score: float = 0.99) -> None:
        self.policy_id = policy_id
        self.score = score

    def search(self, query: str, k: int = 8) -> list[tuple[PolicySection, float]]:
        return [(s, self.score) for s in build_index().sections if s.policy_id == self.policy_id][
            :k
        ]


class _Unavailable:
    def search(self, query: str, k: int = 8) -> list[tuple[PolicySection, float]]:
        return []


class TestScaleCannotDecide:
    def test_a_high_scoring_agreeing_ranker_does_not_override_by_size_alone(self):
        """The bug that motivated rank fusion.

        The second ranker here reports 0.99 on everything while lexical reports
        around 0.13. Under `max` the second one won outright. Under rank fusion
        it agrees with lexical and the right answer survives.
        """
        lexical = build_index()
        hybrid = HybridRetriever(lexical, _Ranker("NBH-ENDO-031", score=0.99))
        assert hybrid.best_policy(QUERY)[0] == "NBH-ENDO-031"

    def test_scores_stay_within_the_contract(self):
        lexical = build_index()
        hybrid = HybridRetriever(lexical, _Ranker("NBH-ENDO-031"))
        for _, score in hybrid.search(QUERY, k=8):
            assert 0.0 <= score <= 1.0


class TestDegradation:
    def test_an_unavailable_index_falls_back_to_lexical(self):
        """A retrieval path that fails closed means no appeal at all."""
        lexical = build_index()
        hybrid = HybridRetriever(lexical, _Unavailable())
        assert hybrid.best_policy(QUERY)[0] == lexical.best_policy(QUERY)[0]

    def test_an_embedding_failure_is_caught_not_raised(self):
        class Broken:
            name = "broken"

            def embed(self, texts, model):
                raise RuntimeError("index unreachable")

            def invoke(self, request):
                raise RuntimeError("index unreachable")

        retriever = VectorRetriever(LlmClient(Broken()), build_index().sections)
        assert retriever.search(QUERY) == []


class TestSelection:
    def test_offline_uses_the_lexical_retriever(self):
        """Offline embeddings are hashed and carry no semantics.

        Fusing them with a retriever that works would be adding noise to signal,
        so the hybrid is not selected where they are all that is available.
        """
        from agents.offline.handlers import build_offline_llm

        retriever = build_retriever(build_offline_llm(), Settings())
        assert type(retriever).__name__ == "TfidfIndex"

    def test_cloud_mode_with_a_real_backend_selects_the_hybrid(self):
        class Real:
            name = "vertex"

            def embed(self, texts, model):
                return [[0.0] * 8 for _ in texts]

            def invoke(self, request):
                raise NotImplementedError

        retriever = build_retriever(LlmClient(Real()), Settings(runtime_mode="cloud"))
        assert isinstance(retriever, HybridRetriever)
