"""Measure whether Overturn reaches correct conclusions.

Every other harness in this repository checks that a mechanism works. This one
checks the product: given a denial letter and a chart, does the system reach the
right answer, and does it refuse to reach a wrong one.

The distinction matters because the failures worth worrying about here are not
crashes. They are a confident, well-cited, entirely irrelevant appeal — and
nothing in a test suite that asserts on status codes would notice.

    uv run python scripts/evaluate.py
    uv run python scripts/evaluate.py --markdown   # table for the README

Runs offline and deterministically, so it costs nothing and can be re-run on
every change.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from agents.intake.documents import SourceDocument
from agents.mapping.charts import ChartNotFound, load_chart
from agents.offline.handlers import build_offline_llm
from agents.orchestrator.deps import build_fleet
from agents.orchestrator.pipeline import Pipeline
from core.schemas.case import CaseRecord
from core.schemas.enums import CaseStatus
from core.store import MemoryStore

REPO = Path(__file__).resolve().parents[1]
DENIALS = REPO / "data" / "denials"

# What each scenario is supposed to end at. Read from the manifest's own stated
# intent rather than from whatever the system currently happens to do.
EXPECTED: dict[str, CaseStatus] = {
    "clean_win": CaseStatus.AWAITING_APPROVAL,
    "prompt_injection": CaseStatus.QUARANTINED,
    "verification_catch": CaseStatus.AWAITING_APPROVAL,
    "no_applicable_policy": CaseStatus.DECLINED_NO_BASIS,
    "insufficient_documentation": CaseStatus.DECLINED_NO_BASIS,
    "scanned_fax": CaseStatus.AWAITING_APPROVAL,
    # The payer conceded the other criteria and rested on the one the chart
    # cannot answer. Declining again is the right answer, and arguing the
    # conceded points would be the wrong one.
    "second_denial_still_undocumented": CaseStatus.DECLINED_NO_BASIS,
}


@dataclass
class CaseResult:
    case_id: str
    scenario: str
    expected: CaseStatus
    actual: CaseStatus
    citations: int = 0
    fabricated_citations: list[str] = field(default_factory=list)
    unlocatable_evidence: list[str] = field(default_factory=list)
    criteria_evaluated: int = 0
    drafting_attempts: int = 0

    @property
    def outcome_correct(self) -> bool:
        return self.actual is self.expected

    @property
    def grounded(self) -> bool:
        return not self.fabricated_citations and not self.unlocatable_evidence

    @property
    def passed(self) -> bool:
        return self.outcome_correct and self.grounded


def check_grounding(case: CaseRecord) -> tuple[list[str], list[str]]:
    """Every citation against the retrieved policy, every quote against the chart.

    Checked here independently of the pipeline's own verification, because a
    harness that trusts the component it is measuring measures nothing.
    """
    fabricated: list[str] = []
    unlocatable: list[str] = []

    if case.retrieval is not None:
        known = case.retrieval.section_ids()
        for draft in case.drafts:
            fabricated.extend(sorted(draft.cited_ids() - known))

    if case.criteria is not None:
        try:
            locators = load_chart(case.case_id).locators()
        except ChartNotFound:
            locators = set()
        for verdict in case.criteria.verdicts:
            for evidence in verdict.evidence:
                if evidence.locator not in locators:
                    unlocatable.append(f"{verdict.criterion_id}:{evidence.locator}")

    return sorted(set(fabricated)), sorted(set(unlocatable))


def run_case(case_id: str, scenario: str) -> CaseResult:
    pipeline = Pipeline(build_fleet(store=MemoryStore(), llm=build_offline_llm()))
    document = SourceDocument(
        uri=f"gs://overturn-intake/{case_id}.txt",
        data=(DENIALS / f"{case_id}.txt").read_bytes(),
        mime_type="text/plain",
    )
    case = pipeline.ingest(document, case_id=case_id)
    fabricated, unlocatable = check_grounding(case)

    return CaseResult(
        case_id=case_id,
        scenario=scenario,
        expected=EXPECTED.get(scenario, CaseStatus.AWAITING_APPROVAL),
        actual=case.status,
        citations=sum(len(d.citations) for d in case.drafts),
        fabricated_citations=fabricated,
        unlocatable_evidence=unlocatable,
        criteria_evaluated=len(case.criteria.verdicts) if case.criteria else 0,
        drafting_attempts=len(case.drafts),
    )


def run_fault_injection() -> list[tuple[str, bool, str]]:
    """Both fault modes, since a safety net nobody drops into proves nothing."""
    import os

    checks: list[tuple[str, bool, str]] = []

    os.environ["OVERTURN_SABOTAGE_DRAFTING"] = "first"
    result = run_case("CASE-003", "verification_catch")
    checks.append(
        (
            "transient fabrication caught, retry clean",
            result.actual is CaseStatus.AWAITING_APPROVAL and result.drafting_attempts == 2,
            f"{result.drafting_attempts} attempt(s), ended {result.actual.value}",
        )
    )

    os.environ["OVERTURN_SABOTAGE_DRAFTING"] = "always"
    result = run_case("CASE-003", "verification_catch")
    checks.append(
        (
            "persistent fabrication stops at the cap, nothing sent",
            result.actual is CaseStatus.NEEDS_HUMAN_REVIEW and result.drafting_attempts == 3,
            f"{result.drafting_attempts} attempt(s), ended {result.actual.value}",
        )
    )
    os.environ.pop("OVERTURN_SABOTAGE_DRAFTING", None)
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markdown", action="store_true", help="Emit a markdown table.")
    args = parser.parse_args()

    manifest = json.loads((REPO / "data" / "cases.json").read_text())
    results = [
        run_case(entry["case_id"], entry["scenario"])
        for entry in manifest["cases"]
        if (DENIALS / f"{entry['case_id']}.txt").exists()
    ]
    faults = run_fault_injection()

    if args.markdown:
        print(
            "| Case | Scenario | Expected | Reached | Fabricated citations | Unlocatable evidence |"
        )
        print("|---|---|---|---|---|---|")
        for r in results:
            mark = "" if r.passed else " ⚠"
            print(
                f"| `{r.case_id}` | {r.scenario.replace('_', ' ')} | "
                f"`{r.expected.value}` | `{r.actual.value}`{mark} | "
                f"{len(r.fabricated_citations)} | {len(r.unlocatable_evidence)} |"
            )
    else:
        header = f"{'case':10} {'scenario':28} {'reached':26} {'cites':>6} {'fab':>4} {'bad ev':>7}"
        print(header)
        print("-" * len(header))
        for r in results:
            mark = " " if r.passed else "!"
            print(
                f"{mark}{r.case_id:9} {r.scenario:28} {r.actual.value:26} "
                f"{r.citations:>6} {len(r.fabricated_citations):>4} "
                f"{len(r.unlocatable_evidence):>7}"
            )

    correct = sum(1 for r in results if r.outcome_correct)
    grounded = sum(1 for r in results if r.grounded)
    total_cites = sum(r.citations for r in results)
    total_fab = sum(len(r.fabricated_citations) for r in results)
    total_bad = sum(len(r.unlocatable_evidence) for r in results)

    print()
    print(f"outcomes correct              {correct}/{len(results)}")
    print(f"cases fully grounded          {grounded}/{len(results)}")
    print(f"citations checked             {total_cites}")
    print(f"citations not in the corpus   {total_fab}")
    print(f"chart quotes with no locator  {total_bad}")
    print()
    for label, ok, detail in faults:
        print(f"{'ok  ' if ok else 'FAIL'} {label:52} {detail}")

    failed = (
        correct != len(results) or grounded != len(results) or any(not ok for _, ok, _ in faults)
    )
    if failed:
        print("\nSome cases did not reach their intended outcome.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
