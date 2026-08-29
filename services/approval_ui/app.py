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

import logging
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates

from core.idempotency import (
    ActionInFlight,
    ActionPreviouslyFailed,
    PayloadMismatch,
    UnsafeToRetry,
)
from core.schemas.enums import CaseStatus
from core.state import CaseNotFound
from core.store import DocumentStore, build_store
from services.approval_ui import view
from services.approval_ui.auth import (
    COOKIE_NAME,
    SESSION_HOURS,
    issue_session,
    load_config,
    password_matches,
    path_is_public,
    session_is_valid,
)
from services.approval_ui.service import (
    REVIEWABLE_STATUS,
    ApprovalError,
    ApprovalService,
    ChecksNotConfirmed,
)

TEMPLATE_DIR = Path(__file__).parent / "templates"

# The public site ships from this process too, so the product has one address.
# `docs/` is also what GitHub Pages publishes, which makes that deployment an
# exact mirror rather than a second thing to keep in sync.
SITE_DIR = Path(__file__).resolve().parents[2] / "docs"

# An explicit allowlist rather than a mounted directory. `docs/` also holds the
# project's markdown, which has no business being served, and a fixed mapping
# means a traversal attempt has nothing to traverse to.
SITE_FILES: dict[str, tuple[str, str]] = {
    "index.html": ("index.html", "text/html; charset=utf-8"),
    "how-it-works.html": ("how-it-works.html", "text/html; charset=utf-8"),
    "demo.html": ("demo.html", "text/html; charset=utf-8"),
    "evidence.html": ("evidence.html", "text/html; charset=utf-8"),
    "architecture.html": ("architecture.html", "text/html; charset=utf-8"),
    "styles.css": ("styles.css", "text/css; charset=utf-8"),
    "app.js": ("app.js", "text/javascript; charset=utf-8"),
    "architecture.svg": ("architecture.svg", "image/svg+xml"),
}

logger = logging.getLogger(__name__)


