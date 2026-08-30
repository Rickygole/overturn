"""Tests for the checks on a criteria matrix.

Mapping's output is what everything downstream trusts. Drafting only ever sees
satisfied criteria, and Verification checks the letter's claims against this
matrix. A fabricated quote that gets in here is laundered into an appeal that
looks perfectly well-supported all the way down, and the letter reaches a human
stamped "verified".

The attack that motivated most of this file requires no altered words at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.mapping.scope import not_applicable_verdicts, out_of_scope_sections
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


# --------------------------------------------------------------------------- #
# Scoping criteria to the service that was actually denied
# --------------------------------------------------------------------------- #
#
# Everything above guards against a verdict that claims too much. This guards
# against a verdict on a question nobody asked.
#
# NBH-CARD-014 covers two modalities in two separate sections. CASE-003 is a
# cardiac MRI denial, and Mapping ruled on the coronary CT criteria as well,
# returning three verdicts that were noise — and one, NBH-CARD-014-4.2, that
# was worse than noise. That criterion asks whether the member has undergone
# coronary revascularisation. The patient had a CABG in 2014. The matrix
# therefore carried an argument for the payer, about a study nobody requested,
# inside our own analysis.


def _denial(case_id: str):
    from agents.offline.handlers import intake_extract
    from core.llm import LlmRequest

    path = Path(__file__).resolve().parents[1] / "data" / "denials" / f"{case_id}.txt"
    return intake_extract(
        LlmRequest(
            agent="intake",
            operation="extract",
            system="",
            prompt=path.read_text(),
            model="offline",
        )
    )


def _retrieval_for(policy_id: str) -> RetrievalResult:
    sections = [
        RetrievedSection(**s.model_dump(), similarity=0.9, matched_query="q")
        for s in load_corpus()
        if s.policy_id == policy_id
    ]
    return RetrievalResult(query="q", sections=sections, top_similarity=0.9)


class TestWhichSectionsGovernTheDeniedService:
    def test_the_other_modalitys_criteria_are_out_of_scope(self):
        out_of_scope = out_of_scope_sections(_denial("CASE-003"), _retrieval_for("NBH-CARD-014"))
        assert list(out_of_scope) == ["NBH-CARD-014-4"]

    def test_the_reason_is_one_a_reader_can_check(self):
        """It has to be readable on the screen, not just correct.

        A determination a clerk cannot audit against the policy's own table of
        contents is the post-hoc correction layer this was written to avoid
        being.
        """
        reason = out_of_scope_sections(_denial("CASE-003"), _retrieval_for("NBH-CARD-014"))[
            "NBH-CARD-014-4"
        ]
        assert "Coronary CT Angiography" in reason
        assert "Cardiac Magnetic Resonance Imaging" in reason
        assert "NBH-CARD-014-3" in reason
        assert "Magnetic resonance imaging, cardiac" in reason

    def test_the_governing_section_is_never_scoped_out(self):
        out_of_scope = out_of_scope_sections(_denial("CASE-003"), _retrieval_for("NBH-CARD-014"))
        assert "NBH-CARD-014-3" not in out_of_scope

    def test_documentation_and_exclusion_sections_are_never_scoped_out(self):
        """They name no service and are written to serve the whole policy.

        NBH-CARD-014-5.2 and 5.3 exist only to support section 3.2. Scoping
        them out because their heading does not say "MRI" would delete three
        satisfied criteria from a case that depends on them.
        """
        out_of_scope = out_of_scope_sections(_denial("CASE-003"), _retrieval_for("NBH-CARD-014"))
        assert "NBH-CARD-014-5" not in out_of_scope
        assert "NBH-CARD-014-6" not in out_of_scope


class TestWhenItRefusesToDecide:
    """Firing wrongly deletes criteria that applied, so it fires narrowly."""

    def test_two_presentations_of_one_service_are_not_two_services(self):
        """NBH-MSK-022 splits on urgency, not on modality.

        "Urgent Presentation" and "Non-Urgent Presentation" are the same lumbar
        MRI under different circumstances, and which one applies is a clinical
        question this module has no business answering. The words the two
        headings share are struck before scoring, which leaves nothing for the
        denied service to match, which is the refusal.
        """
        assert out_of_scope_sections(_denial("CASE-002"), _retrieval_for("NBH-MSK-022")) == {}

    @pytest.mark.parametrize(
        "case_id,policy_id",
        [
            ("CASE-001", "NBH-ENDO-031"),
            ("CASE-005", "NBH-PULM-008"),
            ("CASE-006", "NBH-BEHV-045"),
        ],
    )
    def test_a_policy_with_one_coverage_section_is_untouched(self, case_id, policy_id):
        assert out_of_scope_sections(_denial(case_id), _retrieval_for(policy_id)) == {}

    def test_no_denial_means_no_scoping(self):
        assert out_of_scope_sections(None, _retrieval_for("NBH-CARD-014")) == {}

    def test_a_letter_that_names_no_service_is_not_guessed_at(self):
        denial = _denial("CASE-003").model_copy(update={"services": []})
        assert out_of_scope_sections(denial, _retrieval_for("NBH-CARD-014")) == {}

    def test_a_criterion_the_payer_argued_about_stays_in_scope(self):
        """The determination outranks the heading.

        If the payer's own letter turns on a criterion, it applies to this
        request whatever section it was filed under. A denial arguing about
        graft patency and coronary stents is arguing about NBH-CARD-014-4.2,
        and scoping that row away would delete the thing under dispute.
        """
        denial = _denial("CASE-003").model_copy(
            update={
                "denial_reason_text": (
                    "The member has undergone coronary revascularisation and a coronary "
                    "stent is in place, and the request is not for the evaluation of "
                    "graft patency."
                )
            }
        )
        assert out_of_scope_sections(denial, _retrieval_for("NBH-CARD-014")) == {}


class TestNotApplicableRowsAreRealRows:
    """`not_applicable` is a verdict, not an absence.

    A criterion left out of the matrix reads downstream as one Mapping could
    not reach, and `has_appealable_basis` counts those against the case under
    the worst-case rule added on 29 August. Relabelling noise as silence would
    decline a case that qualifies.
    """

    def _section(self):
        return next(
            s for s in _retrieval_for("NBH-CARD-014").sections if s.section_id == "NBH-CARD-014-4"
        )

    def test_one_row_per_criterion_carrying_the_verbatim_text(self):
        section = self._section()
        rows = not_applicable_verdicts("CASE-003", section, "because")
        assert [r.criterion_id for r in rows] == [c.criterion_id for c in section.criteria]
        assert [r.criterion_text for r in rows] == [c.text for c in section.criteria]
        assert all(r.verdict is CriterionVerdictValue.NOT_APPLICABLE for r in rows)
        assert all(r.evidence == [] for r in rows)

    def test_they_do_not_count_as_unevaluated(self):
        matrix = CriteriaMatrix(
            case_id="CASE-003",
            verdicts=[
                CriterionVerdict(
                    criterion_id="NBH-CARD-014-3.1",
                    criterion_text="t",
                    section_id="NBH-CARD-014-3",
                    verdict=CriterionVerdictValue.SATISFIED,
                    evidence=[ChartEvidence(locator="l", quote="a quote long enough to count")],
                    reasoning="r",
                    confidence=0.9,
                ),
                *not_applicable_verdicts("CASE-003", self._section(), "because"),
            ],
        )
        assert matrix.unevaluated_count == 0
        assert matrix.has_appealable_basis is True

    def test_the_same_rows_as_failures_would_sink_the_case(self):
        """What the old behaviour cost, stated as an assertion.

        Three verdicts on the wrong modality, returned as `not_satisfied`,
        outnumber the satisfied row and the case reports no appealable basis.
        """
        failures = [
            row.model_copy(update={"verdict": CriterionVerdictValue.NOT_SATISFIED})
            for row in not_applicable_verdicts("CASE-003", self._section(), "because")
        ]
        matrix = CriteriaMatrix(
            case_id="CASE-003",
            verdicts=[
                CriterionVerdict(
                    criterion_id="NBH-CARD-014-3.1",
                    criterion_text="t",
                    section_id="NBH-CARD-014-3",
                    verdict=CriterionVerdictValue.SATISFIED,
                    evidence=[ChartEvidence(locator="l", quote="a quote long enough to count")],
                    reasoning="r",
                    confidence=0.9,
                ),
                *failures,
            ],
        )
        assert matrix.has_appealable_basis is False


class TestTheMappingAgentOnCaseThree:
    """The agent end to end, offline, on the case that was wrong."""

    def _run(self):
        from agents.base import build_deps
        from agents.mapping.agent import MappingAgent, MappingRequest
        from agents.mapping.charts import load_chart
        from agents.offline.handlers import install
        from core.llm import LlmClient, ScriptedBackend
        from core.schemas.enums import AgentName
        from core.store import MemoryStore

        backend = install(ScriptedBackend())
        agent = MappingAgent(build_deps(MemoryStore(), AgentName.MAPPING, LlmClient(backend)))
        matrix = agent.run(
            "CASE-003",
            MappingRequest(
                chart=load_chart("CASE-003"),
                retrieval=_retrieval_for("NBH-CARD-014"),
                denial=_denial("CASE-003"),
            ),
        )
        return matrix, backend

    def test_the_coronary_ct_criteria_come_back_not_applicable(self):
        matrix, _ = self._run()
        verdicts = {v.criterion_id: v.verdict for v in matrix.verdicts}
        assert verdicts["NBH-CARD-014-4.1"] is CriterionVerdictValue.NOT_APPLICABLE
        assert verdicts["NBH-CARD-014-4.2"] is CriterionVerdictValue.NOT_APPLICABLE
        assert verdicts["NBH-CARD-014-4.3"] is CriterionVerdictValue.NOT_APPLICABLE

    def test_the_revascularisation_row_is_answered_rather_than_omitted(self):
        """4.2 is the liability row, and hiding it is not the fix.

        It stays in the matrix, on the screen, with a reason saying it governs
        coronary CT angiography. What it must not do is record a finding
        against the patient on a study nobody requested.
        """
        matrix, _ = self._run()
        row = next(v for v in matrix.verdicts if v.criterion_id == "NBH-CARD-014-4.2")
        assert row.verdict is CriterionVerdictValue.NOT_APPLICABLE
        assert "Coronary CT Angiography" in row.reasoning
        assert matrix.unmapped_criteria == []

    def test_the_model_is_never_asked_about_the_other_modality(self):
        _, backend = self._run()
        asked = [c.prompt for c in backend.calls if c.operation == "map_section"]
        assert asked, "the sections that do apply are still mapped by the model"
        assert not any("SECTION NBH-CARD-014-4 " in p for p in asked)

    def test_the_model_is_told_what_was_denied(self):
        _, backend = self._run()
        asked = [c.prompt for c in backend.calls if c.operation == "map_section"]
        assert all(p.startswith("SERVICE DENIED") for p in asked)
        assert all("Magnetic resonance imaging, cardiac" in p for p in asked)

    def test_the_case_still_has_a_basis_to_appeal_on(self):
        matrix, _ = self._run()
        assert matrix.has_appealable_basis is True


class TestTheAuditLineExplainsItself:
    """ "N verdict(s) adjusted in validation" said nothing a reviewer could use.

    It reads as an unexplained correction layer applied after the model, which
    is exactly what it must not be allowed to look like when the adjustments
    themselves each already say what changed and why.
    """

    def _matrix(self) -> CriteriaMatrix:
        return CriteriaMatrix(case_id="CASE-003", verdicts=[])

    def test_the_adjustments_are_named_not_counted(self):
        from agents.mapping.agent import MappingAgent

        line = MappingAgent._describe(
            self._matrix(),
            ["NBH-CARD-014-3.2: dropped a quote not present at 'enc/2026-03-09/echo'"],
            {},
        )
        assert "adjusted in validation" not in line
        assert "dropped a quote not present at 'enc/2026-03-09/echo'" in line

    def test_scoping_is_reported_separately_from_correction(self):
        """Not asking a question is not the same as correcting an answer."""
        from agents.mapping.agent import MappingAgent

        line = MappingAgent._describe(self._matrix(), [], {"NBH-CARD-014-4": "because"})
        assert "NBH-CARD-014-4 not put to the model" in line
        assert "validation changed" not in line
