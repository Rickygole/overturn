"""Tests for the shape of the appeal letter.

A reviewer read the output of this system and said it was "a compliance
checklist with a salutation". They were right: there was no date, no addressee,
no member block, no ordering provider, no signature — nothing that makes a
document a letter a clinic would put on letterhead and post.

The fix is deliberately not "ask the model for a nicer letter". Every field on
the face of an appeal is a fact about the case record, and one of them is an
NPI. So the furniture is assembled in code, from the record, and where the
record is silent the letter carries a bracketed gap that a human fills. These
tests hold that line in both directions: the letter must have the parts, and it
must not have a part the record cannot support.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from agents.drafting.brief import (
    DraftingBrief,
    amount_in_dispute,
    build_brief,
    ordering_provider,
)
from agents.drafting.letter import GAP_TEXT, compose_letter, long_date, strip_furniture
from agents.intake.documents import SourceDocument
from agents.offline.handlers import build_offline_llm
from agents.orchestrator.deps import build_fleet
from agents.orchestrator.pipeline import Pipeline
from core.audit import read_case_trail
from core.schemas.case import CaseRecord
from core.schemas.chart import Encounter, PatientChart, Provenance
from core.schemas.criteria import ChartEvidence, CriteriaMatrix, CriterionVerdict
from core.schemas.denial import DenialExtraction, DeniedService
from core.schemas.enums import AgentName, CaseStatus, CriterionVerdictValue
from core.schemas.policy import PolicyCriterion, RetrievalResult, RetrievedSection
from core.store import MemoryStore

DENIALS = Path(__file__).resolve().parents[1] / "data" / "denials"

CRITERION_TEXT = (
    "The member has symptoms consistent with cardiac disease that have persisted "
    "for at least six weeks, or that are documented as progressive."
)


# --------------------------------------------------------------------------- #
# Fixtures — one case record, assembled by hand so each field can be removed
# --------------------------------------------------------------------------- #


def _denial(**overrides) -> DenialExtraction:
    fields = {
        "payer_name": "Northbeck Health Plan",
        "member_id": "NBH-4417-20551",
        "claim_number": "CLM-2026-0519-71144",
        "patient_name": "Creola518 Heller342",
        "patient_dob": date(1943, 6, 1),
        "services": [
            DeniedService(
                description="Magnetic resonance imaging, cardiac, with contrast material",
                procedure_code="75561",
                date_of_service=None,
            )
        ],
        "denial_reason_text": "Not established as medically necessary under NBH-CARD-014.",
        "denial_reason_code": "NBH-NMN-11",
        "date_of_denial": date(2026, 6, 2),
        "appeal_deadline": date(2026, 11, 29),
        "extraction_notes": "Estimated allowed amount was specified as $2,940.00.",
    }
    fields.update(overrides)
    return DenialExtraction(**fields)


def _matrix() -> CriteriaMatrix:
    return CriteriaMatrix(
        case_id="CASE-003",
        policy_ids=["NBH-CARD-014"],
        chart_summary="Progressive exertional dyspnoea; echocardiogram technically limited.",
        verdicts=[
            CriterionVerdict(
                criterion_id="NBH-CARD-014-3.1",
                criterion_text=CRITERION_TEXT,
                section_id="NBH-CARD-014-3",
                verdict=CriterionVerdictValue.SATISFIED,
                evidence=[
                    ChartEvidence(
                        locator="enc/2026-04-22/cardiology",
                        quote="Her symptoms have progressed further since February.",
                    )
                ],
                reasoning="The note documents progression.",
                confidence=0.94,
            )
        ],
    )


def _retrieval() -> RetrievalResult:
    return RetrievalResult(
        query="cardiac MRI",
        top_similarity=0.4,
        sections=[
            RetrievedSection(
                section_id="NBH-CARD-014-3",
                policy_id="NBH-CARD-014",
                policy_title="Advanced Cardiac Imaging",
                section_heading="Medical necessity criteria",
                text="All of the following must be documented in the member's medical record.",
                criteria=[PolicyCriterion(criterion_id="NBH-CARD-014-3.1", text=CRITERION_TEXT)],
                similarity=0.4,
                matched_query="cardiac MRI",
            )
        ],
    )


def _encounter(locator: str, when: str, clinician: str, specialty: str) -> Encounter:
    return Encounter(
        encounter_id=locator,
        date=date.fromisoformat(when),
        encounter_type="Office visit",
        clinician=clinician,
        specialty=specialty,
        note="Seen today.",
        locator=locator,
        provenance=Provenance.AUTHORED,
    )


def _chart() -> PatientChart:
    return PatientChart(
        patient_id="p1",
        name="Creola518 Heller342",
        date_of_birth=date(1943, 6, 1),
        sex="F",
        member_id="NBH-4417-20551",
        encounters=[
            _encounter("enc/2026-04-22/cardiology", "2026-04-22", "Dr. L. Bianchi", "Cardiology")
        ],
    )


def _case(**overrides) -> CaseRecord:
    case = CaseRecord(case_id="CASE-003", source_document_uri="gs://x/CASE-003.txt")
    case.denial = overrides.pop("denial", _denial())
    case.retrieval = _retrieval()
    case.criteria = overrides.pop("criteria", _matrix())
    for key, value in overrides.items():
        setattr(case, key, value)
    return case


def _brief(case: CaseRecord | None = None, **kwargs) -> DraftingBrief:
    return build_brief(
        case or _case(),
        chart=kwargs.pop("chart", _chart()),
        today=kwargs.pop("today", date(2026, 8, 29)),
        **kwargs,
    )


ARGUMENT = (
    "We are appealing the denial of cardiac magnetic resonance imaging.\n\n"
    "Section NBH-CARD-014-3.1 requires documented progressive symptoms. The "
    "record documents this.\n\n"
    "We ask that the denial be overturned and the claim processed for payment."
)


# --------------------------------------------------------------------------- #
# The parts of a letter
# --------------------------------------------------------------------------- #


class TestItLooksLikeALetter:
    """The reviewer's list, checked one item at a time."""

    @pytest.fixture
    def body(self) -> str:
        return compose_letter(_brief(), ARGUMENT).body

    def test_it_is_dated(self, body):
        assert "August 29, 2026" in body

    def test_it_is_addressed_to_the_payer_appeals_department(self, body):
        assert "Northbeck Health Plan" in body
        assert "Attn: Appeals Department" in body

    def test_it_names_the_member_and_the_identifiers_a_payer_routes_on(self, body):
        assert "Member: Creola518 Heller342" in body
        assert "Member ID: NBH-4417-20551" in body
        assert "Claim number: CLM-2026-0519-71144" in body

    def test_it_states_the_date_of_birth(self, body):
        assert "Date of birth: June 1, 1943" in body

    def test_it_states_the_service_and_its_code(self, body):
        assert "Service at issue: Magnetic resonance imaging" in body
        assert "CPT/HCPCS 75561" in body

    def test_the_procedure_code_sits_with_the_line_item_it_belongs_to(self):
        """On a two-item claim the code is the first item's, not the claim's."""
        denial = _denial(
            services=[
                DeniedService(description="CGM system", procedure_code="E2103"),
                DeniedService(description="CGM sensors", procedure_code="A4239"),
            ]
        )
        body = compose_letter(_brief(_case(denial=denial)), ARGUMENT).body
        assert "Service at issue: CGM system (CPT/HCPCS E2103), and 1 further line item" in body

    def test_it_carries_the_diagnosis_that_was_submitted(self):
        denial = _denial(
            services=[
                DeniedService(
                    description="Cardiac MRI", procedure_code="75561", diagnosis_code="R06.00"
                )
            ]
        )
        body = compose_letter(_brief(_case(denial=denial)), ARGUMENT).body
        assert "Diagnosis submitted: R06.00" in body

    def test_it_names_the_ordering_provider(self, body):
        assert "Ordering provider: Dr. L. Bianchi, Cardiology" in body

    def test_the_salutation_is_one_a_human_would_write(self, body):
        assert "Dear Appeals Coordinator:" in body
        assert "To Appeals Coordinator," not in body

    def test_it_says_what_it_is_asking_for(self, body):
        assert "overturned" in body
        assert "processed for payment" in body

    def test_it_has_a_signature_block_for_the_person_who_will_sign_it(self, body):
        tail = body[body.index("Sincerely,") :]
        assert "Dr. L. Bianchi" in tail
        assert "Ordering provider" in tail
        assert "Credential:" in tail
        assert "NPI:" in tail

    def test_the_argument_survives_intact(self, body):
        assert "Section NBH-CARD-014-3.1 requires documented progressive symptoms." in body

    def test_the_reference_block_precedes_the_salutation_and_the_argument(self, body):
        assert body.index("Claim number:") < body.index("Dear Appeals Coordinator:")
        assert body.index("Dear Appeals Coordinator:") < body.index("Section NBH-CARD-014-3.1")

    def test_it_is_plain_text_that_needs_no_styling(self, body):
        assert "\t" not in body
        assert "<" not in body
        assert not re.search(r"^\s*[*#|-]{1,3}\s", body, re.MULTILINE), "no markdown"

    def test_it_states_that_it_is_filed_in_time(self, body):
        assert "November 29, 2026" in body


