"""Lexical retrieval over the policy corpus.

There is a vector index available on this project and it is used when one is
configured. This module exists anyway, for three reasons that are all worth
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

# Deliberately short. An aggressive stop list on a corpus this small removes
# signal: "not", "or" and "all" carry real meaning in coverage criteria, where
# "all of the following" and "any of the following" are the whole distinction.
STOPWORDS: frozenset[str] = frozenset(
    """
    a an and are as at be been by for from has have in is it its of on or that
    the this to was were will with which within where when been being
    """.split()
)


def tokenize(text: str) -> list[str]:
    """Lowercase, split on word boundaries, drop stopwords and bare digits."""
    return [
        token
        for token in TOKEN_RE.findall(text.lower())
        if token not in STOPWORDS and not token.isdigit()
    ]


def features(text: str) -> list[str]:
    """Unigrams plus adjacent bigrams.

    Unigrams alone cannot tell these two policies apart. "Magnetic", "resonance"
    and "imaging" all appear in both the cardiac imaging policy and the lumbar
    spine policy, and on a corpus this small they carry almost no inverse
    document frequency. The phrase "cardiac magnetic" appears in exactly one of
    them.

    This was not a guess. `scripts/calibrate_retrieval.py` retrieved the lumbar
    policy for a cardiac MRI denial, and bigrams are the smallest honest fix —
    the alternative was lowering a threshold until the wrong answer counted as
    right.
    """
    unigrams = tokenize(text)
    bigrams = [f"{a}_{b}" for a, b in zip(unigrams, unigrams[1:], strict=False)]
    return unigrams + bigrams


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
            term: (1.0 + math.log(count)) * self._idf(term)
            for term, count in counts.items()
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

        by_policy: dict[str, float] = {}
        for section, score in hits:
            by_policy[section.policy_id] = by_policy.get(section.policy_id, 0.0) + score

        policy_id = max(by_policy, key=lambda p: by_policy[p])
        top = max(score for section, score in hits if section.policy_id == policy_id)
        return policy_id, top


@lru_cache(maxsize=1)
def build_index() -> TfidfIndex:
    """The corpus index, built once per process."""
    from agents.retrieval.corpus import load_corpus

    return TfidfIndex(load_corpus())