def create_app(store: DocumentStore | None = None, pipeline: Any | None = None) -> FastAPI:
    """Build the application. Pass a store to test it; leave it out in deployment.

    ``pipeline`` is the transmitter used once both signatures are present. Left
    as ``None`` the service builds the real fleet on demand, which is what
    deployment wants and what a test does not: a test that had to reach a real
    model to prove the second signature transmits would not be run.
    """
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
        error_field: str | None = None,
        notice: dict[str, str] | None = None,
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
            "clerk_checks": view.clerk_checks(verification),
            "readiness": view.readiness(case),
            "submission": view.submission(case),
            "stalled": view.approved_but_not_sent(case),
            "deadline": view.deadline_view(case.denial.appeal_deadline if case.denial else None),
            "trail": service.trail(case.case_id),
            "decidable": case.status == REVIEWABLE_STATUS,
            "error": str(error) if error else None,
            "error_field": error_field or getattr(error, "field", None),
            "error_form": error_form,
            "error_heading": error_heading
            or getattr(error, "heading", None)
            or "This decision was not recorded",
            "notice": notice,
            "form": form or {},
            "reviewer_hint": view.reviewer_hint(request.headers),
        }
        return templates.TemplateResponse(request, "case.html", context, status_code=status_code)

    def render_clinical(
        request: Request,
        case_id: str,
        *,
        error: Exception | None = None,
        error_heading: str | None = None,
        error_field: str | None = None,
        notice: dict[str, str] | None = None,
        form: dict[str, Any] | None = None,
        status_code: int = 200,
    ) -> Response:
        """The clinician's screen: the argument, and nothing but the argument.

        A separate page rather than a panel on the review screen, for two
        reasons. The clinician is being asked a different question from the
        clerk and needs a different half of the record to answer it — the
        criteria matrix and the letter, not the audit trail or the screening
        report — and a page that puts both attestations side by side invites one
        person to tick both, which is the failure the split gate exists to
        prevent.
        """
        case = service.load(case_id)
        draft = view.draft_under_review(case)
        context: dict[str, Any] = {
            "case": case,
            "denial": case.denial,
            "matrix": case.criteria,
            "draft": draft,
            "readiness": view.readiness(case),
            "submission": view.submission(case),
            "stalled": view.approved_but_not_sent(case),
            "deadline": view.deadline_view(case.denial.appeal_deadline if case.denial else None),
            "signable": (
                case.clinician_cosign is None and draft is not None and not case.is_terminal
            ),
            "error": str(error) if error else None,
            "error_field": error_field,
            "error_heading": error_heading
            or getattr(error, "heading", None)
            or "This co-sign was not recorded",
            "notice": notice,
            "form": form or {},
        }
        return templates.TemplateResponse(
            request, "clinical.html", context, status_code=status_code
        )

    def transmit(case_id: str) -> bool:
        """Submit if both signatures are in. True when the send failed.

        Every path out of this is a case whose signature was recorded, so the
        failure reported to the reviewer says exactly that. Telling a clerk
        their approval failed, when the approval is on the record and only the
        sending failed, sends them back to press the button again.

        The transmitter puts the reason on the case before it raises, and the
        screen renders it; nothing is swallowed here beyond the traceback, which
        goes to the log.
        """
        try:
            service.submit_if_ready(case_id, pipeline)
        except (ActionPreviouslyFailed, UnsafeToRetry) as exc:
            logger.error("transmission for %s was not safe to retry: %s", case_id, exc)
            return True
        except Exception:
            logger.exception("transmission for %s failed", case_id)
            return True
        return False

    # -- routes ------------------------------------------------------------- #

    @app.get("/queue", response_class=HTMLResponse)
    def queue(request: Request) -> Response:
        # Approved-and-waiting is its own list. Without it a case the clerk has
        # signed sits in `approved`, which appears in neither of the other two
        # queues, and the clinician whose signature it is waiting for has no way
        # to find it.
        return templates.TemplateResponse(
            request,
            "queue.html",
            {
                "awaiting": [view.queue_row(c) for c in service.awaiting_approval()],
                "awaiting_cosign": [
                    view.queue_row(c) for c in service.cases.find_by_status(CaseStatus.APPROVED)
                ],
                "needs_review": [view.queue_row(c) for c in service.needs_human_review()],
            },
        )

    @app.get("/case/{case_id}", response_class=HTMLResponse)
    def review(
        request: Request,
        case_id: str,
        decided: str | None = None,
        replay: int = 0,
        transmit_failed: int = 0,
    ) -> Response:
        return render_case(request, case_id, notice=_notice(decided, replay, transmit_failed))

    @app.get("/case/{case_id}/clinical", response_class=HTMLResponse)
    def clinical(
        request: Request,
        case_id: str,
        decided: str | None = None,
        replay: int = 0,
        transmit_failed: int = 0,
    ) -> Response:
        return render_clinical(request, case_id, notice=_notice(decided, replay, transmit_failed))

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
        except ChecksNotConfirmed as exc:
            # Not an ApprovalError: it carries no status of its own, and the
            # control at fault is the checkbox group rather than a text field.
            return render_case(
                request,
                case_id,
                error=exc,
                error_heading="All three checks have to be confirmed",
                error_form="approve",
                error_field="checks",
                form={"decided_by": decided_by},
                status_code=400,
            )
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
        # Whichever signature lands second is what transmits. Without this the
        # interface is a dead end: the case reaches `approved` and nothing moves
        # it on.
        return _see_other(
            case_id,
            "approved",
            replay=not outcome.recorded,
            transmit_failed=transmit(case_id),
        )

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

    @app.post("/case/{case_id}/cosign")
    def cosign(
        request: Request,
        case_id: str,
        # Pinned like the approval is: a signature that cannot say which draft it
        # read cannot authorise one written afterwards.
        draft_attempt: Annotated[int, Form()],
        clinician_name: Annotated[str, Form()] = "",
        credential: Annotated[str, Form()] = "",
        npi: Annotated[str, Form()] = "",
        note: Annotated[str, Form()] = "",
        attests_clinical_accuracy: Annotated[bool, Form()] = False,
    ) -> Response:
        entered = {
            "clinician_name": clinician_name,
            "credential": credential,
            "npi": npi,
            "note": note,
        }
        try:
            outcome = service.cosign(
                case_id,
                clinician_name=clinician_name,
                credential=credential,
                attests_clinical_accuracy=attests_clinical_accuracy,
                npi=npi or None,
                note=note.strip() or None,
                draft_attempt=draft_attempt,
            )
        except ChecksNotConfirmed as exc:
            return render_clinical(
                request,
                case_id,
                error=exc,
                error_heading="A co-sign needs the attestation",
                error_field="attests_clinical_accuracy",
                form=entered,
                status_code=400,
            )
        except ApprovalError as exc:
            return render_clinical(
                request,
                case_id,
                error=exc,
                error_field=_cosign_field(exc, clinician_name),
                form=entered,
                status_code=exc.status_code,
            )
        except (ActionInFlight, PayloadMismatch) as exc:
            return render_clinical(
                request,
                case_id,
                error=exc,
                error_heading="Another signature is being recorded right now",
                form=entered,
                status_code=409,
            )
        return _see_other(
            case_id,
            "cosigned",
            replay=not outcome.recorded,
            transmit_failed=transmit(case_id),
            path="/clinical",
        )

    # Two paths, one handler. The other two services expose /healthz and the
    # runbook documents /healthz, so a single service answering only /health is
    # a documented endpoint that 404s — exactly the kind of small inconsistency
    # that makes a reader doubt the parts they cannot check.
    auth = load_config()

    @app.middleware("http")
    async def require_session(request: Request, call_next):
        """One door in front of everything that shows a case.

        Health checks stay outside it — Cloud Run probes them before anything
        else, and a health check behind a login reports the service unhealthy
        the moment the login works.
        """
        if not auth.enabled or path_is_public(request.url.path):
            return await call_next(request)
        if session_is_valid(request.cookies.get(COOKIE_NAME), auth):
            return await call_next(request)
        return RedirectResponse("/login", status_code=303)

    @app.get("/login")
    def login_form(request: Request) -> Response:
        if not auth.enabled or session_is_valid(request.cookies.get(COOKIE_NAME), auth):
            return RedirectResponse("/queue", status_code=303)
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login")
    def login(request: Request, password: Annotated[str, Form()] = "") -> Response:
        if not password_matches(password, auth):
            # Deliberately vague, and deliberately not rate limited here —
            # Cloud Run sits behind Google's edge and this is a single shared
            # credential for synthetic data, not an account system.
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": "That password is not correct."},
                status_code=401,
            )
        response = RedirectResponse(_landing_path(service), status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            issue_session(auth),
            max_age=SESSION_HOURS * 3600,
            httponly=True,
            # Secure only where the connection is. A Secure cookie over http is
            # silently dropped by the browser, which on a local run looks
            # exactly like a login that accepts the password and then refuses
            # to let you in. Cloud Run is always https.
            secure=request.url.scheme == "https",
            samesite="lax",
        )
        return response

    @app.post("/theme")
    def set_theme(
        request: Request,
        mode: Annotated[str, Form()] = "auto",
        back: Annotated[str, Form()] = "/",
    ) -> Response:
        """Remember a colour choice, without JavaScript.

        The page ships no script — there is a test asserting that — so the
        preference is a cookie and a redirect rather than localStorage. It also
        means the choice survives with scripting disabled, which is the same
        reason the rest of this interface works without it.
        """
        # Only ever redirect within this app. `back` comes from a form field.
        target = back if back.startswith("/") and not back.startswith("//") else "/"
        response = RedirectResponse(target, status_code=303)
        if mode in ("light", "dark"):
            response.set_cookie("overturn_theme", mode, max_age=60 * 60 * 24 * 365, samesite="lax")
        else:
            response.delete_cookie("overturn_theme")
        return response

    @app.post("/logout")
    def logout() -> Response:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE_NAME)
        return response

    @app.get("/healthz")
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

    # -- the public site ----------------------------------------------------- #
    #
    # The site and the queue are one deployment so the product has one address:
    # a reader lands on the front page, reads how the system works, signs in and
    # reaches the queue without the hostname ever changing.

    def _site_file(asset: str) -> Response:
        filename, media_type = SITE_FILES[asset]
        path = SITE_DIR / filename
        if not path.is_file():
            # Only reachable if the image was built without docs/. Say so rather
            # than 404ing silently: the symptom otherwise (site gone, queue
            # perfectly fine) points nowhere near the Dockerfile.
            logger.error("site asset %s is missing from %s", filename, SITE_DIR)
            return Response("This page is not available in this build.", status_code=404)
        return FileResponse(path, media_type=media_type)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def landing() -> Response:
        """The front door — the same page GitHub Pages serves at the site root."""
        return _site_file("index.html")

    # Declared last on purpose. `/{asset}` matches any single path segment, so
    # it would swallow `/login` and `/queue` if it came first; FastAPI resolves
    # in declaration order and that ordering is the whole safeguard.
    @app.get("/{asset}", include_in_schema=False)
    def site_asset(asset: str) -> Response:
        if asset not in SITE_FILES:
            return Response("Not found", status_code=404, media_type="text/plain")
        return _site_file(asset)

    return app