class TestTheAppealLevelIsOnTheFace:
    def test_a_first_level_appeal_says_so(self):
        body = compose_letter(_brief(), ARGUMENT).body
        assert "Re: First-level appeal of an adverse benefit determination" in body

    def test_an_escalated_case_does_not_still_call_itself_a_first_level_appeal(self):
        from core.schemas.enums import AppealLevel

        case = _case(appeal_level=AppealLevel.SECOND_LEVEL)
        body = compose_letter(_brief(case), ARGUMENT).body
        assert "Re: Second-level appeal of an adverse benefit determination" in body


# --------------------------------------------------------------------------- #
# What is not on the record does not go on the letter
# --------------------------------------------------------------------------- #


class TestNothingIsInvented:
    def test_the_npi_is_a_gap_and_never_a_number(self):
        letter = compose_letter(_brief(), ARGUMENT)
        assert "ordering_provider_npi" in letter.gaps
        for line in letter.body.splitlines():
            if "NPI" in line:
                assert not re.search(r"\d{9,}", line), f"an NPI appeared from nowhere: {line}"

    def test_no_ten_digit_identifier_appears_anywhere_in_the_letter(self):
        """The corpus's denial notices carry a *facility* NPI. It is not the
        ordering provider's, it is not extracted, and it must not be borrowed."""
        body = compose_letter(_brief(), ARGUMENT).body
        assert not re.search(r"\b\d{10}\b", body)

    def test_a_missing_date_of_service_is_stated_as_missing(self):
        letter = compose_letter(_brief(), ARGUMENT)
        assert GAP_TEXT["date_of_service"] in letter.body
        assert "date_of_service" in letter.gaps

    def test_a_date_of_service_that_exists_is_printed(self):
        denial = _denial(
            services=[
                DeniedService(
                    description="Continuous glucose monitoring system",
                    procedure_code="E2103",
                    date_of_service=date(2026, 7, 6),
                )
            ]
        )
        letter = compose_letter(_brief(_case(denial=denial)), ARGUMENT)
        assert "Date of service: July 6, 2026" in letter.body
        assert "date_of_service" not in letter.gaps

    def test_the_sender_letterhead_and_the_payer_address_are_gaps(self):
        letter = compose_letter(_brief(), ARGUMENT)
        assert {"sender_letterhead", "payer_appeals_address"} <= set(letter.gaps)

    def test_every_gap_is_visible_in_the_letter_itself(self):
        """A gap recorded in a list a clerk never opens is not a gap, it is a
        blank the letter goes out with."""
        letter = compose_letter(_brief(), ARGUMENT)
        for text in letter.gap_text:
            assert text in letter.body

    def test_a_missing_chart_costs_the_provider_line_but_not_the_letter(self):
        letter = compose_letter(_brief(chart=None), ARGUMENT)
        assert GAP_TEXT["ordering_provider"] in letter.body
        assert "ordering_provider" in letter.gaps
        assert "Claim number: CLM-2026-0519-71144" in letter.body


