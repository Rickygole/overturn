"""Tests for the checks on a criteria matrix.

Mapping's output is what everything downstream trusts. Drafting only ever sees
satisfied criteria, and Verification checks the letter's claims against this
matrix. A fabricated quote that gets in here is laundered into an appeal that
looks perfectly well-supported all the way down, and the letter reaches a human
stamped "verified".

The attack that motivated most of this file requires no altered words at all.
"""

from __future__ import annotations

import pytest

from agents.mapping.validate import (
    chart_text_by_locator,
    normalise,
    quote_is_present,
    sanitise_matrix,
)
from agents.retrieval.corpus import load_corpus
from core.schemas.chart import Encounter, PatientChart
from core.schemas.criteria import ChartEvidence, CriteriaMatrix, CriterionVerdict
from core.schemas.enums import CriterionVerdictValue
from core.schemas.policy import RetrievalResult, RetrievedSection

NOTE = (
    "Patient reports he did not feel the usual warning symptoms before the "
    "episode. He confirms he no longer reliably senses hypoglycaemia and "
    "describes two further episodes. Field glucose 48 mg/dL."
)


@pytest.fixture
def chart() -> PatientChart:
    from datetime import date

    return PatientChart(
        patient_id="p1",
        name="Test Patient",
        date_of_birth=date(1966, 4, 2),
        sex="M",
        member_id="NBH-0001",
        encounters=[
            Encounter(
                encounter_id="e1",
                date=date(2026, 4, 6),
                encounter_type="Emergency department visit",
                note=NOTE,
                locator="enc/2026-04-06/emergency",
            )
        ],
    )


class TestTruncationAttack:
    """Verbatim and faithful are not the same property.

    The gap between them does not require changing a single word. It only
    requires stopping early.
    """

    def test_a_quote_that_steps_over_a_negation_is_rejected(self, chart):
        index = chart_text_by_locator(chart)
        assert (
            quote_is_present(
                "feel the usual warning symptoms before the episode",
                "enc/2026-04-06/emergency",
                index,
            )
            is False
        )

    def test_the_same_quote_keeping_its_negation_is_accepted(self, chart):
        index = chart_text_by_locator(chart)
        assert quote_is_present(
            "he did not feel the usual warning symptoms",
            "enc/2026-04-06/emergency",
            index,
        )

    def test_a_quote_after_no_longer_is_rejected(self, chart):
        index = chart_text_by_locator(chart)
        assert (
            quote_is_present("reliably senses hypoglycaemia", "enc/2026-04-06/emergency", index)
            is False
        )


class TestNoFalsePositives:
    """A jumpy check drops real evidence and downgrades real verdicts.

    That is the same harm the product exists to prevent — a winnable claim
    dying on a technicality — so these matter as much as the attacks above.
    """

    @pytest.mark.parametrize(
        "quote",
        [
            "describes two further episodes",  # next clause, after "and"
            "Field glucose 48 mg/dL",  # next sentence
            "He confirms he no longer reliably senses hypoglycaemia",  # carries its own
        ],
    )
    def test_a_faithful_quote_survives(self, chart, quote):
        index = chart_text_by_locator(chart)
        assert quote_is_present(quote, "enc/2026-04-06/emergency", index)

    def test_reflowed_whitespace_is_not_fabrication(self, chart):
        index = chart_text_by_locator(chart)
        assert quote_is_present(
            "describes   two\nfurther  episodes", "enc/2026-04-06/emergency", index
        )


class TestFabrication:
    def test_a_quote_that_is_not_in_the_record_is_rejected(self, chart):
        index = chart_text_by_locator(chart)
        assert (
            quote_is_present("the patient completed a marathon", "enc/2026-04-06/emergency", index)
            is False
        )

    def test_a_locator_that_does_not_exist_is_rejected(self, chart):
        assert (
            quote_is_present("anything", "enc/9999-99-99/nowhere", chart_text_by_locator(chart))
            is False
        )


class TestSanitisationIsOneWay:
    """Nothing here may strengthen a verdict, only weaken it."""

    def _matrix(self, verdict, locator, quote) -> CriteriaMatrix:
        return CriteriaMatrix(
            case_id="c1",
            verdicts=[
                CriterionVerdict(
                    criterion_id="NBH-ENDO-031-3.4",
                    criterion_text="t",
                    section_id="NBH-ENDO-031-3",
                    verdict=verdict,
                    evidence=[ChartEvidence(locator=locator, quote=quote)] if locator else [],
                    reasoning="r",
                    confidence=0.9,
                )
            ],
        )

    def _retrieval(self) -> RetrievalResult:
        sections = [
            RetrievedSection(**s.model_dump(), similarity=0.4, matched_query="q")
            for s in load_corpus()
            if s.policy_id == "NBH-ENDO-031"
        ]
        return RetrievalResult(query="q", sections=sections, top_similarity=0.4)

    def test_a_truncated_quote_downgrades_the_verdict(self, chart):
        raw = self._matrix(
            CriterionVerdictValue.SATISFIED,
            "enc/2026-04-06/emergency",
            "feel the usual warning symptoms before the episode",
        )
        clean, adjustments = sanitise_matrix(raw, self._retrieval(), chart)

        assert clean.verdicts[0].verdict is CriterionVerdictValue.INSUFFICIENT_DOCUMENTATION
        assert clean.satisfied_count == 0
        assert adjustments, "a silent correction is indistinguishable from no error"

    def test_a_faithful_quote_keeps_its_verdict(self, chart):
        raw = self._matrix(
            CriterionVerdictValue.SATISFIED,
            "enc/2026-04-06/emergency",
            "he did not feel the usual warning symptoms",
        )
        clean, adjustments = sanitise_matrix(raw, self._retrieval(), chart)

        assert clean.verdicts[0].verdict is CriterionVerdictValue.SATISFIED
        assert adjustments == []

    def test_an_invented_criterion_is_dropped_entirely(self, chart):
        raw = CriteriaMatrix(
            case_id="c1",
            verdicts=[
                CriterionVerdict(
                    criterion_id="NBH-ENDO-031-9.9",
                    criterion_text="t",
                    section_id="NBH-ENDO-031-9",
                    verdict=CriterionVerdictValue.SATISFIED,
                    evidence=[
                        ChartEvidence(
                            locator="enc/2026-04-06/emergency",
                            quote="describes two further episodes",
                        )
                    ],
                    reasoning="r",
                    confidence=0.9,
                )
            ],
        )
        clean, adjustments = sanitise_matrix(raw, self._retrieval(), chart)
        assert clean.verdicts == []
        assert any("not a criterion" in a for a in adjustments)


class TestNormalise:
    def test_unicode_variants_collapse(self):
        assert normalise("don’t “quote” me") == normalise('don\'t "quote" me')

    def test_case_and_whitespace_collapse(self):
        assert normalise("  The   PATIENT\nreports ") == "the patient reports"
