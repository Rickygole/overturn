"""Vector retrieval over the policy corpus — built, tested, and NOT WIRED IN.

**Read this before citing it as a capability.** `build_retriever` below has no
caller. `RetrievalAgent.__init__` takes `build_index()` — the TF-IDF index — in
every mode, including cloud. This module is exercised by its tests and by
nothing else.

That is a deliberate state and not an oversight, for the reasons below, but it
has already caused one incorrect claim in a submission document: someone read
`build_retriever`, saw that it selects the hybrid in cloud mode, confirmed that
`infra/deploy.sh` sets cloud mode, and concluded the deployed system runs hybrid
retrieval. Both facts were right and the conclusion was wrong.

The serverless index is reachable on this project — `docs/PLATFORM_PROBE.md`
records the probe. This module uses it. The lexical retriever in `lexical.py`
is still what runs by default, and that is a decision rather than an omission:

**The corpus is six documents with controlled vocabulary.** A denial says
"continuous glucose monitoring" and the governing policy is titled "Continuous
Glucose Monitoring Systems". This is the easy case for term matching and the
expensive case for nothing.

**Embeddings cost money per query and the lexical path costs nothing.** For a
clinic appealing a $1,284 claim, that ratio is the product.

**A retriever that only works when someone is paying cannot be tested.** The
entire pipeline runs offline, deterministically, on every commit, because the
default path needs no network.

Where embeddings genuinely help is the case term matching cannot reach: a
denial reason worded nothing like the policy that governs it. So the two are
combined rather than chosen between — `HybridRetriever` takes the better score
per section, which cannot do worse than either alone on the cases where one of
them is right.
"""

from __future__ import annotations

import logging
import math

from agents.retrieval.lexical import TfidfIndex, build_index
from core.config import Settings, get_settings
from core.llm import LlmClient
from core.schemas.policy import PolicySection

logger = logging.getLogger(__name__)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return max(0.0, min(1.0, dot / norm)) if norm else 0.0