# --------------------------------------------------------------------------- #
# The ordering provider
# --------------------------------------------------------------------------- #


class TestOrderingProvider:
    def test_it_is_the_clinician_the_cited_evidence_rests_on(self):
        name, specialty = ordering_provider(_case(), _chart())
        assert (name, specialty) == ("Dr. L. Bianchi", "Cardiology")

    def test_it_is_not_merely_the_most_recent_encounter(self):
        """CASE-006's last cited encounter is a case manager's. She did not
        order the care and must not sign the letter."""
        chart = _chart()
        chart.encounters = [
            _encounter("enc/2026-07-20/psychiatry", "2026-07-20", "Dr. M. Farah", "Psychiatry"),
            _encounter("enc/2026-07-21/psychiatry", "2026-07-21", "Dr. M. Farah", "Psychiatry"),
            _encounter(
                "enc/2026-07-24/care-coordination",
                "2026-07-24",
                "D. Whitfield, LICSW",
                "Behavioral Health Case Management",
            ),
        ]
        matrix = _matrix()
        matrix.verdicts[0].evidence = [
            ChartEvidence(locator="enc/2026-07-20/psychiatry", quote="Seen today."),
            ChartEvidence(locator="enc/2026-07-21/psychiatry", quote="Seen today."),
            ChartEvidence(locator="enc/2026-07-24/care-coordination", quote="Seen today."),
        ]
        name, _ = ordering_provider(_case(criteria=matrix), chart)
        assert name == "Dr. M. Farah"

    def test_a_tie_yields_a_gap_rather_than_a_coin_toss(self):
        chart = _chart()
        chart.encounters = [
            _encounter("enc/a", "2026-01-01", "Dr. A. One", "Cardiology"),
            _encounter("enc/b", "2026-02-01", "Dr. B. Two", "Cardiology"),
        ]
        matrix = _matrix()
        matrix.verdicts[0].evidence = [
            ChartEvidence(locator="enc/a", quote="Seen today."),
            ChartEvidence(locator="enc/b", quote="Seen today."),
        ]
        assert ordering_provider(_case(criteria=matrix), chart) == (None, None)

    def test_evidence_that_is_not_an_encounter_does_not_confuse_it(self):
        matrix = _matrix()
        matrix.verdicts[0].evidence = [
            ChartEvidence(locator="lab/a1c-2026-05-19", quote="8.6 percent"),
            ChartEvidence(locator="enc/2026-04-22/cardiology", quote="Seen today."),
        ]
        name, _ = ordering_provider(_case(criteria=matrix), _chart())
        assert name == "Dr. L. Bianchi"

    def test_the_derivation_agrees_with_every_denial_notice_in_the_corpus(self):
        """The notice names a requesting provider and Intake has no field for
        it, so the chart is the only source. This checks the derivation against
        the answer the notice gives, on every staged case that reaches drafting.
        """
        import json

        from agents.mapping.charts import load_chart

        answers = json.loads(
            (
                Path(__file__).resolve().parents[1] / "data" / "offline" / "mapping_answers.json"
            ).read_text()
        )
        checked = 0
        for case_id, rows in answers.items():
            chart = load_chart(case_id)
            matrix = CriteriaMatrix(
                case_id=case_id,
                verdicts=[
                    CriterionVerdict(
                        criterion_id=criterion_id,
                        criterion_text="x" * 20,
                        section_id="NBH-CARD-014-3",
                        verdict=CriterionVerdictValue(row["verdict"]),
                        evidence=[
                            ChartEvidence(locator=e["locator"], quote=e["quote"])
                            for e in row.get("evidence", [])
                        ],
                        reasoning="fixture",
                        confidence=0.9,
                    )
                    for criterion_id, row in rows.items()
                ],
            )
            name, _ = ordering_provider(_case(criteria=matrix), chart)
            stated = re.search(
                r"Requesting Provider:\s*(.+)", (DENIALS / f"{case_id}.txt").read_text()
            )
            if not stated or name is None:
                continue
            surname = stated.group(1).split(",")[0].split(". ")[-1].strip()
            assert surname in name, f"{case_id}: derived {name!r}, notice says {stated.group(1)!r}"
            checked += 1
        assert checked >= 5, "the corpus should exercise this on more than a couple of cases"


