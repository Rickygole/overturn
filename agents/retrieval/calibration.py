"""What a retrieval score means, in figures a reader can check.

This module exists because of a specific failure. The audit trail printed

    retrieved NBH-CARD-014 (7 sections, 14 criteria) at similarity 0.092
    after reformulation

and a reader who knew what a cosine similarity is concluded, reasonably, that
0.092 is a near-orthogonal match and that retrieval had failed and got lucky on
a retry. Every step of that inference is sound. The premise it rests on -- that
these cosines live on the usual 0-to-1 scale where 0.9 is good and 0.1 is
noise -- is not, and nothing in the line said so.

**Why the numbers are small.** The score is a cosine between an L2-normalised
query vector and an L2-normalised whole-section vector. A denial letter is
~60-140 words of payer prose; a policy section is a document. The overlapping
terms are a small fraction of both norms, so the cosine is small *however
correct the match is*. It gets smaller as the query gets longer, which is the
opposite of getting worse. Measured on CASE-003, truncating the same denial and
retrieving the same, correct policy:

    first 5 words   0.235      first 40 words   0.101
    first 10 words  0.089      first 80 words   0.077
    first 20 words  0.059      all 98 words     0.092

The answer never changes. Only the norm does. A single number off that curve
cannot be read as a quality score, and printing one alone is a reporting
defect regardless of what it says.

**What the scale actually is.** Anchors on this corpus, from
``scripts/calibrate_retrieval.py`` and the probes in
``tests/test_retrieval.py::TestScoreScale``:

    a section quoted back to the index verbatim   ~0.88   (the practical ceiling)
    a policy title alone                          ~0.30
    unrelated English prose                       ~0.01   (the floor)
    a real denial against its governing policy    0.080 - 0.152
    a real denial against a policy that does not
      govern it (runner-up on the same case)      up to 0.046
    the best score on a denial no policy governs  0.012

So 0.092 is not "9% of a good match". It is inside the band every correct match
on this corpus occupies, roughly eight times the unrelated-text floor, and well
clear of the ceiling of the wrong-policy population.

**The statistic that actually discriminates.** The raw score does not separate
these populations nearly as well as the *ratio between the winner and the
runner-up* does. Across all eight cases:

    cases with a governing policy   winner beats runner-up by 1.7x to 7.3x
    the case with none (CASE-004)   winner beats runner-up by 1.09x

When a governing policy exists, one policy stands out. When none does, the
corpus returns a flat smear of near-identical weak scores, and the flatness is
the signal -- not the magnitude. That is why the audit line leads with the
ratio and prints the cosine behind it.

The constants below are measurements, not preferences. They are re-derived from
the corpus on every test run by ``tests/test_retrieval.py::TestScoreScale`` and
by ``scripts/calibrate_retrieval.py``, both of which fail if the corpus or the
scorer moves out from under them.
"""

from __future__ import annotations

from dataclasses import dataclass

# The observed band for a correct match, from the eight-case calibration run
# over the queries Retrieval actually builds. Printed as context so a single
# score can be placed on a scale instead of being read against an imagined one.
CORRECT_MATCH_BAND: tuple[float, float] = (0.07, 0.16)

# Cosine of an unrelated-English-prose query against its nearest policy. The
# empirical zero of this scale, which is nowhere near 0.0.
UNRELATED_TEXT_FLOOR = 0.01

# Cosine of a policy section quoted back to the index verbatim. The practical
# ceiling: no denial letter can score near this, because no denial letter is a
# copy of the policy.
VERBATIM_QUOTE_CEILING = 0.88

# Winner-to-runner-up ratios. On the measured set every case with a governing
# policy clears DECISIVE_RATIO or sits just under it, and the one case with no
# governing policy scores 1.09 -- the populations do not come close to touching,
# which is the whole reason this ratio is the headline figure.
DECISIVE_RATIO = 2.0
CLEAR_RATIO = 1.5


@dataclass(frozen=True)
class PolicyMargin:
    """A retrieval result placed against the alternatives it beat.

    ``score`` alone is the number that caused the misreading. This carries the
    comparators with it so no caller can print the bare cosine by accident.
    """

    policy_id: str
    score: float
    runner_up_id: str | None
    runner_up_score: float

    @property
    def margin(self) -> float:
        """Absolute gap to the best policy that is not the winner."""
        return self.score - self.runner_up_score

    @property
    def ratio(self) -> float | None:
        """How many times the runner-up the winner scored.

        ``None`` when nothing else matched at all, which is a stronger result
        than any finite ratio and must not be rendered as one.
        """
        if self.runner_up_id is None or self.runner_up_score <= 0.0:
            return None
        return self.score / self.runner_up_score

    @property
    def verdict(self) -> str:
        """Plain language, backed by the ratio, not by the raw cosine."""
        ratio = self.ratio
        if ratio is None:
            return "unopposed"
        if ratio >= DECISIVE_RATIO:
            return "decisive"
        if ratio >= CLEAR_RATIO:
            return "clear"
        return "contested"

    def against_runner_up(self) -> str:
        """"...x the next-best policy", or the unopposed case spelled out."""
        if self.runner_up_id is None or self.ratio is None:
            return "no other policy in the corpus matched at all"
        return (
            f"{self.ratio:.1f}x the next-best policy "
            f"({self.runner_up_id} at {self.runner_up_score:.3f})"
        )

    def against_floor(self, floor: float) -> str:
        """The score placed against the threshold it actually had to clear."""
        if floor <= 0:
            return f"clears a floor of {floor:.2f}"
        return f"{self.score / floor:.1f}x the {floor:.2f} no-policy floor"


def scale_note() -> str:
    """The one clause that would have prevented the misreading."""
    low, high = CORRECT_MATCH_BAND
    return (
        f"small by construction -- a short denial against a whole policy -- so on this "
        f"corpus correct matches measure {low:.2f}-{high:.2f}, unrelated text "
        f"~{UNRELATED_TEXT_FLOOR:.2f}, a verbatim quote ~{VERBATIM_QUOTE_CEILING:.2f}"
    )


def describe_match(margin: PolicyMargin, no_policy_floor: float) -> str:
    """The audit sentence for a retrieval that found a policy.

    Never emits a cosine without both comparators beside it: the floor it had to
    clear and the best policy it beat.
    """
    return (
        f"{margin.verdict} match: {margin.against_runner_up()}, "
        f"{margin.against_floor(no_policy_floor)} "
        f"[cosine {margin.score:.3f}; {scale_note()}]"
    )


def describe_decline(margin: PolicyMargin | None, no_policy_floor: float) -> str:
    """The audit sentence for a retrieval that declined to name a policy.

    Declining is a result, not an absence of one, and it needs the same context:
    what the best candidate was and how flat the field looked. A flat field is
    the positive evidence that nothing governs, and it is more informative than
    the magnitude of the top score.
    """
    if margin is None:
        return (
            "no policy in the corpus governs this denial: nothing matched on any term. "
            "Declining to appeal rather than appealing weakly"
        )
    flatness = (
        f"the field is flat -- {margin.against_runner_up()} -- which is what "
        f"'no governing policy' looks like: nothing stands out"
        if margin.ratio is not None and margin.ratio < CLEAR_RATIO
        else margin.against_runner_up()
    )
    return (
        f"no policy in the corpus governs this denial: best candidate "
        f"{margin.policy_id} at cosine {margin.score:.3f}, below the "
        f"{no_policy_floor:.2f} no-policy floor, and {flatness}. "
        f"Declining to appeal rather than appealing weakly"
    )
