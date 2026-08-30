"""Command-line control of cases: review, approve, co-sign, escalate.

Two reasons this exists alongside the web interface.

**Recording.** The demo has to show a case going all the way out, and driving
that from a terminal is reproducible in a way that clicking is not. A beat that
depends on a button being in the right place is a beat that can fail on the day.

**It is the honest fallback.** If the interface is unavailable, the gate still
has to be operable — and the gate is the one thing in this system that a person
must be able to reach. Both paths call the same `ApprovalService`, so neither
can drift into permitting something the other refuses.

    uv run python scripts/casectl.py list
    uv run python scripts/casectl.py show CASE-001
    uv run python scripts/casectl.py approve CASE-001 --by clerk@clinic.example
    uv run python scripts/casectl.py cosign CASE-001 --clinician "M. Castellanos" --credential MD
    uv run python scripts/casectl.py tick
"""

from __future__ import annotations

import argparse
import sys

from agents.offline.handlers import build_offline_llm
from agents.orchestrator.deps import build_fleet
from agents.orchestrator.pipeline import Pipeline
from core.config import get_settings
from core.schemas.case import CaseRecord
from core.schemas.enums import CaseStatus
from core.store import DocumentStore, build_store
from services.approval_ui.service import ApprovalError, ApprovalService

BOLD = "\033[1m"
DIM = "\033[90m"
RESET = "\033[0m"


def _store() -> DocumentStore:
    return build_store()


def _pipeline(store: DocumentStore) -> Pipeline:
    settings = get_settings()
    llm = None if settings.runtime_mode == "cloud" else build_offline_llm()
    return Pipeline(build_fleet(store=store, llm=llm))


def _signatures(case: CaseRecord) -> str:
    clerk = "yes" if case.human_decision and case.human_decision.approved else "no"
    clinician = (
        "yes"
        if case.clinician_cosign and case.clinician_cosign.attests_clinical_accuracy
        else ("not required" if not case.requires_clinician_cosign else "no")
    )
    return f"clerk={clerk} clinician={clinician} ready={case.ready_to_submit}"


