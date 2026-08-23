"""Closed vocabularies shared across every inter-agent contract.

These are strings rather than integers on purpose: they are written verbatim
into Firestore and into the audit log, where a human reads them.
"""

from __future__ import annotations

from enum import StrEnum


class CaseStatus(StrEnum):
    """The lifecycle of a single denied claim.

    Statuses advance monotonically except for ``drafting`` <-> ``verifying``,
    which cycles while Verification is rejecting drafts, and the terminal states
    reachable from anywhere.
    """

    RECEIVED = "received"
    SCREENING = "screening"
    QUARANTINED = "quarantined"
    EXTRACTED = "extracted"
    RETRIEVING = "retrieving"
    MAPPING = "mapping"
    DRAFTING = "drafting"
    VERIFYING = "verifying"
    AWAITING_APPROVAL = "awaiting_human_approval"
    APPROVED = "approved"
    SUBMITTED = "submitted"
    PAYER_RESPONDED = "payer_responded"
    ESCALATED = "escalated"
    OVERTURNED = "overturned"
    UPHELD = "upheld"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    DECLINED_NO_BASIS = "declined_no_basis"
    FAILED = "failed"


TERMINAL_STATUSES: frozenset[CaseStatus] = frozenset(
    {
        CaseStatus.QUARANTINED,
        CaseStatus.OVERTURNED,
        CaseStatus.UPHELD,
        CaseStatus.DECLINED_NO_BASIS,
        CaseStatus.FAILED,
    }
)


class CriterionVerdictValue(StrEnum):
    """Whether the chart documents what a single policy criterion demands."""

    SATISFIED = "satisfied"
    NOT_SATISFIED = "not_satisfied"
    INSUFFICIENT_DOCUMENTATION = "insufficient_documentation"
    NOT_APPLICABLE = "not_applicable"


class AppealLevel(StrEnum):
    """Rungs of the appeal ladder, in the order Lifecycle climbs them."""

    FIRST_LEVEL = "first_level_appeal"
    PEER_TO_PEER = "peer_to_peer_review"
    SECOND_LEVEL = "second_level_appeal"
    EXTERNAL_REVIEW = "independent_external_review"


class ActionType(StrEnum):
    """Every action with an effect outside this system.

    Each one is routed through the idempotency guard, keyed by
    ``(case_id, action_type, attempt)``.
    """

    CREATE_CASE = "create_case"
    QUARANTINE_DOCUMENT = "quarantine_document"
    NOTIFY_HUMAN = "notify_human"
    # Recording a human decision is its own action, not a notification.
    #
    # These were once both NOTIFY_HUMAN, and the two callers numbered their
    # attempts on different things: the pipeline counted notifications sent, the
    # approval interface used the draft attempt being approved. Both produced
    # "notify_human:1" for the ordinary case, with different payloads, so the
    # guard correctly reported a payload mismatch and refused — permanently, on
    # every retry. A clerk could never approve a case that verified on its first
    # attempt, which is every clean case.
    #
    # Two counters that mean different things must never share a namespace.
    RECORD_APPROVAL = "record_approval"
    RECORD_REJECTION = "record_rejection"
    RECORD_COSIGN = "record_cosign"
    SUBMIT_APPEAL = "submit_appeal"
    ESCALATE = "escalate"
    REQUEST_PEER_REVIEW = "request_peer_review"
    FILE_EXTERNAL_REVIEW = "file_external_review"
    CLOSE_CASE = "close_case"


class AgentName(StrEnum):
    """The seven agents. Used as the audit-log author and the trace span name."""

    SENTINEL = "sentinel"
    INTAKE = "intake"
    RETRIEVAL = "retrieval"
    MAPPING = "mapping"
    DRAFTING = "drafting"
    VERIFICATION = "verification"
    LIFECYCLE = "lifecycle"
    ORCHESTRATOR = "orchestrator"


class ThreatCategory(StrEnum):
    """What Sentinel found in an inbound document."""

    PROMPT_INJECTION = "prompt_injection"
    INSTRUCTION_CONTENT = "instruction_content"
    TOOL_POISONING = "tool_poisoning"
    UNEXPECTED_PII = "unexpected_pii"
    SUSPICIOUS_ENCODING = "suspicious_encoding"
