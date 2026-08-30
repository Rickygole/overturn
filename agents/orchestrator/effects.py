"""Actions with effects outside this system.

Every function here is passed to :meth:`IdempotencyGuard.execute` as the thing
to do once. None of them is ever called directly, and that is the point: these
are the operations where doing it twice is worse than not doing it at all.

The payer submission is a simulated endpoint. Overturn does not transmit
anything to a real insurer, and the README says so. What is real is the shape:
an idempotent claim, an external call, a recorded confirmation.

Every write below goes through ``GatewayHandle.authorize`` under the identity
that actually owns the action, the same as every other datastore consumer in
this codebase (``core/state.py``, ``core/audit.py``, ``core/memory.py``). This
used to reach ``fleet.store.set`` directly, unauthorized, which briefly made
this the one function in the fleet where "no second door" (``core/gateway.py``)
was false. Quarantining a document is scoped to Sentinel, which is the agent
`POLICY` actually grants ``quarantine: WRITE`` — the orchestrator itself only
holds ``READ`` on that collection, on purpose, since a write it can reach
unchecked is a write the policy is not really enforcing.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any

from core.gateway import Access
from core.schemas.base import utcnow
from core.schemas.draft import AppealDraft
from core.schemas.lifecycle import EscalationDecision
from core.schemas.sentinel import ScreeningResult

if TYPE_CHECKING:
    from agents.orchestrator.deps import Fleet

logger = logging.getLogger(__name__)


def quarantine_document(fleet: Fleet, case_id: str, screening: ScreeningResult) -> dict[str, Any]:
    """Record a rejected document where a human can review the decision.

    The document text is not copied here. A quarantined document is exactly the
    thing not to spread further into the system; the hash and the findings are
    enough to review the call, and the original stays where it landed.
    """
    record = {
        "case_id": case_id,
        "document_uri": screening.document_uri,
        "content_sha256": screening.content_sha256,
        "quarantined_at": utcnow().isoformat(),
        "findings": [f.to_firestore() for f in screening.findings],
        "layers_run": screening.layers_run,
    }
    collection = fleet.sentinel.deps.gateway.authorize("quarantine", Access.WRITE)
    fleet.store.set(collection, screening.content_sha256, record)
    logger.warning(
        "quarantined %s for case %s: %d finding(s)",
        screening.document_uri,
        case_id,
        len(screening.findings),
    )
    return {"quarantined": True, "sha256": screening.content_sha256}


def notify_human(fleet: Fleet, case_id: str, reason: str) -> dict[str, Any]:
    """Tell a person a case needs them.

    A log line and a stored record here. In deployment this is a Pub/Sub publish
    to whatever the clinic actually reads.
    """
    payload = {
        "case_id": case_id,
        "reason": reason,
        "at": utcnow().isoformat(),
    }
    collection = fleet.orchestrator.gateway.authorize("case_memory", Access.WRITE)
    fleet.store.set(
        collection,
        f"{case_id}:notification:{hashlib.sha256(reason.encode()).hexdigest()[:12]}",
        payload,
    )
    logger.info("notify: %s — %s", case_id, reason)
    return payload


def submit_appeal(fleet: Fleet, case_id: str, draft: AppealDraft) -> dict[str, Any]:
    """Transmit an approved appeal to the payer.

    The one action in the system where doing it twice has a real cost: a
    duplicate appeal on one claim confuses the payer's own process and can reset
    a review clock. This function is only ever reached through the idempotency
    guard, and only after both signatures are present.
    """
    from services.payer_sim import submit as payer_submit

    confirmation = payer_submit(
        case_id=case_id,
        subject=draft.subject_line,
        body=draft.body,
        citations=[c.section_id for c in draft.citations],
    )
    logger.info("submitted appeal for %s, confirmation %s", case_id, confirmation)
    return {
        "confirmation": confirmation,
        "submitted_at": utcnow().isoformat(),
        "draft_attempt": draft.attempt,
        "citation_count": len(draft.citations),
    }


def escalate(fleet: Fleet, case_id: str, decision: EscalationDecision) -> dict[str, Any]:
    """Move an unanswered appeal to the next rung."""
    from services.payer_sim import escalate as payer_escalate

    reference = payer_escalate(
        case_id=case_id,
        level=decision.next_level.value if decision.next_level else "closed",
        rationale=decision.rationale,
    )
    logger.info(
        "escalated %s to %s, reference %s",
        case_id,
        decision.next_level.value if decision.next_level else "(closed)",
        reference,
    )
    return {
        "reference": reference,
        "level": decision.next_level.value if decision.next_level else None,
        "at": utcnow().isoformat(),
    }