def cmd_list(args: argparse.Namespace) -> int:
    service = ApprovalService(_store())
    waiting = service.awaiting_approval()
    review = service.needs_human_review()

    if not waiting and not review:
        print("Nothing is waiting on a person.")
        return 0

    if waiting:
        print(f"{BOLD}awaiting approval{RESET}")
        for case in waiting:
            deadline = case.denial.appeal_deadline if case.denial else None
            print(
                f"  {case.case_id:12} {case.draft_attempts} draft(s)  "
                f"deadline {deadline or 'unstated'}  {DIM}{_signatures(case)}{RESET}"
            )
    if review:
        print(f"\n{BOLD}needs human review{RESET}")
        for case in review:
            reason = (case.needs_human_reason or "").split(".")[0]
            print(f"  {case.case_id:12} {reason[:88]}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = _store()
    service = ApprovalService(store)
    case = service.load(args.case_id)

    print(f"{BOLD}{case.case_id}{RESET}  {case.status.value}")
    if case.denial:
        print(f"  payer      {case.denial.payer_name}")
        print(f"  claim      {case.denial.claim_number or '(not stated)'}")
        for service_line in case.denial.services:
            print(f"  service    {service_line.description} [{service_line.procedure_code}]")
        print(f"  denied on  {' '.join(case.denial.denial_reason_text.split())[:150]}")

    if case.criteria:
        print(f"\n{BOLD}criteria{RESET}")
        for verdict in case.criteria.verdicts:
            mark = {"satisfied": "+", "not_satisfied": "-"}.get(verdict.verdict.value, "?")
            where = f"{DIM}[{verdict.evidence[0].locator}]{RESET}" if verdict.evidence else ""
            print(f"  {mark} {verdict.criterion_id:22} {verdict.verdict.value:28} {where}")

    if case.verifications:
        print(f"\n{BOLD}verification{RESET}")
        for check in case.verifications:
            status = "passed" if check.passed else "REJECTED"
            print(f"  attempt {check.attempt}: {check.citations_checked} citation(s) {status}")
            for instruction in check.revision_instructions()[:2]:
                print(f"      {DIM}{instruction[:100]}{RESET}")

    if args.letter and case.latest_draft:
        print(f"\n{BOLD}draft (attempt {case.latest_draft.attempt}){RESET}\n")
        print(case.latest_draft.body)

    if args.trail:
        print(f"\n{BOLD}audit trail{RESET}")
        # Through the service's own gateway handle rather than a raw store
        # query: `read_case_trail` now requires one, the same as every other
        # datastore reader in this codebase.
        for event in service.trail(case.case_id):
            mark = " " if event.succeeded else "!"
            print(f" {mark} {event.agent.value:13} {event.operation:18} {event.decision[:80]}")

    print(f"\n{BOLD}signatures{RESET}  {_signatures(case)}")
    if case.needs_human_reason:
        print(f"{BOLD}note{RESET}  {case.needs_human_reason}")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    """The clerk's gate: the paper trail, not the medicine."""
    store = _store()
    service = ApprovalService(store)
    case = service.load(args.case_id)

    if case.latest_verification and not case.latest_verification.passed:
        print("Refusing: the most recent draft did not pass verification.")
        return 1

    attempt = args.attempt or (case.latest_draft.attempt if case.latest_draft else None)
    if attempt is None:
        print("Refusing: this case has no draft to approve.")
        return 1

    if not args.yes:
        print(
            f"You are confirming that the citations in attempt {attempt} resolve, that "
            f"the quoted policy text matches its source, and that nothing is asserted "
            f"the criteria matrix does not support.\n"
            f"You are NOT being asked whether this care was appropriate."
        )
        if input("Confirm all three (y/N)? ").strip().lower() != "y":
            print("Nothing recorded.")
            return 1

    try:
        outcome = service.approve(
            case_id=args.case_id,
            decided_by=args.by,
            draft_attempt=attempt,
            citations_checked=True,
            quotes_checked=True,
            assertions_checked=True,
        )
    except ApprovalError as exc:
        print(f"Refused: {exc}")
        return 1

    print(
        f"{'recorded' if outcome.recorded else 'already recorded'}: "
        f"attempt {attempt} approved by {args.by}"
    )
    return _try_submit(service, args.case_id, store)


def cmd_cosign(args: argparse.Namespace) -> int:
    """The clinician's signature on the clinical argument."""
    store = _store()
    service = ApprovalService(store)
    case = service.load(args.case_id)
    attempt = args.attempt or (case.latest_draft.attempt if case.latest_draft else None)

    try:
        outcome = service.cosign(
            case_id=args.case_id,
            clinician_name=args.clinician,
            credential=args.credential,
            attests_clinical_accuracy=True,
            npi=args.npi,
            note=args.note,
            draft_attempt=attempt,
        )
    except ApprovalError as exc:
        print(f"Refused: {exc}")
        return 1

    print(
        f"{'recorded' if outcome.recorded else 'already recorded'}: "
        f"attempt {attempt} co-signed by {args.clinician}, {args.credential}"
    )
    return _try_submit(service, args.case_id, store)


def _try_submit(service: ApprovalService, case_id: str, store: DocumentStore) -> int:
    """Whichever signature lands second is what transmits."""
    case = service.submit_if_ready(case_id, _pipeline(store))
    if case.status is CaseStatus.SUBMITTED:
        print(f"\nTransmitted. Payer response due {case.response_deadline}.")
    elif not case.ready_to_submit:
        print(f"\nNot transmitted — {_signatures(case)}")
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    service = ApprovalService(_store())
    try:
        service.reject(args.case_id, decided_by=args.by, reason=args.reason)
    except ApprovalError as exc:
        print(f"Refused: {exc}")
        return 1
    print(f"rejected: {args.case_id}")
    return 0


def cmd_tick(args: argparse.Namespace) -> int:
    """What Cloud Scheduler calls. Runs the escalation sweep once."""
    store = _store()
    pipeline = _pipeline(store)
    overdue = pipeline.fleet.cases.find_overdue()
    if not overdue:
        print("No case is past its payer deadline.")
        return 0

    print(f"{len(overdue)} case(s) overdue")
    for case in pipeline.escalate_overdue():
        print(
            f"  {case.case_id:12} {case.status.value:22} "
            f"level={case.appeal_level.value:28} escalations={case.escalation_count}"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="Cases waiting on a person.").set_defaults(fn=cmd_list)

    show = sub.add_parser("show", help="Everything known about one case.")
    show.add_argument("case_id")
    show.add_argument("--letter", action="store_true", help="Print the drafted letter.")
    show.add_argument("--trail", action="store_true", help="Print the audit trail.")
    show.set_defaults(fn=cmd_show)

    approve = sub.add_parser("approve", help="Clerk approval of the paper trail.")
    approve.add_argument("case_id")
    approve.add_argument("--by", required=True)
    approve.add_argument("--attempt", type=int)
    approve.add_argument("--yes", action="store_true", help="Skip the confirmation.")
    approve.set_defaults(fn=cmd_approve)

    cosign = sub.add_parser("cosign", help="Clinician signature on the clinical argument.")
    cosign.add_argument("case_id")
    cosign.add_argument("--clinician", required=True)
    cosign.add_argument("--credential", required=True)
    cosign.add_argument("--npi")
    cosign.add_argument("--note")
    cosign.add_argument("--attempt", type=int)
    cosign.set_defaults(fn=cmd_cosign)

    reject = sub.add_parser("reject", help="Send a draft back.")
    reject.add_argument("case_id")
    reject.add_argument("--by", required=True)
    reject.add_argument("--reason", required=True)
    reject.set_defaults(fn=cmd_reject)

    sub.add_parser("tick", help="Run the escalation sweep once.").set_defaults(fn=cmd_tick)

    args = parser.parse_args()
    try:
        return args.fn(args)
    except FileNotFoundError as exc:
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