# --------------------------------------------------------------------------- #
# The money
# --------------------------------------------------------------------------- #


class TestAmountInDispute:
    def test_the_allowed_amount_in_the_intake_footnote_reaches_the_letter(self):
        letter = compose_letter(_brief(), ARGUMENT)
        assert "Amount in dispute: $2,940.00" in letter.body
        assert "estimated allowed amount" in letter.body

    def test_a_typed_billed_amount_is_preferred_to_the_footnote(self):
        denial = _denial(
            services=[
                DeniedService(
                    description="CGM system", procedure_code="E2103", billed_amount=1284.0
                )
            ],
            extraction_notes=None,
        )
        assert amount_in_dispute(_case(denial=denial)) == ("$1,284.00", "amount billed")

    def test_line_items_are_summed(self):
        denial = _denial(
            services=[
                DeniedService(description="CGM system", billed_amount=1000.0),
                DeniedService(description="Sensors", billed_amount=284.0),
            ],
            extraction_notes=None,
        )
        assert amount_in_dispute(_case(denial=denial))[0] == "$1,284.00"

    def test_the_label_may_follow_the_figure(self):
        denial = _denial(extraction_notes="The notice lists $2,940.00 as the allowed amount.")
        assert amount_in_dispute(_case(denial=denial))[0] == "$2,940.00"

    def test_two_disagreeing_figures_are_a_gap_not_a_choice(self):
        denial = _denial(
            extraction_notes=(
                "Total billed was specified as $3,100.00. Estimated allowed "
                "amount was specified as $2,940.00."
            )
        )
        assert amount_in_dispute(_case(denial=denial)) == (None, None)
        assert (
            GAP_TEXT["amount_in_dispute"]
            in compose_letter(_brief(_case(denial=denial)), ARGUMENT).body
        )

    def test_an_unlabelled_number_is_not_money(self):
        denial = _denial(extraction_notes="Reference 4417 appears twice on the notice.")
        assert amount_in_dispute(_case(denial=denial)) == (None, None)

    def test_no_note_at_all_is_a_gap(self):
        denial = _denial(extraction_notes=None)
        letter = compose_letter(_brief(_case(denial=denial)), ARGUMENT)
        assert "amount_in_dispute" in letter.gaps


