"""Lexical retrieval over the policy corpus.

A vector path exists in `agents/retrieval/vector.py` and is selected in cloud
mode. This module is the default anyway, for three reasons that are all worth
more than they cost:

  * The pipeline has to run with no network and no bill. A retriever that only
    works against a hosted index means the end-to-end run only works when
    someone is paying.
  * Policy retrieval is unusually well suited to term matching. A denial says
    "continuous glucose monitoring" and the governing policy is titled
    "Continuous Glucose Monitoring Systems". The vocabulary is controlled and
    the corpus is small; this is the easy case for TF-IDF and the hard case for
    nothing.
  * It is a fallback with a known failure mode. When the vector index is
    unreachable, degrading to term matching is better than degrading to no
    appeal.

Scores are cosine similarities between L2-normalised non-negative vectors, so
they land natively in ``[0, 1]`` and satisfy ``RetrievedSection.similarity``
without a rescaling fudge.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from functools import lru_cache

from core.schemas.policy import PolicySection

TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-.][a-z0-9]+)*")

# How far apart two words may be and still count as a pair. Three is enough to
# span "cardiac magnetic resonance imaging" end to end and short enough that
# unrelated words in the same sentence do not start matching each other.
PAIR_WINDOW = 3

# Deliberately short. An aggressive stop list on a corpus this small removes
# signal: "not", "or" and "all" carry real meaning in coverage criteria, where
# "all of the following" and "any of the following" are the whole distinction.
STOPWORDS: frozenset[str] = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "will",
        "with",
        "which",
        "within",
        "where",
        "when",
        "been",
        "being",
    ]
)


def tokenize(text: str) -> list[str]:
    """Lowercase, split on word boundaries, drop stopwords and bare digits."""
    return [
        token
        for token in TOKEN_RE.findall(text.lower())
        if token not in STOPWORDS and not token.isdigit()
    ]


def features(text: str) -> list[str]:
    """Unigrams plus order-independent pairs within a small window.

    Unigrams alone cannot tell two policies apart here. "Magnetic", "resonance"
    and "imaging" all appear in both the cardiac imaging policy and the lumbar
    spine policy, and across six documents they carry almost no inverse document
    frequency. The pair {cardiac, imaging} appears in exactly one of them.

    The pairs are unordered and span a window rather than being strict adjacent
    bigrams, because the two sides of this match are written by different people
    for different purposes. A CPT descriptor says "Magnetic resonance imaging,
    cardiac, with contrast". The policy it should match says "cardiac magnetic
    resonance imaging". Ordered adjacent bigrams share nothing between those two
    strings; unordered window pairs share three.

    None of this was guesswork. `scripts/calibrate_retrieval.py` retrieved the
    lumbar policy for a cardiac MRI denial twice — once with unigrams, once with
    ordered bigrams — and this is the change that fixed it without touching a
    threshold.
    """
    unigrams = tokenize(text)
    pairs: list[str] = []
    for i, first in enumerate(unigrams):
        for second in unigrams[i + 1 : i + 1 + PAIR_WINDOW]:
            if first != second:
                low, high = sorted((first, second))
                pairs.append(f"{low}|{high}")
    return unigrams + pairs


class TfidfIndex:
    """A small in-memory TF-IDF index over policy sections."""

    def __init__(self, sections: list[PolicySection]) -> None:
        self.sections = sections
        self._docs: list[dict[str, float]] = []
        self._df: Counter[str] = Counter()

        raw_counts: list[Counter[str]] = []
        for section in sections:
            counts = Counter(features(self._document_text(section)))
            raw_counts.append(counts)
            self._df.update(counts.keys())

        self._n = max(1, len(sections))
        for counts in raw_counts:
            self._docs.append(self._weight(counts))

    @staticmethod
    def _document_text(section: PolicySection) -> str:
        """Title and heading carry disproportionate signal, so weight them.

        Repeating them three times is crude and it is also the right amount of
        machinery for a six-document corpus. A denial naming the service is
        matching against the policy title far more than against its prose.
        """
        heading = f"{section.policy_title} {section.section_heading} "
        return heading * 3 + section.text

    def _idf(self, term: str) -> float:
        df = self._df.get(term, 0)
        return math.log((self._n - df + 0.5) / (df + 0.5) + 1.0)

    def _weight(self, counts: Counter[str]) -> dict[str, float]:
        """Sublinear term frequency times IDF, L2-normalised."""
        weights = {
            term: (1.0 + math.log(count)) * self._idf(term) for term, count in counts.items()
        }
        norm = math.sqrt(sum(w * w for w in weights.values())) or 1.0
        return {term: w / norm for term, w in weights.items()}

    def search(self, query: str, k: int = 8) -> list[tuple[PolicySection, float]]:
        """Return the ``k`` best sections with their cosine similarity."""
        query_weights = self._weight(Counter(features(query)))
        if not query_weights:
            return []

        scored: list[tuple[PolicySection, float]] = []
        for section, doc in zip(self.sections, self._docs, strict=True):
            # Iterate the shorter side; queries are far shorter than sections.
            score = sum(weight * doc.get(term, 0.0) for term, weight in query_weights.items())
            if score > 0:
                scored.append((section, min(1.0, score)))

        scored.sort(key=lambda pair: (-pair[1], pair[0].section_id))
        return scored[:k]

    def best_policy(self, query: str) -> tuple[str, float] | None:
        """The policy whose sections score highest in aggregate.

        Retrieval returns whole policies rather than isolated top-k sections,
        because Mapping needs the complete criteria set for a policy to render
        a verdict on each criterion. A criteria list truncated by rank is a
        criteria list with silent holes in it.
        """
        hits = self.search(query, k=12)
        if not hits:
            return None

        # Rank by the single best-matching section, with the aggregate only as a
        # tiebreak. Summing is the obvious thing and it is wrong: a policy with
        # six mediocre sections outscores a policy with one excellent one, so a
        # cardiac MRI denial gets routed to the lumbar spine policy because that
        # policy happens to have more sections in the top k. The strongest single
        # match is the signal; the rest is corpus shape.
        best_per_policy: dict[str, float] = {}
        total_per_policy: dict[str, float] = {}
        for section, score in hits:
            pid = section.policy_id
            best_per_policy[pid] = max(best_per_policy.get(pid, 0.0), score)
            total_per_policy[pid] = total_per_policy.get(pid, 0.0) + score

        policy_id = max(
            best_per_policy,
            key=lambda p: (best_per_policy[p], total_per_policy[p]),
        )
        return policy_id, best_per_policy[policy_id]


@lru_cache(maxsize=1)
def build_index() -> TfidfIndex:
    """The corpus index, built once per process."""
    from agents.retrieval.corpus import load_corpus

    return TfidfIndex(load_corpus())
