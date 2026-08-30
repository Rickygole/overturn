"""Run one or more cases end to end, offline.

No cloud project, no credits, no network. Every agent runs, the retry loop runs,
the idempotency guard runs, the audit trail is written and the trace spans are
emitted. Only the generative calls are answered offline.

    uv run python scripts/run_pipeline.py CASE-001
    uv run python scripts/run_pipeline.py --all
    OVERTURN_SABOTAGE_DRAFTING=1 uv run python scripts/run_pipeline.py CASE-003
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agents.intake.documents import SourceDocument
from agents.offline.handlers import build_offline_llm
from agents.orchestrator.deps import build_fleet
from agents.orchestrator.pipeline import Pipeline
from core.audit import read_case_trail
from core.config import get_settings
from core.gateway import Access
from core.llm import build_llm
from core.store import MemoryStore, build_store

REPO = Path(__file__).resolve().parents[1]
DENIAL_DIR = REPO / "data" / "denials"

GREY = "\033[90m"
BOLD = "\033[1m"
RESET = "\033[0m"


def load_denial(case_id: str) -> SourceDocument:
    path = DENIAL_DIR / f"{case_id}.txt"
    if not path.exists():
        raise SystemExit(f"no denial letter at {path}")
    return SourceDocument(
        uri=f"gs://overturn-intake/{case_id}.txt",
        data=path.read_bytes(),
        mime_type="text/plain",
    )


def _llm():
    """Honour the configured backend.

    This used to hardcode the offline handlers, which meant `OVERTURN_LLM_BACKEND=vertex`
    silently produced an offline run — byte-identical output, and a very
    convincing false success.
    """
    settings = get_settings()
    if settings.llm_backend in {"vertex", "adk"} or settings.runtime_mode == "cloud":
        return build_llm()
    return build_offline_llm()


def run_case(case_id: str, store: MemoryStore) -> None:
    fleet = build_fleet(store=store, llm=_llm())
    pipeline = Pipeline(fleet)

    print(f"\n{BOLD}{'=' * 78}{RESET}")
    print(f"{BOLD}{case_id}{RESET}")
    print(f"{BOLD}{'=' * 78}{RESET}")

    case = pipeline.ingest(load_denial(case_id), case_id=case_id)

    print(f"\n{BOLD}state transitions{RESET}")
    for entry in case.history:
        note = f"  {GREY}{entry.note}{RESET}" if entry.note else ""
        print(f"  {entry.to_status.value:26} by {entry.actor}{note}")

    print(f"\n{BOLD}agent trail{RESET}")
    for event in read_case_trail(store, fleet.orchestrator.gateway, case_id):
        mark = " " if event.succeeded else "!"
        print(f" {mark} {event.agent.value:13} {event.operation:18} {event.decision}")

    if case.criteria:
        print(f"\n{BOLD}criteria matrix{RESET}")
        for verdict in case.criteria.verdicts:
            symbol = {"satisfied": "+", "not_satisfied": "-"}.get(verdict.verdict.value, "?")
            evidence = f"{GREY}[{verdict.evidence[0].locator}]{RESET}" if verdict.evidence else ""
            print(f"  {symbol} {verdict.criterion_id:22} {verdict.verdict.value:28} {evidence}")

    if case.drafts:
        print(f"\n{BOLD}drafting and verification{RESET}")
        for draft in case.drafts:
            check = next((v for v in case.verifications if v.attempt == draft.attempt), None)
            status = "PASSED" if check and check.passed else "REJECTED"
            print(f"  attempt {draft.attempt}: {len(draft.citations)} citations -> {status}")
            if check and not check.passed:
                for instruction in check.revision_instructions()[:3]:
                    print(f"      {GREY}{instruction[:96]}{RESET}")

    print(f"\n{BOLD}final status: {case.status.value}{RESET}")
    actions_collection = fleet.orchestrator.gateway.authorize("actions", Access.READ)
    actions = store.query(actions_collection, where=[("case_id", "==", case_id)])
    for _, action in sorted(actions, key=lambda kv: kv[1]["action_key"]):
        print(
            f"  action {action['action_type']:22} {action['status']:10} "
            f"deliveries={action['delivery_count']}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", nargs="*", default=[])
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    cases = (
        sorted(p.stem for p in DENIAL_DIR.glob("CASE-*.txt"))
        if args.all
        else args.cases or ["CASE-001"]
    )

    settings = get_settings()
    if settings.sabotage_configured:
        print(
            "\n*** FAULT INJECTION ENABLED: Drafting is being instructed to "
            "fabricate a citation. ***"
        )

    # Persist when a local state path is configured, so the CLI and the web
    # interface can pick these cases up from another process.
    store = build_store() if settings.local_state_path else MemoryStore()
    for case_id in cases:
        run_case(case_id, store)
    return 0


if __name__ == "__main__":
    sys.exit(main())