def _verification_for(case: Any, draft: Any) -> Any:
    """The verdict on the draft being shown, not merely the newest verdict."""
    if draft is None:
        return case.latest_verification
    match = next((v for v in case.verifications if v.attempt == draft.attempt), None)
    return match or case.latest_verification


def _cosign_field(exc: ApprovalError, clinician_name: str) -> str | None:
    """Which control on the co-sign form the refusal belongs to.

    ``MissingReviewer`` names ``decided_by``, which is the clerk's field and does
    not exist on this form. Pointing at a control that is not on screen is worse
    than pointing at none, so the two candidates are told apart here.
    """
    if not isinstance(exc, ApprovalError) or exc.field != "decided_by":
        return None
    return "clinician_name" if not clinician_name.strip() else "credential"


def _see_other(
    case_id: str,
    decided: str,
    replay: bool = False,
    transmit_failed: bool = False,
    path: str = "",
) -> RedirectResponse:
    """303 after a decision, so a refresh cannot resubmit it."""
    suffix = "&replay=1" if replay else ""
    suffix += "&transmit_failed=1" if transmit_failed else ""
    return RedirectResponse(
        f"/case/{quote(case_id, safe='')}{path}?decided={decided}{suffix}", status_code=303
    )


def _notice(decided: str | None, replay: int, transmit_failed: int = 0) -> dict[str, str] | None:
    """The banner shown after a decision.

    The replayed case gets its own heading. Telling a reviewer "Decision
    recorded" when nothing was recorded is the kind of small lie that makes
    someone press the button a third time.

    A failed transmission gets its own too, and it is careful to say that the
    signature *was* recorded. A reviewer told only "that did not work" will sign
    again, and the thing that did not work was the sending.
    """
    if decided in {"approved", "cosigned"} and transmit_failed:
        return {
            "tone": "danger",
            "heading": "Signature recorded — the appeal was not transmitted",
            "body": (
                "The decision is on the record. Sending it to the payer failed, so the "
                "case has been put in front of a person with the reason attached. "
                "Nothing reached the payer that is not shown below."
            ),
        }
    if decided == "cosigned" and replay:
        return {
            "heading": "Already co-signed",
            "body": (
                "This draft already carries a clinician's signature. Nothing further "
                "was recorded — the earlier one stands."
            ),
        }
    if decided == "cosigned":
        return {
            "heading": "Co-sign recorded",
            "body": (
                "The clinical attestation is on the case. If the clerk's approval is "
                "also present, the appeal has been transmitted."
            ),
        }
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
            "body": (
                "Your approval of the paper trail is on the record. Nothing is "
                "transmitted until every required signature is present — the "
                "submission status below says which are."
            ),
        }
    if decided == "rejected":
        return {
            "heading": "Rejection recorded",
            "body": "The case has been returned for human review.",
        }
    return None


def _landing_path(service: ApprovalService) -> str:
    """Where a reviewer lands after signing in: the queue.

    This used to pick the single most instructive case and drop the reviewer
    straight into it, on the theory that a first-time visitor should see the
    product working rather than a list. That was the wrong instinct twice over.
    A clerk signing in wants their work, not a curated exhibit, and dropping
    someone into one case with no sense of what else is waiting is precisely
    what made the interface feel scattered. The queue is the honest front page:
    it shows the whole workload, and every case is one click away.
    """
    return "/queue"
