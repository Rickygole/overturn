"""Measure retrieval scores across every case, so the thresholds are evidence.

There are two thresholds in the retrieval path and they answer different
questions:

  ``retrieval_score_floor``      below this, reformulate the query and try again
  ``retrieval_no_policy_floor``  below this, no policy in the corpus governs
                                 this denial, and the honest action is to
                                 decline to appeal rather than appeal weakly

Setting them by intuition produces a retriever that works on the cases someone
happened to try. This script prints the actual distribution -- correct-policy
scores against wrong-policy scores -- and the constants are then set from the
gap between them. If the gap is narrow, that is a retrieval problem to fix, not
a constant to nudge.

**The queries are the real ones.** An earlier version of this script carried a
hand-written approximation of each case's query, in a dict, under a comment
saying it was "written as Retrieval will build it". It was not. Measured
against the letters, that dict overstated CASE-004 -- the one case with no
governing policy -- by a factor of four (0.048 against the true 0.012), which
made the separation between the two populations look half as wide as it is.
The script's own comment already said the lesson: a measurement harness that
does not exercise the real code path measures the harness. It had stopped
reimplementing the *ranking* and was still reimplementing the *query*.

So the queries below are built by parsing the actual denial letters in
``data/denials/`` and running ``agents.retrieval.agent.build_query`` over the
result -- the same function the agent calls.

**One caveat, stated because it cannot be tested away.** The parse uses the
offline ``intake.extract`` handler, a real regex parser over the letter. In
cloud mode Intake is Gemini, and if it extracts a different service description
or reason text the query differs and so do these numbers. What would settle it
is dumping ``RetrievalResult.query`` from a cloud run and diffing it against
the query printed here; on CASE-003 the deployed run scored 0.092 and this
harness scores the first-pass query at 0.092, so on that case they agree.

Run after any change to the corpus, the scorer, or the letters::

    uv run python scripts/calibrate_retrieval.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agents.offline.handlers import intake_extract  # noqa: E402
from agents.retrieval.agent import build_query  # noqa: E402
from agents.retrieval.calibration import (  # noqa: E402
    CLEAR_RATIO,
    CORRECT_MATCH_BAND,
    DECISIVE_RATIO,
    UNRELATED_TEXT_FLOOR,
    VERBATIM_QUOTE_CEILING,
)
from agents.retrieval.lexical import build_index  # noqa: E402
from core.config import get_settings  # noqa: E402
from core.llm import LlmRequest  # noqa: E402

UNRELATED_PROSE = "the quick brown fox jumps over the lazy dog while eating a sandwich in paris"


def case_queries() -> dict[str, str]:
    """The query each case actually produces, through the real builder."""
    queries: dict[str, str] = {}
    for letter in sorted((REPO / "data" / "denials").glob("CASE-*.txt")):
        request = LlmRequest(
            agent="intake",
            operation="extract",
            system="",
            prompt=letter.read_text(),
            model="offline",
        )
        queries[letter.stem] = build_query(intake_extract(request))
    return queries


def expected_policies() -> dict[str, str | None]:
    manifest = json.loads((REPO / "data" / "cases.json").read_text())
    return {c["case_id"]: c["policy_id"] for c in manifest["cases"]}


def print_scale(index) -> None:
    """What the numbers mean, before any of them are shown.

    A cosine between a short denial and a whole policy is small however correct
    it is. Printing the scale first is the difference between a reader who can
    calibrate the table below and a reader who assumes 0.1 is noise -- and the
    second reader is not being unreasonable, they are being uninformed by us.
    """
    section = next(s for s in index.sections if s.section_id == "NBH-CARD-014-1")
    verbatim = index.best_policy(section.text)
    title = index.best_policy("Advanced Cardiac Imaging")
    unrelated = index.best_policy(UNRELATED_PROSE)

    print("what this scale is")
    print("-" * 78)
    print(f"  a policy section quoted back verbatim  {verbatim[1]:.3f}   (practical ceiling)")
    print(f"  a policy title alone                   {title[1]:.3f}")
    print(f"  unrelated English prose                {unrelated[1]:.3f}   (empirical floor)")

    # The length effect, which is the whole reason a raw score is unreadable.
    long_query = case_queries()["CASE-003"]
    words = long_query.split()
    trail = "  ".join(
        f"{n}w {index.best_policy(' '.join(words[:n]))[1]:.3f}" for n in (5, 20, 40, len(words))
    )
    print(f"  same CASE-003 denial, truncated        {trail}")
    print(
        "  ^ the correct policy is retrieved at every one of those lengths. The score "
        "moves by 4x\n    across them anyway, because the query norm moves. A single "
        "score off this curve\n    is not a quality measurement."
    )
    print()


def main() -> int:
    index = build_index()
    expected = expected_policies()
    settings = get_settings()

    print_scale(index)

    correct_scores: list[float] = []
    correct_ratios: list[float] = []
    no_policy_scores: list[float] = []
    no_policy_ratios: list[float] = []
    failures: list[str] = []

    header = f"{'case':10} {'expected':16} {'retrieved':16} {'top':>6} {'runner-up':>10} {'ratio':>7}"
    print(header)
    print("-" * 78)

    for case_id, query in case_queries().items():
        want = expected.get(case_id)
        # Call the same ranking the agent calls. An earlier version of this
        # script reimplemented it, the two implementations disagreed, and the
        # calibration passed while the agent retrieved the wrong policy. A
        # measurement harness that does not exercise the real code path
        # measures the harness.
        ranked = index.rank_policies(query)
        got, top = ranked[0] if ranked else ("(nothing)", 0.0)
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
        ratio = top / runner_up if runner_up > 0 else float("inf")

        if want is None:
            no_policy_scores.append(top)
            no_policy_ratios.append(ratio)
            verdict = "no policy expected"
        elif got == want:
            correct_scores.append(top)
            correct_ratios.append(ratio)
            verdict = "ok"
        else:
            correct_scores.append(0.0)
            correct_ratios.append(0.0)
            failures.append(f"{case_id}: wanted {want}, got {got}")
            verdict = "WRONG"

        print(
            f"{case_id:10} {str(want):16} {got:16} {top:6.3f} {runner_up:10.3f} "
            f"{ratio:7.2f}  {verdict}"
        )

    print()
    if correct_scores:
        print(
            f"correct-policy top scores : min {min(correct_scores):.3f}  "
            f"max {max(correct_scores):.3f}"
        )
        print(f"correct-policy ratios     : min {min(correct_ratios):.2f}x")
    if no_policy_scores:
        print(f"no-policy-case top scores : max {max(no_policy_scores):.3f}")
        print(f"no-policy-case ratios     : max {max(no_policy_ratios):.2f}x")

    ok = not failures
    if correct_scores and no_policy_scores:
        gap = min(correct_scores) - max(no_policy_scores)
        print(f"separation (score)        : {gap:.3f}")
        if gap <= 0:
            print(
                "\nThe two populations overlap. That is a retrieval problem, not a "
                "threshold problem -- do not paper over it with a constant."
            )
            return 1

        # The ratio separates these populations far better than the raw score
        # does, which is why the audit trail leads with it. Report it as a
        # first-class figure, not a derived curiosity.
        ratio_gap = min(correct_ratios) - max(no_policy_ratios)
        print(f"separation (ratio)        : {ratio_gap:.2f}x")
        if ratio_gap <= 0:
            print(
                "\nThe winner-to-runner-up ratio no longer separates the two "
                "populations. The audit trail leads with that ratio, so it is now "
                "leading with a number that does not discriminate -- fix the "
                "scorer or stop printing the verdict."
            )
            ok = False

        print()
        print("do the deployed constants still hold?")
        print("-" * 78)
        ok &= _check(
            f"retrieval_no_policy_floor = {settings.retrieval_no_policy_floor}",
            max(no_policy_scores) < settings.retrieval_no_policy_floor <= min(correct_scores),
            f"must sit in ({max(no_policy_scores):.3f}, {min(correct_scores):.3f}]",
        )
        ok &= _check(
            f"retrieval_score_floor     = {settings.retrieval_score_floor}",
            settings.retrieval_score_floor > min(correct_scores),
            "must sit above the weakest correct match, or the reformulation path "
            "is dead code that no test exercises",
        )
        ok &= _check(
            f"CORRECT_MATCH_BAND        = {CORRECT_MATCH_BAND}",
            CORRECT_MATCH_BAND[0] <= min(correct_scores)
            and max(correct_scores) <= CORRECT_MATCH_BAND[1],
            f"the audit trail prints this band as context; measured "
            f"{min(correct_scores):.3f}-{max(correct_scores):.3f}",
        )
        ok &= _check(
            f"DECISIVE_RATIO / CLEAR_RATIO = {DECISIVE_RATIO} / {CLEAR_RATIO}",
            max(no_policy_ratios) < CLEAR_RATIO,
            f"'clear' must mean something a no-policy case cannot reach; the "
            f"no-policy case ratio is {max(no_policy_ratios):.2f}x",
        )
        ok &= _check(
            f"UNRELATED_TEXT_FLOOR / VERBATIM_QUOTE_CEILING = "
            f"{UNRELATED_TEXT_FLOOR} / {VERBATIM_QUOTE_CEILING}",
            abs(index.best_policy(UNRELATED_PROSE)[1] - UNRELATED_TEXT_FLOOR) < 0.01,
            "the anchors printed in every audit line must match what the index does",
        )

    for failure in failures:
        print(f"\nFAILURE  {failure}")

    print(
        "\nNOTE  docs/MODEL_CHOICES.md still carries the table produced by the "
        "hand-written queries this script no longer uses. Its CASE-004 row (0.048) "
        "and its separation figure (0.044) are both artefacts of that dict."
    )
    return 0 if ok else 1


def _check(label: str, passed: bool, why: str) -> bool:
    print(f"  {'HOLDS ' if passed else 'BROKEN'}  {label}   {why}")
    return passed


if __name__ == "__main__":
    sys.exit(main())
