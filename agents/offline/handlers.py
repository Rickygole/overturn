"""Offline stand-ins for the generative calls.

There are no cloud credits yet, and the pipeline still has to run end to end —
all seven agents, the retry loop, the scheduler, the approval gate — so the
architecture can be exercised rather than described.

The rule these handlers are written to: **derive the answer from the request.**
A lookup table keyed by case id would make every test pass and prove nothing.
Six of the eight handlers below do real work on the prompt they are given:

  * ``sentinel.guard_scan`` re-runs the rule detectors at reduced confidence, so
    the merge logic between layers is genuinely exercised.
  * ``intake.extract`` is a real regex parser over the letter text.
  * ``retrieval.reformulate`` expands abbreviations from a clinical synonym map,
    and has to actually lift the score or the reformulation path is untested.
  * ``drafting.compose`` assembles a letter from the brief in the prompt, and
    honours the sabotage flag by emitting a citation it was never given.
  * ``verification.verify_citation`` computes real content-word overlap between
    the claim and the source text, and can genuinely fail.
  * ``verification.verify_assertions`` does the same for each clinical claim.

Only ``mapping.map_section`` reads a fixture, because the judgement it stands in
for is a genuine clinical-documentation call that no amount of string matching
approximates. Its fixtures quote the chart verbatim — and if a quote drifts,
``sanitise_matrix`` drops it, so the fixtures self-check.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from agents.sentinel.rules import scan
from core.llm import LlmClient, LlmRequest, ScriptedBackend
from core.schemas.criteria import ChartEvidence, CriteriaMatrix, CriterionVerdict
from core.schemas.denial import DenialExtraction, DeniedService
from core.schemas.draft import AppealDraft, Citation
from core.schemas.enums import CriterionVerdictValue
from core.schemas.lifecycle import EscalationDecision
from core.schemas.policy import RetrievalResult
from core.schemas.sentinel import ScreeningResult
from core.schemas.verification import VerificationFinding, VerificationResult

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "data" / "offline"

STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "will",
        "with",
        "which",
        "within",
        "must",
        "been",
        "being",
        "not",
        "no",
        "all",
        "any",
        "member",
        "record",
        "documented",
        "documentation",
    ]
)


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS and len(w) > 2}


def _overlap(claim: str, source: str) -> float:
    """Fraction of the claim's content words present in the source."""
    claim_words = _content_words(claim)
    if not claim_words:
        return 0.0
    return len(claim_words & _content_words(source)) / len(claim_words)


# --------------------------------------------------------------------------- #
# Sentinel
# --------------------------------------------------------------------------- #


def sentinel_guard_scan(request: LlmRequest) -> ScreeningResult:
    """A second detector, deliberately less certain than the rules."""
    match = re.search(
        r"<<<UNTRUSTED_DOCUMENT>>>\n(.*?)\n<<<END_UNTRUSTED_DOCUMENT>>>",
        request.prompt,
        re.DOTALL,
    )
    text = match.group(1) if match else request.prompt
    findings = [
        f.model_copy(update={"detector": "gemma", "confidence": round(f.confidence * 0.85, 2)})
        for f in scan(text)
    ]
    return ScreeningResult(document_uri="offline", content_sha256="0" * 64, findings=findings)


# --------------------------------------------------------------------------- #
# Intake
# --------------------------------------------------------------------------- #

_FIELD_PATTERNS = {
    "member_id": r"Member ID:\s*(\S+)",
    "claim_number": r"Claim Number:\s*(\S+)",
    "patient_name": r"Member Name:\s*(.+)",
    "denial_reason_code": r"Denial reason code:\s*(\S+)",
}