# --------------------------------------------------------------------------- #
# Furniture the model writes anyway
# --------------------------------------------------------------------------- #


class TestStripFurniture:
    # The head of attempt 3 on CASE-003 as the deployed pipeline actually wrote
    # it, which is the shape this strip exists to remove.
    LIVE_HEAD = (
        "RE: Member ID NBH-4417-20551\n"
        "Claim Number: CLM-2026-0519-71144\n"
        "Service Denied: Magnetic resonance imaging, cardiac\n\n"
        "To Appeals Coordinator,\n\n"
        "We are appealing the denial of coverage for cardiac magnetic resonance imaging."
    )

    def test_a_model_written_reference_block_and_salutation_are_removed(self):
        stripped = strip_furniture(self.LIVE_HEAD)
        assert stripped.startswith("We are appealing")
        assert "RE:" not in stripped
        assert "To Appeals Coordinator," not in stripped

    def test_a_model_written_sign_off_is_removed(self):
        text = "The record documents this.\n\nSincerely,\n\nThe Appeals Team\n\nEnclosures: chart"
        assert strip_furniture(text) == "The record documents this."

    def test_a_salutation_with_no_blank_line_under_it_is_still_removed(self):
        text = "Dear Appeals Coordinator,\nWe are appealing the denial of cardiac MRI."
        assert strip_furniture(text) == "We are appealing the denial of cardiac MRI."

    def test_prose_that_merely_starts_with_a_furniture_word_is_kept(self):
        text = "To establish that the record documents progression, we cite the note of 22 April."
        assert strip_furniture(text) == text

    def test_a_body_made_entirely_of_furniture_is_left_alone(self):
        """Stripping to nothing would be a blank letter, which is worse than a
        doubled salutation and much harder to notice."""
        text = "Re: an appeal\n\nDear Sir,"
        assert strip_furniture(text) == text

    def test_the_composed_letter_has_exactly_one_salutation(self):
        body = compose_letter(_brief(), self.LIVE_HEAD).body
        assert body.count("Dear Appeals Coordinator:") == 1
        assert "To Appeals Coordinator," not in body
        assert body.count("Sincerely,") == 1


# --------------------------------------------------------------------------- #
# The letter and the verifier
# --------------------------------------------------------------------------- #


