"""HTTP surface for the human approval gate.

Server-rendered HTML, no build step, no client framework, no CDN. The whole
interface renders from this process with the network unplugged, which matters
because the one screen a clerk must be able to trust is the one that decides
whether a letter goes to an insurer.

The routing layer is deliberately thin: it parses a form, calls
:class:`~services.approval_ui.service.ApprovalService`, and re-renders. Every
rule about what may be approved lives in the service, where it can be tested
without a web client.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from core.idempotency import ActionInFlight, PayloadMismatch
from core.state import CaseNotFound
from core.store import DocumentStore, build_store
from services.approval_ui import view
from services.approval_ui.service import REVIEWABLE_STATUS, ApprovalError, ApprovalService

TEMPLATE_DIR = Path(__file__).parent / "templates"


def create_app(store: DocumentStore | None = None) -> FastAPI:
    """Build the application. Pass a store to test it; leave it out in deployment."""
    # /docs and /redoc are switched off on purpose: both fetch Swagger assets
    # from a CDN, and this service has to render with no network at all.
    app = FastAPI(title="Overturn — appeal review", docs_url=None, redoc_url=None)

    service = ApprovalService(store if store is not None else build_store())
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    templates.env.trim_blocks = True
    templates.env.lstrip_blocks = True
    templates.env.filters.update(view.FILTERS)

    app.state.service = service
    app.state.templates = templates

    def render_case(
        request: Request,
        case_id: str,
        *,
        error: ApprovalError | Exception | None = None,
        error_heading: str | None = None,
        error_form: str | None = None,
        notice: str | None = None,
        form: dict[str, Any] | None = None,
        status_code: int = 200,
    ) -> Response:
        """The review screen, with whatever the last submission left behind."""
        case = service.load(case_id)
        draft = view.draft_under_review(case)
        verification = _verification_for(case, draft)
        context: dict[str, Any] = {
            "case": case,
            "denial": case.denial,
            "screening": case.screening,
            "matrix": case.criteria,
            "draft": draft,
            "verification": verification,
            "history": view.attempt_history(case),
            "checks": view.check_rows(verification),
            "deadline": view.deadline_view(case.denial.appeal_deadline if case.denial else None),
            "trail": service.trail(case.case_id),
            "decidable": case.status == REVIEWABLE_STATUS,
            "error": str(error) if error else None,
            "error_field": getattr(error, "field", None),
            "error_form": error_form,
            "error_heading": error_heading
            or getattr(error, "heading", None)
            or "This decision was not recorded",
            "notice": notice,
            "form": form or {},
            "reviewer_hint": view.reviewer_hint(request.headers),
        }
        return templates.TemplateResponse(request, "case.html", context, status_code=status_code)

    # -- routes ------------------------------------------------------------- #

    @app.get("/", response_class=HTMLResponse)
    def queue(request: Request) -> Response:
        return templates.TemplateResponse(
            request,
            "queue.html",
            {
                "awaiting": [view.queue_row(c) for c in service.awaiting_approval()],
                "needs_review": [view.queue_row(c) for c in service.needs_human_review()],
            },
        )

    @app.get("/case/{case_id}", response_class=HTMLResponse)
    def review(
        request: Request,
        case_id: str,
        decided: str | None = None,
        replay: int = 0,
    ) -> Response:
        return render_case(request, case_id, notice=_notice(decided, replay))

    @app.post("/case/{case_id}/approve")
    def approve(
        request: Request,
        case_id: str,
        # Required, with no fallback to "the latest draft". A submission that
        # cannot say which attempt the reviewer read is not an approval.
        draft_attempt: Annotated[int, Form()],
        decided_by: Annotated[str, Form()] = "",
        # The three things a clerk is competent to confirm. Verification has
        # already computed all three; these record that a person looked.
        citations_checked: Annotated[bool, Form()] = False,
        quotes_checked: Annotated[bool, Form()] = False,
        assertions_checked: Annotated[bool, Form()] = False,
    ) -> Response:
        try:
            outcome = service.approve(
                case_id,
                decided_by=decided_by,
                draft_attempt=draft_attempt,
                citations_checked=citations_checked,
                quotes_checked=quotes_checked,
                assertions_checked=assertions_checked,
            )
            # Whichever signature lands second is what transmits. Without this
            # the interface is a dead end: the case reaches `approved` and
            # nothing moves it on.
            service.submit_if_ready(case_id)
        except ApprovalError as exc:
            return render_case(
                request,
                case_id,
                error=exc,
                error_form="approve",
                form={"decided_by": decided_by},
                status_code=exc.status_code,
            )
        except (ActionInFlight, PayloadMismatch) as exc:
            return render_case(
                request,
                case_id,
                error=exc,
                error_heading="Another decision is being recorded right now",
                error_form="approve",
                form={"decided_by": decided_by},
                status_code=409,
            )
        return _see_other(case_id, "approved", replay=not outcome.recorded)

    @app.post("/case/{case_id}/reject")
    def reject(
        request: Request,
        case_id: str,
        decided_by: Annotated[str, Form()] = "",
        reason: Annotated[str, Form()] = "",
    ) -> Response:
        try:
            service.reject(case_id, decided_by=decided_by, reason=reason)
        except ApprovalError as exc:
            return render_case(
                request,
                case_id,
                error=exc,
                error_form="reject",
                form={"decided_by": decided_by, "reason": reason},
                status_code=exc.status_code,
            )
        return _see_other(case_id, "rejected")

    @app.get("/health")
    def health() -> JSONResponse:
        # Deliberately does not touch the datastore. A health check that fails
        # when Firestore hiccups gets the revision killed for no reason.
        return JSONResponse({"status": "ok", "service": "approval_ui"})

    @app.exception_handler(CaseNotFound)
    def case_not_found(request: Request, exc: CaseNotFound) -> Response:
        return templates.TemplateResponse(
            request,
            "not_found.html",
            {"case_id": exc.args[0] if exc.args else ""},
            status_code=404,
        )

    return app


def _verification_for(case: Any, draft: Any) -> Any:
    """The verdict on the draft being shown, not merely the newest verdict."""
    if draft is None:
        return case.latest_verification
    match = next((v for v in case.verifications if v.attempt == draft.attempt), None)
    return match or case.latest_verification


def _see_other(case_id: str, decided: str, replay: bool = False) -> RedirectResponse:
    """303 after a decision, so a refresh cannot resubmit it."""
    suffix = "&replay=1" if replay else ""
    return RedirectResponse(
        f"/case/{quote(case_id, safe='')}?decided={decided}{suffix}", status_code=303
    )


def _notice(decided: str | None, replay: int) -> dict[str, str] | None:
    """The banner shown after a decision.

    The replayed case gets its own heading. Telling a reviewer "Decision
    recorded" when nothing was recorded is the kind of small lie that makes
    someone press the button a third time.
    """
    if decided == "approved" and replay:
        return {
            "heading": "Already approved",
            "body": (
                "This case was already approved. Nothing further was recorded — "
                "the earlier decision stands."
            ),
        }
    if decided == "approved":
        return {
            "heading": "Approval recorded",
            "body": "This appeal may now be transmitted to the payer.",
        }
    if decided == "rejected":
        return {
            "heading": "Rejection recorded",
            "body": "The case has been returned for human review.",
        }
    return None