def intake_extract(request: LlmRequest) -> DenialExtraction:
    """A real parser over the letter, not a canned answer."""
    text = request.prompt

    def field(name: str) -> str | None:
        match = re.search(_FIELD_PATTERNS[name], text)
        return match.group(1).strip() if match else None

    services: list[DeniedService] = []
    # A service description wraps onto a second line when it is long, which the
    # cardiac MRI letter does. Matching only single-line descriptions silently
    # produced zero services on that letter.
    for line in re.finditer(
        r"^\s{2}([A-Z][^\n]{10,90}(?:\n\s{2}[a-z][^\n]{0,90})?)\n\s+(?:CPT|HCPCS)\s+(\w+)",
        text,
        re.MULTILINE,
    ):
        description = " ".join(line.group(1).split())
        services.append(DeniedService(description=description, procedure_code=line.group(2)))

    reason_match = re.search(
        r"Reason for determination:\s*\n\s*\n(.*?)(?:\n\s*\n\s{2}This determination)",
        text,
        re.DOTALL,
    )
    reason = (
        " ".join(reason_match.group(1).split())
        if reason_match
        else "Denied as not medically necessary."
    )

    policy_match = re.search(r"Medical Policy\s+(NBH-[A-Z]+-\d+)", text)
    date_match = re.search(r"Date of Notice:\s*([A-Za-z]+ \d+, \d{4})", text)
    deadline_match = re.search(r"on or\n?\s*before ([A-Za-z]+ \d+, \d{4})", text)

    from datetime import datetime

    def parse(raw: str | None):
        if not raw:
            return None
        try:
            return datetime.strptime(raw, "%B %d, %Y").date()
        except ValueError:
            return None

    return DenialExtraction(
        payer_name="Northbeck Health Plan",
        member_id=field("member_id"),
        claim_number=field("claim_number"),
        patient_name=field("patient_name"),
        services=services,
        denial_reason_text=reason,
        denial_reason_code=field("denial_reason_code"),
        date_of_denial=parse(date_match.group(1) if date_match else None),
        appeal_deadline=parse(deadline_match.group(1) if deadline_match else None),
        appeal_window_days=180,
        referenced_policy_hint=policy_match.group(1) if policy_match else None,
    )


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #

SYNONYMS = {
    "cgm": "continuous glucose monitoring sensor transmitter insulin diabetes",
    "continuous glucose": "continuous glucose monitoring systems insulin regimen hypoglycaemia",
    "cardiac": "cardiac magnetic resonance imaging cardiomyopathy echocardiogram",
    "lumbar": "lumbar spine magnetic resonance imaging radiculopathy conservative management",
    "cpap": "continuous positive airway pressure obstructive sleep apnea polysomnogram",
    "positive airway": "positive airway pressure obstructive sleep apnea apnea-hypopnea index",
    "outpatient": "intensive outpatient programme behavioral health severity level of care",
    "genomic": "comprehensive genomic profiling solid tumour advanced metastatic",
}


def retrieval_reformulate(request: LlmRequest) -> RetrievalResult:
    """Expand clinical abbreviations. Must actually improve the score."""
    lowered = request.prompt.lower()
    additions = [expansion for trigger, expansion in SYNONYMS.items() if trigger in lowered]
    original = re.search(r"Original search query:\n(.+)", request.prompt)
    base = original.group(1).strip() if original else ""
    return RetrievalResult(
        query=base,
        reformulated_query=" ".join([base, *additions]).strip() or None,
    )


# --------------------------------------------------------------------------- #
# Mapping
# --------------------------------------------------------------------------- #


def _mapping_fixtures() -> dict:
    path = FIXTURE_DIR / "mapping_answers.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def mapping_map_section(request: LlmRequest) -> CriteriaMatrix:
    """Read verdicts from fixtures for the criteria in this prompt."""
    case_match = re.search(r"case_id '([^']+)'", request.prompt)
    case_id = case_match.group(1) if case_match else "unknown"
    section_match = re.search(r"SECTION (\S+) —", request.prompt)
    section_id = section_match.group(1) if section_match else ""

    answers = _mapping_fixtures().get(case_id, {})
    verdicts: list[CriterionVerdict] = []

    for match in re.finditer(
        r"^CRITERION (\S+)\n(.+?)(?=\n\nCRITERION |\n\n=|\Z)",
        request.prompt,
        re.MULTILINE | re.DOTALL,
    ):
        criterion_id = match.group(1)
        criterion_text = " ".join(match.group(2).split())[:400]
        answer = answers.get(criterion_id)
        if answer is None:
            verdicts.append(
                CriterionVerdict(
                    criterion_id=criterion_id,
                    criterion_text=criterion_text,
                    section_id=section_id,
                    verdict=CriterionVerdictValue.INSUFFICIENT_DOCUMENTATION,
                    reasoning="The record does not address this criterion.",
                    confidence=0.6,
                )
            )
            continue

        verdicts.append(
            CriterionVerdict(
                criterion_id=criterion_id,
                criterion_text=criterion_text,
                section_id=section_id,
                verdict=CriterionVerdictValue(answer["verdict"]),
                evidence=[
                    ChartEvidence(locator=e["locator"], quote=e["quote"])
                    for e in answer.get("evidence", [])
                ],
                reasoning=answer.get("reasoning", ""),
                confidence=answer.get("confidence", 0.8),
            )
        )

    return CriteriaMatrix(case_id=case_id, verdicts=verdicts)


