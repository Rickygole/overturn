"""Contract tests.

These exist because the agents hand these objects to each other unvalidated by
anything else. If a contract loosens, this is where it should break.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.schemas import (
    APPEAL_LADDER,
    AppealDraft,
    AppealLevel,
    CaseRecord,
    CaseStatus,
    ChartEvidence,
    Citation,
    CriteriaMatrix,
    CriterionVerdict,
    CriterionVerdictValue,
    DenialExtraction,
    PolicyCriterion,
    RetrievalResult,
    RetrievedSection,
    VerificationResult,
)


def _section(section_id: str = "MHP-CARD-014-3") -> RetrievedSection:
    return RetrievedSection(
        section_id=section_id,
        policy_id="MHP-CARD-014",
        policy_title="Advanced Cardiac Imaging",
        section_heading="Coverage Criteria",
        text="Coverage is provided when all of the following criteria are documented.",
        criteria=[
            PolicyCriterion(
                criterion_id=f"{section_id}.1",
                text="Symptoms persisting for at least six weeks.",
            ),
            PolicyCriterion(
                criterion_id=f"{section_id}.2",
                text="A prior stress test performed within the preceding twelve months.",
            ),
        ],
        similarity=0.83,
        matched_query="cardiac MRI denied as not medically necessary",
    )


class TestStrictness:
    def test_unexpected_key_is_rejected(self):
        """A drifted contract must fail at the boundary, not downstream."""
        with pytest.raises(ValidationError):
            DenialExtraction(
                payer_name="Meridian Health Plan",
                denial_reason_text="Not medically necessary",
                totally_new_field="surprise",
            )

    def test_short_policy_text_is_rejected(self):
        with pytest.raises(ValidationError):
            RetrievedSection(
                section_id="MHP-CARD-014-3",
                policy_id="MHP-CARD-014",
                policy_title="t",
                section_heading="h",
                text="too short",
                similarity=0.5,
                matched_query="q",
            )

    def test_similarity_is_bounded(self):
        with pytest.raises(ValidationError):
            _section().model_copy(update={"similarity": 1.4}).model_validate(
                {**_section().model_dump(), "similarity": 1.4}
            )


class TestRetrievalClosedWorld:
    def test_section_ids_include_nested_criteria(self):
        """Drafting may cite a criterion id, not only its parent section."""
        result = RetrievalResult(query="q", sections=[_section()], top_similarity=0.83)
        assert result.section_ids() == {
            "MHP-CARD-014-3",
            "MHP-CARD-014-3.1",
            "MHP-CARD-014-3.2",
        }

    def test_empty_retrieval_reports_no_applicable_policy(self):
        result = RetrievalResult(query="q", no_applicable_policy=True)
        assert result.section_ids() == set()
        assert result.no_applicable_policy


class TestCriteriaMatrix:
    def test_satisfied_without_evidence_is_not_usable(self):
        """A satisfied verdict with no chart citation is an unsupported claim."""
        verdict = CriterionVerdict(
            criterion_id="MHP-CARD-014-3.1",
            criterion_text="t",
            section_id="MHP-CARD-014-3",
            verdict=CriterionVerdictValue.SATISFIED,
            evidence=[],
            reasoning="asserted without a chart pointer",
            confidence=0.9,
        )
        assert verdict.usable_in_appeal is False

    def test_appealable_basis_requires_satisfied_to_outweigh_failures(self):
        def row(cid: str, v: CriterionVerdictValue, evidence: bool) -> CriterionVerdict:
            return CriterionVerdict(
                criterion_id=cid,
                criterion_text="t",
                section_id="MHP-CARD-014-3",
                verdict=v,
                evidence=(
                    [ChartEvidence(locator="note 2026-03-14", quote="documented")]
                    if evidence
                    else []
                ),
                reasoning="r",
                confidence=0.8,
            )

        mostly_good = CriteriaMatrix(
            case_id="c1",
            verdicts=[
                row("a", CriterionVerdictValue.SATISFIED, True),
                row("b", CriterionVerdictValue.SATISFIED, True),
                row("c", CriterionVerdictValue.NOT_SATISFIED, False),
            ],
        )
        assert mostly_good.has_appealable_basis is True
        assert len(mostly_good.appealable_verdicts()) == 2

        mostly_bad = CriteriaMatrix(
            case_id="c1",
            verdicts=[
                row("a", CriterionVerdictValue.SATISFIED, True),
                row("b", CriterionVerdictValue.NOT_SATISFIED, False),
                row("c", CriterionVerdictValue.NOT_SATISFIED, False),
            ],
        )
        assert mostly_bad.has_appealable_basis is False


class TestVerification:
    def test_nonexistent_citation_fails_and_produces_feedback(self):
        result = VerificationResult(
            case_id="c1",
            attempt=1,
            citations_checked=2,
            citations_nonexistent=["MHP-CARD-099-9.9"],
        )
        assert result.passed is False
        instructions = result.revision_instructions()
        assert len(instructions) == 1
        assert "MHP-CARD-099-9.9" in instructions[0]

    def test_clean_result_passes(self):
        assert VerificationResult(case_id="c1", attempt=2, citations_checked=3).passed


class TestCaseRecord:
    def test_transition_records_history_and_bumps_revision(self):
        case = CaseRecord(case_id="c1", source_document_uri="gs://b/d.pdf")
        case.transition(CaseStatus.SCREENING, actor="orchestrator")
        case.transition(CaseStatus.EXTRACTED, actor="intake", note="3 services parsed")

        assert case.status == CaseStatus.EXTRACTED
        assert case.revision == 2
        assert [h.to_status for h in case.history] == [
            CaseStatus.SCREENING,
            CaseStatus.EXTRACTED,
        ]
        assert case.history[1].from_status == CaseStatus.SCREENING

    def test_overdue_is_false_unless_submitted(self):
        case = CaseRecord(case_id="c1", source_document_uri="gs://b/d.pdf")
        case.set_response_deadline(days=0, accelerated_seconds_per_day=None)
        assert case.is_overdue is False

        case.status = CaseStatus.SUBMITTED
        assert case.is_overdue is True

    def test_approved_draft_returns_the_exact_attempt_signed_off(self):
        from core.schemas import HumanDecision

        case = CaseRecord(case_id="c1", source_document_uri="gs://b/d.pdf")
        case.drafts = [
            AppealDraft(case_id="c1", attempt=1, subject_line="s", body="b" * 40),
            AppealDraft(case_id="c1", attempt=2, subject_line="s", body="b" * 40),
        ]
        case.human_decision = HumanDecision(
            decided_by="clerk@clinic.example",
            approved=True,
            draft_attempt_approved=1,
        )
        approved = case.approved_draft()
        assert approved is not None
        assert approved.attempt == 1

    def test_unapproved_case_has_no_approved_draft(self):
        from core.schemas import HumanDecision

        case = CaseRecord(case_id="c1", source_document_uri="gs://b/d.pdf")
        case.drafts = [AppealDraft(case_id="c1", attempt=1, subject_line="s", body="b" * 40)]
        case.human_decision = HumanDecision(decided_by="clerk", approved=False)
        assert case.approved_draft() is None


class TestAppealLadder:
    def test_ladder_is_a_connected_chain_ending_in_external_review(self):
        level = AppealLevel.FIRST_LEVEL
        visited = []
        while level is not None:
            visited.append(level)
            level = APPEAL_LADDER[level].next_level
        assert visited == [
            AppealLevel.FIRST_LEVEL,
            AppealLevel.PEER_TO_PEER,
            AppealLevel.SECOND_LEVEL,
            AppealLevel.EXTERNAL_REVIEW,
        ]

    def test_every_level_has_a_rung(self):
        assert set(APPEAL_LADDER) == set(AppealLevel)


class TestDraft:
    def test_cited_ids_collects_every_citation(self):
        draft = AppealDraft(
            case_id="c1",
            attempt=1,
            subject_line="Appeal",
            body="body text long enough to be real",
            citations=[
                Citation(section_id="MHP-CARD-014-3.1", claim="requires six weeks of symptoms"),
                Citation(section_id="MHP-CARD-014-3.2", claim="requires a prior stress test"),
            ],
        )
        assert draft.cited_ids() == {"MHP-CARD-014-3.1", "MHP-CARD-014-3.2"}
