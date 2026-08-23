"""The Verification agent: the reason this system can be trusted to send a letter.

Three checks, in increasing order of how much they cost and how much they can
be wrong about:

1. **Does the cited identifier exist?** Pure Python set membership against the
   retrieved policy. Cannot hallucinate. Runs first because it is free, and a
   draft that fails it is not worth spending model calls on.
2. **Does the source text support the claim?** One model call per citation, each
   shown the section text and the claim and nothing else.
3. **Is every clinical assertion grounded?** One model call against the chart
   evidence in the criteria matrix.

Any fatal finding rejects the draft. The findings are turned into revision
instructions and handed back to Drafting; they are not discarded, because "try
again" without saying what was wrong produces the same draft.
"""

from __future__ import annotations

from dataclasses import dataclass

from agents.base import OverturnAgent
from agents.verification.checks import (
    check_assertions_enumerated,
    check_citation_existence,
    check_supporting_criteria,
    evidence_corpus,
    resolve_section_text,
)
from core.audit import Recording
from core.schemas.criteria import CriteriaMatrix
from core.schemas.draft import AppealDraft
from core.schemas.enums import AgentName
from core.schemas.policy import RetrievalResult
from core.schemas.verification import VerificationFinding, VerificationResult

from .prompts import ASSERTION_SYSTEM, CITATION_SYSTEM


@dataclass(frozen=True)
class VerificationRequest:
    """A draft, and the closed world it was supposed to be written from."""

    draft: AppealDraft
    retrieval: RetrievalResult
    matrix: CriteriaMatrix

    def to_firestore(self) -> dict:
        return {
            "case_id": self.draft.case_id,
            "attempt": self.draft.attempt,
            "cited_ids": sorted(self.draft.cited_ids()),
        }


class VerificationAgent(OverturnAgent[VerificationRequest, VerificationResult]):
    """Checks a draft before any human sees it."""

    name = AgentName.VERIFICATION
    operation = "verify"

    def _summarise(self, request: VerificationRequest) -> str:
        return (
            f"attempt {request.draft.attempt}, {len(request.draft.citations)} citations, "
            f"{len(request.draft.clinical_assertions)} assertions"
        )

    def _execute(
        self,
        case_id: str,
        request: VerificationRequest,
        rec: Recording,
        attempt: int,
    ) -> VerificationResult:
        draft = request.draft
        findings: list[VerificationFinding] = []

        # Check 1 — free, deterministic, and the one that catches a fabrication.
        nonexistent, existence_findings = check_citation_existence(draft, request.retrieval)
        findings.extend(existence_findings)
        findings.extend(check_supporting_criteria(draft, request.matrix))
        findings.extend(check_assertions_enumerated(draft))

        if nonexistent:
            # A draft citing a section that does not exist is already rejected.
            # Paying for semantic checks on it would be spending money to
            # confirm a conclusion already reached.
            rec.decision = (
                f"attempt {draft.attempt} REJECTED without model calls: "
                f"{len(nonexistent)} fabricated citation(s) — {', '.join(nonexistent)}"
            )
            return VerificationResult(
                case_id=case_id,
                attempt=draft.attempt,
                citations_checked=len(draft.citations),
                citations_nonexistent=nonexistent,
                findings=findings,
            )

        # Check 2 — one call per citation, each starved of context.
        unsupported: list[str] = []
        tokens_in = tokens_out = 0
        model_used: str | None = None

        for citation in draft.citations:
            source = resolve_section_text(citation.section_id, request.retrieval)
            if source is None:
                continue
            result, response = self.llm.structured(
                agent=self.name.value,
                operation="verify_citation",
                system=CITATION_SYSTEM,
                prompt=(
                    f"SOURCE TEXT for {citation.section_id}, verbatim:\n\n{source}\n\n"
                    f"{'=' * 60}\n"
                    f"THE LETTER ASSERTS THAT THIS SECTION:\n{citation.claim}\n\n"
                    f"Does the source text support that assertion?"
                ),
                schema=VerificationResult,
                model=self.settings.model_flash,
            )
            model_used = response.model
            tokens_in += response.input_tokens or 0
            tokens_out += response.output_tokens or 0
            if result.citations_unsupported or result.findings:
                unsupported.append(citation.section_id)
                findings.extend(
                    f.model_copy(update={"source_text": source[:600]}) for f in result.findings
                )
                if not result.findings:
                    findings.append(
                        VerificationFinding(
                            check="citation_accurate",
                            severity="fatal",
                            locus=citation.section_id,
                            detail=(
                                f"{citation.section_id} does not say what the letter "
                                f"claims it says: {citation.claim!r}"
                            ),
                            source_text=source[:600],
                        )
                    )

        # Check 3 — every clinical claim against the chart evidence.
        ungrounded: list[str] = []
        if draft.clinical_assertions:
            evidence = evidence_corpus(request.matrix)
            assertions = "\n".join(f"- {a}" for a in draft.clinical_assertions)
            result, response = self.llm.structured(
                agent=self.name.value,
                operation="verify_assertions",
                system=ASSERTION_SYSTEM,
                prompt=(
                    f"CHART EVIDENCE, complete:\n\n{evidence or '(no evidence recorded)'}\n\n"
                    f"{'=' * 60}\n"
                    f"ASSERTIONS THE LETTER MAKES:\n{assertions}\n\n"
                    f"Which are not supported by the evidence above?"
                ),
                schema=VerificationResult,
                model=self.settings.model_flash,
            )
            model_used = response.model
            tokens_in += response.input_tokens or 0
            tokens_out += response.output_tokens or 0
            ungrounded = list(result.ungrounded_assertions)
            findings.extend(result.findings)

        verification = VerificationResult(
            case_id=case_id,
            attempt=draft.attempt,
            citations_checked=len(draft.citations),
            citations_nonexistent=nonexistent,
            citations_unsupported=unsupported,
            ungrounded_assertions=ungrounded,
            findings=findings,
            checked_by_model=model_used,
        )

        rec.model = model_used
        rec.input_tokens = tokens_in or None
        rec.output_tokens = tokens_out or None
        rec.decision = self._describe(verification)
        return verification

    @staticmethod
    def _describe(result: VerificationResult) -> str:
        if result.passed:
            return (
                f"attempt {result.attempt} PASSED: {result.citations_checked} "
                f"citation(s) verified against source text, all assertions grounded"
            )
        reasons = []
        if result.citations_nonexistent:
            reasons.append(f"{len(result.citations_nonexistent)} nonexistent citation(s)")
        if result.citations_unsupported:
            reasons.append(f"{len(result.citations_unsupported)} unsupported citation(s)")
        if result.ungrounded_assertions:
            reasons.append(f"{len(result.ungrounded_assertions)} ungrounded assertion(s)")
        return f"attempt {result.attempt} REJECTED: " + ", ".join(reasons)