# --------------------------------------------------------------------------- #
# Drafting
# --------------------------------------------------------------------------- #


def drafting_compose(request: LlmRequest) -> AppealDraft:
    """Assemble a letter from the brief actually present in the prompt."""
    prompt = request.prompt
    case_match = re.search(r"CASE[- ]?(\S+)", prompt)
    claim = re.search(r"CLAIM NUMBER: (\S+)", prompt)
    service = re.search(r"SERVICE DENIED: (.+)", prompt)
    attempt_match = re.search(r"THIS IS ATTEMPT (\d+)", prompt)
    attempt = int(attempt_match.group(1)) if attempt_match else 1

    rejected: set[str] = set(re.findall(r"Remove the citation to (\S+)", prompt))
    rejected |= set(re.findall(r"citation to (\S+) misstates", prompt))
    dropped_claims = set(
        re.findall(r"not supported by any row in\s+the criteria matrix: '(.+?)'", prompt)
    )

    criteria = re.findall(
        r"CRITERION (\S+)  \(in section (\S+)\)\n  Criterion text: (.+?)\n"
        r"  Why it is met:  (.+?)\n",
        prompt,
        re.DOTALL,
    )
    quotes = re.findall(r'- \[([^\]]+)\] "(.+?)"', prompt)

    citations: list[Citation] = []
    assertions: list[str] = []
    paragraphs: list[str] = []

    for criterion_id, section_id, text, reason in criteria:
        if criterion_id in rejected or section_id in rejected:
            continue
        citations.append(
            Citation(
                section_id=criterion_id,
                claim=" ".join(text.split())[:300],
                supporting_criterion_ids=[criterion_id],
            )
        )
        paragraphs.append(
            f"Criterion {criterion_id} requires: {' '.join(text.split())[:220]} "
            f"The record satisfies this. {' '.join(reason.split())[:220]}"
        )

    for locator, quote in quotes:
        statement = f"The record at {locator} states: {quote}"
        if statement not in dropped_claims and quote not in dropped_claims:
            assertions.append(statement)

    if request.system and "FAULT-INJECTION" in request.system:
        # Deliberate fabrication, used only to prove Verification catches it.
        citations.append(
            Citation(
                section_id="NBH-CARD-014-9.9",
                claim="This section provides that any relative contraindication is "
                "deemed addressed where the ordering clinician requests the study.",
                supporting_criterion_ids=[],
            )
        )
        assertions.append(
            "The patient's pacemaker has been confirmed as MRI-conditional and the "
            "contraindication has been addressed."
        )
        paragraphs.append(
            "Under NBH-CARD-014-9.9, the relative contraindication is deemed addressed."
        )

    body = "\n\n".join(
        [
            f"Re: Appeal of adverse benefit determination, claim "
            f"{claim.group(1) if claim else '(number not stated)'}",
            f"We are appealing the denial of {service.group(1) if service else 'the service'}. "
            f"The record documents what the plan's published criteria require, "
            f"addressed below criterion by criterion.",
            *paragraphs,
            "We request that the denial be overturned and the claim processed for payment.",
        ]
    )

    return AppealDraft(
        case_id=case_match.group(1) if case_match else "unknown",
        attempt=attempt,
        subject_line=f"Appeal of adverse benefit determination — claim "
        f"{claim.group(1) if claim else 'unknown'}",
        body=body,
        citations=citations,
        clinical_assertions=assertions,
    )


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #

CITATION_SUPPORT_FLOOR = 0.45
ASSERTION_SUPPORT_FLOOR = 0.55


