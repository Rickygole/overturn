"""Measure retrieval scores across every case, so the thresholds are evidence.

There are two thresholds in the retrieval path and they answer different
questions:

  ``retrieval_score_floor``      below this, reformulate the query and try again
  ``retrieval_no_policy_floor``  below this, no policy in the corpus governs
                                 this denial, and the honest action is to
                                 decline to appeal rather than appeal weakly

Setting them by intuition produces a retriever that works on the cases someone
happened to try. This script prints the actual distribution — correct-policy
scores against wrong-policy scores — and the constants are then set from the
gap between them. If the gap is narrow, that is a retrieval problem to fix, not
a constant to nudge.

Run after any change to the corpus or the scorer::

    uv run python scripts/calibrate_retrieval.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agents.retrieval.lexical import build_index

REPO = Path(__file__).resolve().parents[1]

# The query each case produces, written as Retrieval will build it: the denial
# reason plus the service description, which is all Intake gives it.
CASE_QUERIES: dict[str, str] = {
    "CASE-001": "continuous glucose monitoring system receiver transmitter sensors denied "
    "not medically necessary intensive insulin regimen self-monitoring blood glucose",
    "CASE-002": "magnetic resonance imaging lumbar spine without contrast denied "
    "trial of conservative management six weeks not established radiculopathy",
    "CASE-003": "cardiac magnetic resonance imaging with contrast denied initial "
    "diagnostic evaluation not inconclusive echocardiogram ejection fraction",
    "CASE-004": "cosmetic dermabrasion denied not a covered benefit under the plan",
    "CASE-005": "CPAP continuous positive airway pressure device and supplies denied "
    "obstructive sleep apnea apnea-hypopnea index sleep study",
    "CASE-006": "intensive outpatient programme behavioral health denied less intensive "
    "level of care not attempted standardised severity assessment",
    "CASE-007": "continuous glucose monitoring system denied not medically necessary "
    "intensive insulin regimen",
    "CASE-008": "cardiac magnetic resonance imaging with contrast denied on appeal "
    "initial diagnostic evaluation considered adequate echocardiogram",
}


def expected_policies() -> dict[str, str | None]:
    manifest = json.loads((REPO / "data" / "cases.json").read_text())
    return {c["case_id"]: c["policy_id"] for c in manifest["cases"]}


def main() -> int:
    index = build_index()
    expected = expected_policies()

    correct_scores: list[float] = []
    wrong_scores: list[float] = []
    failures: list[str] = []

    print(f"{'case':10} {'expected':16} {'retrieved':16} {'top':>6} {'runner-up':>10}  margin")
    print("-" * 78)

    for case_id, query in CASE_QUERIES.items():
        want = expected.get(case_id)
        hits = index.search(query, k=12)

        by_policy: dict[str, float] = {}
        for section, score in hits:
            by_policy[section.policy_id] = max(by_policy.get(section.policy_id, 0.0), score)

        ranked = sorted(by_policy.items(), key=lambda kv: -kv[1])
        got, top = (ranked[0] if ranked else ("(nothing)", 0.0))
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
        margin = top - runner_up

        if want is None:
            wrong_scores.append(top)
            verdict = "no policy expected"
        elif got == want:
            correct_scores.append(top)
            verdict = "ok"
        else:
            correct_scores.append(0.0)
            failures.append(f"{case_id}: wanted {want}, got {got}")
            verdict = "WRONG"

        print(
            f"{case_id:10} {str(want):16} {got:16} {top:6.3f} {runner_up:10.3f}  "
            f"{margin:6.3f}  {verdict}"
        )

    print()
    if correct_scores:
        print(f"correct-policy top scores : min {min(correct_scores):.3f}  "
              f"max {max(correct_scores):.3f}")
    if wrong_scores:
        print(f"no-policy-case top scores : max {max(wrong_scores):.3f}")

    if correct_scores and wrong_scores:
        gap = min(correct_scores) - max(wrong_scores)
        print(f"separation                : {gap:.3f}")
        if gap <= 0:
            print("\nThe two populations overlap. That is a retrieval problem, not a "
                  "threshold problem — do not paper over it with a constant.")
            return 1
        print(
            f"\nSuggested no_policy_floor : {max(wrong_scores) + gap * 0.35:.2f}  "
            f"(above every no-policy case, below every real one)"
        )
        print(
            f"Suggested score_floor     : {min(correct_scores) + 0.02:.2f}  "
            f"(reformulate on the weakest real matches, which is the point of it)"
        )

    for failure in failures:
        print(f"\nFAILURE  {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