class TestTheHeaderIsNotAClaim:
    """Verification checks citations and clinical assertions. A letterhead is
    neither, and must not be smuggled into either list — an addressee block that
    has to be verified against a chart would fail, correctly, forever."""

    def test_composing_the_letter_adds_no_citations_or_assertions(self):
        brief = _brief()
        before = compose_letter(brief, ARGUMENT)
        assert "clinical_assertions" not in before.body
        assert isinstance(before.gaps, tuple)

    def test_the_pipeline_still_reaches_the_human_gate(self):
        case = _run("CASE-003")
        assert case.status is CaseStatus.AWAITING_APPROVAL

    def test_no_header_field_appears_as_a_clinical_assertion(self):
        case = _run("CASE-003")
        joined = " ".join(case.latest_draft.clinical_assertions)
        for fragment in ("NBH-4417-20551", "CLM-2026-0519-71144", "Attn:", "NPI"):
            assert fragment not in joined


# --------------------------------------------------------------------------- #
# End to end, on the scripted backend
# --------------------------------------------------------------------------- #


def _run(case_id: str, store: MemoryStore | None = None):
    pipeline = Pipeline(build_fleet(store=store or MemoryStore(), llm=build_offline_llm()))
    return pipeline.ingest(
        SourceDocument(
            uri=f"gs://overturn-intake/{case_id}.txt",
            data=(DENIALS / f"{case_id}.txt").read_bytes(),
            mime_type="text/plain",
        ),
        case_id=case_id,
    )


class TestOfflineAndLiveAgreeOnShape:
    """The scripted backend and Gemini both return an argument, and both get the
    same letter around it from the same function. Two bugs today came from the
    offline path drifting from the deployed one; this is the guard."""

    def test_the_scripted_backend_writes_no_letter_furniture_of_its_own(self):
        from agents.offline.handlers import drafting_compose
        from core.llm import LlmRequest

        draft = drafting_compose(
            LlmRequest(
                agent="drafting",
                operation="compose",
                system="",
                prompt=(
                    "PAYER: Northbeck Health Plan\nCLAIM NUMBER: CLM-1\n"
                    "SERVICE DENIED: cardiac MRI\n\n"
                    "CRITERION NBH-CARD-014-3.1  (in section NBH-CARD-014-3)\n"
                    f"  Criterion text: {CRITERION_TEXT}\n"
                    "  Why it is met:  The note documents progression.\n"
                    "  Documented in the chart by:\n"
                    '    - [enc/2026-04-22/cardiology] "Symptoms have progressed."\n'
                ),
                model="offline",
            )
        )
        assert "Dear" not in draft.body
        assert not draft.body.startswith("Re:")
        assert "Sincerely" not in draft.body

    def test_the_offline_run_produces_a_letter_with_every_part(self):
        body = _run("CASE-003").latest_draft.body
        for part in (
            "Northbeck Health Plan",
            "Attn: Appeals Department",
            "Member: Creola518 Heller342",
            "Member ID: NBH-4417-20551",
            "Claim number: CLM-2026-0519-71144",
            "Ordering provider: Dr. L. Bianchi, Cardiology",
            "Dear Appeals Coordinator:",
            "Sincerely,",
        ):
            assert part in body, f"missing from the offline letter: {part}"

    def test_the_offline_run_carries_the_money_at_stake(self):
        """$2,940 was in an intake footnote and nowhere a payer would look."""
        assert "Amount in dispute: $2,940.00" in _run("CASE-003").latest_draft.body

    def test_the_gaps_are_named_in_the_audit_trail(self):
        """Not counted — named. A clerk should be able to see what the system
        declined to fill in without opening the letter."""
        store = MemoryStore()
        case = _run("CASE-003", store)
        drafting = [
            event
            for event in read_case_trail(store, case.case_id)
            if event.agent is AgentName.DRAFTING
        ]
        assert drafting, "drafting recorded nothing"
        assert "ordering_provider_npi" in drafting[-1].decision
        assert "sender_letterhead" in drafting[-1].decision


class TestTheLetterDateIsTheDateItWasWritten:
    def test_the_brief_dates_the_letter_today_by_default(self):
        brief = build_brief(_case(), chart=_chart())
        from core.schemas.base import utcnow

        assert brief.letter_date == utcnow().date()

    def test_long_date_reads_the_way_the_notice_it_answers_reads(self):
        assert long_date(date(2026, 6, 2)) == "June 2, 2026"
