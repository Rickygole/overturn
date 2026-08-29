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

    OVERTURN_RUNTIME_MODE=local OVERTURN_LLM_BACKEND=vertex \\
        uv run python scripts/evaluate.py --live

``--live`` runs the identical eight cases against real Vertex AI instead of the
offline backend: same manifest, same expectations, same grounding checks. It
costs real money and real time (roughly 40-60 model calls for the full
manifest) and is not run by default or in CI. Fault injection is skipped in
live mode -- it exists to prove the retry loop holds under a fault the offline
backend is told to manufacture, and a live run over real cases either shows
that loop firing on its own or it doesn't; manufacturing one on top adds cost
without adding evidence.
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
from core.audit import read_case_trail
from core.llm import build_llm
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

    # --- Only populated meaningfully when the run is real (see `--live`). The
    # offline backend answers from registered handlers with no billed tokens,
    # so these are near-zero noise there and are not reported for it. ---------
    verification_rejected: bool = False
    verification_recovered: bool = False
    rejection_reasons: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_by_agent: dict[str, dict[str, int]] = field(default_factory=dict)

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


def check_verification_recovery(case: CaseRecord) -> tuple[bool, bool, list[str]]:
    """Did Verification reject a draft, and did the retry then pass?

    The headline claim in this project is not "Verification exists" but
    "Verification caught something and the loop recovered without a person
    doing anything." That is only checkable from the sequence of verdicts, not
    from the final status alone -- a case that ends ``awaiting_human_approval``
    on the first attempt proves nothing about the retry loop at all.
    """
    rejected = any(not v.passed for v in case.verifications)
    recovered = False
    reasons: list[str] = []
    for i, verification in enumerate(case.verifications):
        if verification.passed:
            continue
        reasons.extend(f.detail for f in verification.findings if f.severity == "fatal")
        if any(later.passed for later in case.verifications[i + 1 :]):
            recovered = True
    return rejected, recovered, reasons


def summarise_cost(store: MemoryStore, case_id: str) -> tuple[int, int, dict[str, dict[str, int]]]:
    """Sum billed tokens per case and per agent from the audit trail.

    Read from the audit log rather than threaded through by hand, because the
    audit log is the one place every agent already reports what a call cost --
    duplicating that bookkeeping here would be a second place for it to drift.
    """
    by_agent: dict[str, dict[str, int]] = {}
    total_in = total_out = 0
    for event in read_case_trail(store, case_id):
        if event.input_tokens is None and event.output_tokens is None:
            continue
        bucket = by_agent.setdefault(event.agent.value, {"input": 0, "output": 0, "calls": 0})
        bucket["input"] += event.input_tokens or 0
        bucket["output"] += event.output_tokens or 0
        bucket["calls"] += 1
        total_in += event.input_tokens or 0
        total_out += event.output_tokens or 0
    return total_in, total_out, by_agent


def run_case(case_id: str, scenario: str, live: bool = False) -> CaseResult:
    store = MemoryStore()
    llm = build_llm() if live else build_offline_llm()
    pipeline = Pipeline(build_fleet(store=store, llm=llm))
    document = SourceDocument(
        uri=f"gs://overturn-intake/{case_id}.txt",
        data=(DENIALS / f"{case_id}.txt").read_bytes(),
        mime_type="text/plain",
    )
    case = pipeline.ingest(document, case_id=case_id)
    fabricated, unlocatable = check_grounding(case)
    rejected, recovered, reasons = check_verification_recovery(case)
    input_tokens, output_tokens, cost_by_agent = summarise_cost(store, case_id)

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
        verification_rejected=rejected,
        verification_recovered=recovered,
        rejection_reasons=reasons,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_by_agent=cost_by_agent,
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
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run against real Vertex AI (respects OVERTURN_LLM_BACKEND / "
        "OVERTURN_RUNTIME_MODE) instead of the offline backend. Costs real "
        "money and time; skips fault injection.",
    )
    args = parser.parse_args()

    if args.live:
        print(
            "*** LIVE RUN: calling real Vertex AI for every case. "
            "This costs money and takes minutes, not seconds. ***\n"
        )

    manifest = json.loads((REPO / "data" / "cases.json").read_text())
    results = [
        run_case(entry["case_id"], entry["scenario"], live=args.live)
        for entry in manifest["cases"]
        if (DENIALS / f"{entry['case_id']}.txt").exists()
    ]
    faults = [] if args.live else run_fault_injection()

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
    elif args.live:
        header = (
            f"{'case':10} {'scenario':28} {'reached':26} {'cites':>6} {'fab':>4} "
            f"{'bad ev':>7} {'attempts':>9} {'retry ok':>9} {'tokens in/out':>16}"
        )
        print(header)
        print("-" * len(header))
        for r in results:
            mark = " " if r.passed else "!"
            retry = (
                "yes"
                if r.verification_recovered
                else ("n/a" if not r.verification_rejected else "NO")
            )
            print(
                f"{mark}{r.case_id:9} {r.scenario:28} {r.actual.value:26} "
                f"{r.citations:>6} {len(r.fabricated_citations):>4} "
                f"{len(r.unlocatable_evidence):>7} {r.drafting_attempts:>9} {retry:>9} "
                f"{r.input_tokens:>7}/{r.output_tokens:<7}"
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

    if args.live:
        recovered = [r for r in results if r.verification_recovered]
        rejected_not_recovered = [
            r for r in results if r.verification_rejected and not r.verification_recovered
        ]
        total_in = sum(r.input_tokens for r in results)
        total_out = sum(r.output_tokens for r in results)
        print()
        print(f"drafts Verification rejected and the retry then passed: {len(recovered)}")
        for r in recovered:
            print(f"  {r.case_id}: {'; '.join(r.rejection_reasons)[:160]}")
        if rejected_not_recovered:
            print(
                f"drafts Verification rejected and never recovered: {len(rejected_not_recovered)}"
            )
            for r in rejected_not_recovered:
                print(f"  {r.case_id} -> {r.actual.value}")
        print()
        print(f"total tokens billed            {total_in} in / {total_out} out")
        for agent in ("sentinel", "intake", "retrieval", "mapping", "drafting", "verification"):
            agent_in = sum(r.cost_by_agent.get(agent, {}).get("input", 0) for r in results)
            agent_out = sum(r.cost_by_agent.get(agent, {}).get("output", 0) for r in results)
            agent_calls = sum(r.cost_by_agent.get(agent, {}).get("calls", 0) for r in results)
            if agent_calls:
                print(
                    f"  {agent:13} {agent_calls:>3} call(s)  {agent_in:>7} in / {agent_out:>6} out"
                )

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