def verification_verify_citation(request: LlmRequest) -> VerificationResult:
    """Real overlap between the claim and the source. Can genuinely fail."""
    source = re.search(r"verbatim:\n\n(.*?)\n\n=+", request.prompt, re.DOTALL)
    claim = re.search(r"THE LETTER ASSERTS THAT THIS SECTION:\n(.+?)\n", request.prompt)
    section = re.search(r"SOURCE TEXT for (\S+)", request.prompt)

    if not source or not claim:
        return VerificationResult(case_id="offline", attempt=1)

    score = _overlap(claim.group(1), source.group(1))
    if score >= CITATION_SUPPORT_FLOOR:
        return VerificationResult(case_id="offline", attempt=1, citations_checked=1)

    section_id = section.group(1) if section else "unknown"
    return VerificationResult(
        case_id="offline",
        attempt=1,
        citations_checked=1,
        citations_unsupported=[section_id],
        findings=[
            VerificationFinding(
                check="citation_accurate",
                severity="fatal",
                locus=section_id,
                detail=(
                    f"The source text for {section_id} does not support the claim "
                    f"made about it (content overlap {score:.0%}). Restate it from "
                    f"the section's actual wording or drop the point."
                ),
            )
        ],
    )


def verification_verify_assertions(request: LlmRequest) -> VerificationResult:
    """Each clinical claim against the chart evidence, by real overlap."""
    evidence = re.search(r"CHART EVIDENCE, complete:\n\n(.*?)\n\n=+", request.prompt, re.DOTALL)
    assertions_block = re.search(
        r"ASSERTIONS THE LETTER MAKES:\n(.*?)\n\nWhich", request.prompt, re.DOTALL
    )
    if not assertions_block:
        return VerificationResult(case_id="offline", attempt=1)

    corpus = evidence.group(1) if evidence else ""
    ungrounded: list[str] = []
    findings: list[VerificationFinding] = []

    for line in assertions_block.group(1).splitlines():
        assertion = line.lstrip("- ").strip()
        if not assertion:
            continue
        if _overlap(assertion, corpus) < ASSERTION_SUPPORT_FLOOR:
            ungrounded.append(assertion)
            findings.append(
                VerificationFinding(
                    check="assertion_grounded",
                    severity="fatal",
                    locus=assertion[:80],
                    detail=(
                        f"No chart evidence supports this claim. Remove it or "
                        f"replace it with something the record documents: {assertion!r}"
                    ),
                )
            )

    return VerificationResult(
        case_id="offline",
        attempt=1,
        ungrounded_assertions=ungrounded,
        findings=findings,
    )


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


def lifecycle_decide(request: LlmRequest) -> EscalationDecision:
    """Prose only. Every structural field is overwritten by the ladder."""
    from core.schemas.enums import ActionType, AppealLevel, CaseStatus

    case = re.search(r"CASE (\S+)", request.prompt)
    stage = re.search(r"Current stage: (\S+)", request.prompt)
    window = re.search(r"Response window: (\d+) days", request.prompt)
    following = re.search(r"already determined: (\S+)", request.prompt)

    case_id = case.group(1) if case else "unknown"
    next_name = (following.group(1) if following else "none").replace("_", " ")
    days = window.group(1) if window else "the published"

    return EscalationDecision(
        case_id=case_id,
        current_level=AppealLevel(stage.group(1)) if stage else AppealLevel.FIRST_LEVEL,
        next_status=CaseStatus.ESCALATED,
        action=ActionType.ESCALATE,
        rationale=(
            f"The appeal was submitted and the payer's {days}-day response window "
            f"has closed with no response received. Advancing to {next_name} under "
            f"the plan's published appeal process."
        ),
        notify_message=(
            f"Case {case_id}: no payer response within {days} days. Escalating to {next_name}."
        ),
    )


# --------------------------------------------------------------------------- #


def install(backend: ScriptedBackend) -> ScriptedBackend:
    """Register every handler the pipeline needs."""
    backend.register("sentinel", "guard_scan", sentinel_guard_scan)
    backend.register("intake", "extract", intake_extract)
    backend.register("retrieval", "reformulate", retrieval_reformulate)
    backend.register("mapping", "map_section", mapping_map_section)
    backend.register("drafting", "compose", drafting_compose)
    backend.register("verification", "verify_citation", verification_verify_citation)
    backend.register("verification", "verify_assertions", verification_verify_assertions)
    backend.register("lifecycle", "decide", lifecycle_decide)
    return backend


def build_offline_llm() -> LlmClient:
    """A client that needs no network and no credits."""
    return LlmClient(install(ScriptedBackend()))