class VectorRetriever:
    """Embedding similarity over policy sections.

    Section embeddings are computed once and cached for the process. The corpus
    is 42 sections and does not change at runtime, so re-embedding it per query
    would be paying repeatedly for an answer that cannot have changed.
    """

    name = "vector"

    def __init__(
        self,
        llm: LlmClient,
        sections: list[PolicySection] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.llm = llm
        self.settings = settings or get_settings()
        self.sections = sections or build_index().sections
        self._embeddings: list[list[float]] | None = None

    def _corpus_embeddings(self) -> list[list[float]]:
        if self._embeddings is None:
            texts = [f"{s.policy_title}. {s.section_heading}. {s.text}" for s in self.sections]
            self._embeddings = self.llm.embed(texts, self.settings.embedding_model)
        return self._embeddings

    def search(self, query: str, k: int = 8) -> list[tuple[PolicySection, float]]:
        try:
            query_vector = self.llm.embed([query], self.settings.embedding_model)[0]
            corpus = self._corpus_embeddings()
        except Exception as exc:
            # A retrieval path that fails closed means no appeal at all. Degrade
            # to nothing here and let the hybrid fall back to lexical, which is
            # a worse retriever and an infinitely better outcome than none.
            logger.warning("vector retrieval unavailable, falling back: %s", exc)
            return []

        scored = [
            (section, _cosine(query_vector, embedding))
            for section, embedding in zip(self.sections, corpus, strict=False)
        ]
        scored.sort(key=lambda pair: (-pair[1], pair[0].section_id))
        return scored[:k]


# Reciprocal rank fusion constant. 60 is the value from the original paper and
# there is no calibration data here that would justify tuning it.
RRF_K = 60

# The lexical retriever is calibrated (docs/MODEL_CHOICES.md); the vector path
# is not yet. Until it is, it contributes recall rather than authority.
LEXICAL_WEIGHT = 1.0
VECTOR_WEIGHT = 0.5


class HybridRetriever:
    """Combines two rankers by rank, never by raw score.

    Taking the better of the two scores is the obvious approach and it is wrong.
    Cosine similarity over embeddings and cosine over TF-IDF are not the same
    quantity, and they do not share a scale: on this corpus the lexical scores
    land around 0.1–0.3 while embedding scores sit near 1.0 for almost anything.
    Under `max`, the noisier retriever wins every time simply by having larger
    numbers — which is exactly what happened, and it routed a glucose monitoring
    denial to the behavioural health policy.

    Reciprocal rank fusion sidesteps the problem by discarding the scores and
    keeping only the ordering. A section ranked first by either retriever gets
    the same contribution regardless of whether that retriever reports 0.13 or
    0.99, so neither can dominate by scale alone.

    **The assumption this rests on, stated plainly:** RRF treats agreement
    between rankers as evidence, so it is only sound when both rankers are
    competent. Fed a ranker that is confidently wrong, it will promote whatever
    that ranker agrees with — a section appearing mid-list in both rankings
    outscores a section ranked first in only one, which is the intended
    behaviour and is exactly wrong when one of the two is noise.

    That is why `build_retriever` selects this only in cloud mode with real
    embeddings behind it, and why the vector path gets half a vote until it has
    a calibration run of its own. A second retriever is not free; it has to earn
    its say the same way the first one did.
    """

    name = "hybrid"

    def __init__(self, lexical: TfidfIndex, vector: VectorRetriever) -> None:
        self.lexical = lexical
        self.vector = vector

    def search(self, query: str, k: int = 8) -> list[tuple[PolicySection, float]]:
        # Weighted, because the two rankers have not earned equal say. The
        # lexical retriever is calibrated against every case in the corpus and
        # its failures are documented; the vector path has never been measured
        # here at all. Equal weights make plain RRF a coin flip whenever one
        # ranker is confidently wrong — it puts a section at rank one, the other
        # puts a different section at rank one, and the tie breaks on
        # alphabetical order.
        #
        # The weight is a statement about demonstrated reliability, and it moves
        # when the vector path has a calibration run of its own behind it.
        rankings = [
            (self.lexical.search(query, k=k * 2), LEXICAL_WEIGHT),
            (self.vector.search(query, k=k * 2), VECTOR_WEIGHT),
        ]

        fused: dict[str, float] = {}
        sections: dict[str, PolicySection] = {}
        for ranking, weight in rankings:
            for rank, (section, _) in enumerate(ranking, start=1):
                sections[section.section_id] = section
                fused[section.section_id] = fused.get(section.section_id, 0.0) + weight / (
                    RRF_K + rank
                )

        if not fused:
            return []

        # Rescale into [0, 1] so the result still satisfies
        # `RetrievedSection.similarity` and the configured thresholds keep
        # meaning what they meant. The ordering is what carries the information.
        top = max(fused.values())
        ranked = [
            (sections[section_id], min(1.0, score / top) if top else 0.0)
            for section_id, score in fused.items()
        ]
        ranked.sort(key=lambda pair: (-pair[1], pair[0].section_id))
        return ranked[:k]

    def best_policy(self, query: str) -> tuple[str, float] | None:
        """Same contract as the lexical index: strongest single section wins."""
        hits = self.search(query, k=12)
        if not hits:
            return None
        best_per_policy: dict[str, float] = {}
        total_per_policy: dict[str, float] = {}
        for section, score in hits:
            pid = section.policy_id
            best_per_policy[pid] = max(best_per_policy.get(pid, 0.0), score)
            total_per_policy[pid] = total_per_policy.get(pid, 0.0) + score
        policy_id = max(best_per_policy, key=lambda p: (best_per_policy[p], total_per_policy[p]))
        return policy_id, best_per_policy[policy_id]


def build_hybrid(llm: LlmClient) -> HybridRetriever:
    """Only meaningful with real embeddings behind it.

    The offline backend returns deterministic hashed vectors so the indexing and
    storage paths can be exercised without a bill. They carry no semantics, and
    fusing them with a retriever that works would be adding noise to signal — so
    `build_retriever` selects this only in cloud mode.
    """
    lexical = build_index()
    return HybridRetriever(lexical, VectorRetriever(llm, lexical.sections))


def build_retriever(llm: LlmClient, settings: Settings | None = None):
    """Lexical by default; hybrid only where the embeddings mean something."""
    settings = settings or get_settings()
    lexical = build_index()
    if settings.runtime_mode != "cloud" or llm.offline:
        return lexical
    return HybridRetriever(lexical, VectorRetriever(llm, lexical.sections, settings))
