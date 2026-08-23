"""Tests for whether a case is worth appealing at all.

The interesting property is not that the system can write an appeal. It is that
it declines to write one when the chart does not answer the question the payer
asked — and that it can tell that apart from a chart that merely has a gap
somewhere.

Two cases in the corpus are the whole test. Both have a criterion marked
`insufficient_documentation` in an otherwise strong chart. One should proceed
and one should not, and no count of satisfied criteria distinguishes them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.base import build_deps
from agents.mapping.dispute import (
    disputed_criteria,
    has_answerable_dispute,
    primary_disputed_criteria,
)
from agents.offline.handlers import build_offline_llm, intake_extract
from agents.retrieval.agent import RetrievalAgent, RetrievalRequest
from core.llm import LlmRequest
from core.schemas.criteria import ChartEvidence, CriteriaMatrix, CriterionVerdict
from core.schemas.enums import AgentName, CriterionVerdictValue
from core.store import MemoryStore

DENIALS = Path(__file__).resolve().parents[1] / "data" / "denials"


def _denial(case_id: str):
    return intake_extract(
        LlmRequest(
            agent="intake",
            operation="extract",
            system="",
            prompt=(DENIALS / f"{case_id}.txt").read_text(),
            model="offline",
        )
    )


def _retrieval(case_id: str, denial):
    agent = RetrievalAgent(build_deps(MemoryStore(), AgentName.RETRIEVAL, build_offline_llm()))
    return agent.run(case_id, RetrievalRequest(denial=denial))


class TestIdentifyingWhatWasDisputed:
    @pytest.mark.parametrize(
        "case_id,expected",
        [
            ("CASE-003", "NBH-CARD-014-3.3"),  # initial evaluation inconclusive
            ("CASE-006", "NBH-BEHV-045-3.4"),  # less intensive level of care
        ],
    )
    def test_the_payers_reason_maps_to_the_right_criterion(self, case_id, expected):
        denial = _denial(case_id)
        assert expected in primary_disputed_criteria(
            case_id and denial, _retrieval(case_id, denial)
        )

    def test_only_the_strongest_matches_count_as_primary(self):
        """Vocabulary bleed is not a dispute.

        "Care", "intensive" and "level" appear across half a behavioural health
        policy. Treating every weak match as disputed made a case answerable
        whenever something adjacent happened to be documented.
        """
        denial = _denial("CASE-006")
        retrieval = _retrieval("CASE-006", denial)
        everything = disputed_criteria(denial, retrieval)
        primary = primary_disputed_criteria(denial, retrieval)

        assert len(primary) < len(everything)
        assert primary == ["NBH-BEHV-045-3.4"]

    def test_a_boilerplate_reason_yields_nothing(self):
        denial = _denial("CASE-003")
        denial.denial_reason_text = "The request does not meet the plan's criteria."
        assert primary_disputed_criteria(denial, _retrieval("CASE-003", denial)) == []


def _matrix(**verdicts: str) -> CriteriaMatrix:
    rows = []
    for criterion_id, value in verdicts.items():
        criterion_id = criterion_id.replace("_", "-").replace("--", ".")
        verdict = CriterionVerdictValue(value)
        rows.append(
            CriterionVerdict(
                criterion_id=criterion_id,
                criterion_text="t",
                section_id=criterion_id.rsplit(".", 1)[0],
                verdict=verdict,
                evidence=(
                    [ChartEvidence(locator="enc/x", quote="documented in the note")]
                    if verdict is CriterionVerdictValue.SATISFIED
                    else []
                ),
                reasoning="r",
                confidence=0.9,
            )
        )
    return CriteriaMatrix(case_id="c", verdicts=rows)


class TestAnswerability:
    def test_a_documented_disputed_criterion_is_answerable(self):
        matrix = _matrix(**{"NBH-CARD-014-3--3": "satisfied"})
        answerable, why = has_answerable_dispute(matrix, ["NBH-CARD-014-3.3"])
        assert answerable is True
        assert "point to argue" in why

    def test_an_undocumented_disputed_criterion_is_not(self):
        matrix = _matrix(**{"NBH-BEHV-045-3--4": "insufficient_documentation"})
        answerable, why = has_answerable_dispute(matrix, ["NBH-BEHV-045-3.4"])
        assert answerable is False
        assert "silent on exactly that" in why
        assert "obtain that note" in why

    def test_a_satisfied_verdict_with_no_evidence_does_not_count(self):
        """An assertion is not documentation."""
        matrix = CriteriaMatrix(
            case_id="c",
            verdicts=[
                CriterionVerdict(
                    criterion_id="NBH-BEHV-045-3.4",
                    criterion_text="t",
                    section_id="NBH-BEHV-045-3",
                    verdict=CriterionVerdictValue.SATISFIED,
                    evidence=[],
                    reasoning="asserted without a chart pointer",
                    confidence=0.9,
                )
            ],
        )
        answerable, _ = has_answerable_dispute(matrix, ["NBH-BEHV-045-3.4"])
        assert answerable is False

    def test_one_of_two_tied_disputes_being_documented_is_enough(self):
        """A reason can genuinely contest two things; answering either is real."""
        matrix = _matrix(
            **{
                "NBH-CARD-014-3--3": "satisfied",
                "NBH-CARD-014-3--5": "insufficient_documentation",
            }
        )
        answerable, _ = has_answerable_dispute(matrix, ["NBH-CARD-014-3.3", "NBH-CARD-014-3.5"])
        assert answerable is True

    def test_an_unparseable_reason_falls_back_rather_than_declining(self):
        """Payers do send pure boilerplate. That is not grounds to give up."""
        matrix = _matrix(**{"NBH-ENDO-031-3--1": "satisfied"})
        answerable, why = has_answerable_dispute(matrix, [])
        assert answerable is True
        assert "could not be tied to a specific criterion" in why
        # "We could not tell" and "we checked" are different facts, and the
        # clerk is entitled to know which one they are looking at.
        assert "Read the denial letter" in why


class TestRecitationIsNotAHolding:
    """CASE-008 is the case that motivated weighting by rarity.

    That letter recites what the reviewer considered — the electrocardiogram,
    the echocardiogram report — before stating what it actually turned on, which
    was an unresolved MRI contraindication. Counting shared terms equally made
    the recitation tie with the holding, one of the ties was satisfied, and the
    case read as answerable. The system would have drafted a letter arguing
    points the reviewer explicitly conceded and never touched the one it denied
    on.
    """

    def test_the_holding_outranks_the_recitation(self):
        denial = _denial("CASE-008")
        assert primary_disputed_criteria(denial, _retrieval("CASE-008", denial)) == [
            "NBH-CARD-014-3.5"
        ]

    def test_the_conceded_criteria_are_not_treated_as_disputed(self):
        denial = _denial("CASE-008")
        primary = primary_disputed_criteria(denial, _retrieval("CASE-008", denial))
        assert "NBH-CARD-014-3.2" not in primary
        assert "NBH-CARD-014-5.3" not in primary


class TestClinicalAcronymsSurvive:
    """The length filter deleted every three-letter acronym in the domain.

    a1c, cgm, mri, ecg, osa, ahi, iop — the most discriminating words a denial
    reason contains — while keeping "2026".
    """

    def test_acronyms_are_kept(self):
        from agents.mapping.dispute import _terms

        assert {"a1c", "cgm", "mri", "ecg", "osa", "ahi", "iop"} <= _terms(
            "a1c cgm mri ecg osa ahi iop"
        )


class TestExclusionsAreNotDisputable:
    """A satisfied exclusion means the service is excluded.

    Asking whether one is "satisfied" inverts the sign of the whole test.
    """

    def test_an_exclusion_section_never_appears_as_disputed(self):
        denial = _denial("CASE-005")
        retrieval = _retrieval("CASE-005", denial)
        exclusion_ids = {
            criterion.criterion_id
            for section in retrieval.sections
            if "exclusion" in section.section_heading.lower()
            for criterion in section.criteria
        }
        assert exclusion_ids, "this test needs a policy that has exclusions"
        assert not exclusion_ids & set(primary_disputed_criteria(denial, retrieval))
