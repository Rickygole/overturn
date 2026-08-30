"""What the verifier can and cannot be fooled by.

A red team got four different false claims past this and all four shared one
shape: keep the vocabulary, change the meaning. Word overlap is blind to
exactly that, because a sentence and its opposite share almost every word.

Each test below is one of those attacks.
"""

from __future__ import annotations

import pytest

from agents.base import build_deps
from agents.offline.handlers import _content_words, _overlap, install
from agents.retrieval.corpus import load_corpus
from agents.verification.agent import VerificationAgent, VerificationRequest
from agents.verification.checks import is_faithful_restatement
from core.llm import LlmClient, ScriptedBackend
from core.schemas.criteria import ChartEvidence, CriteriaMatrix, CriterionVerdict
from core.schemas.draft import AppealDraft, Citation
from core.schemas.enums import AgentName, CriterionVerdictValue
from core.schemas.policy import RetrievalResult, RetrievedSection
from core.store import MemoryStore

CRITERION = (
    "At least one of the following is documented within the twelve months "
    "preceding the request: a hemoglobin A1c result of 7.0 percent or greater; "
    "or one or more episodes of level 2 hypoglycaemia, defined as a blood "
    "glucose measurement below 54 mg/dL."
)


class TestFaithfulClaimsPass:
    @pytest.mark.parametrize(
        "claim",
        [
            "a hemoglobin A1c result of 7.0 percent or greater",
            "a blood glucose measurement below 54 mg/dL",
            "at least one of the following is documented within the twelve months",
        ],
    )
    def test_a_restatement_is_supported(self, claim):
        assert _overlap(claim, CRITERION) >= 0.55


class TestMeaningPreservingAttacks:
    """Same words, different meaning. All of these once scored near 1.0."""

    def test_a_negated_requirement_is_not_supported(self):
        assert _overlap("a hemoglobin A1c result is not required", CRITERION) == 0.0

    def test_an_exclusion_cited_as_coverage_is_not_supported(self):
        assert _overlap("this section provides that coverage is not available", CRITERION) == 0.0

    def test_an_inverted_threshold_is_not_supported(self):
        """'or less' instead of 'or greater' turns a floor into a ceiling."""
        assert _overlap("a hemoglobin A1c result of 7.0 percent or less", CRITERION) == 0.0

    def test_an_inverted_bound_is_not_supported(self):
        assert _overlap("a blood glucose measurement above 54 mg/dL", CRITERION) == 0.0

    def test_a_falsified_number_is_not_supported(self):
        assert _overlap("a hemoglobin A1c result of 14.2 percent or greater", CRITERION) == 0.0


class TestNumbersAreVisible:
    """The length filter used to delete every number under three characters."""

    def test_short_numbers_survive_tokenisation(self):
        words = _content_words("A1c of 7.0 percent, glucose 54 mg/dL, level 2")
        assert "7.0" in words
        assert "54" in words
        assert "2" in words

    def test_a_decimal_stays_one_token(self):
        assert "7.0" in _content_words("a result of 7.0 percent")
        assert "7" not in _content_words("a result of 7.0 percent")


class TestNegationIsDirectional:
    """A claim may not introduce a negation; it may sit inside a source that has one.

    Comparing the two negation sets for equality looked tighter and was much
    worse. The source in an assertion check is the whole evidence corpus, so one
    'not' in an unrelated quote made every plainly-supported claim mismatch and
    the verifier rejected an entire honest draft.
    """

    def test_a_claim_without_negation_survives_a_source_that_has_one(self):
        source = (
            "The echocardiogram was technically limited. The patient did not "
            "report chest pain. Ejection fraction was 45 to 50 percent."
        )
        claim = "Ejection fraction was 45 to 50 percent"
        assert _overlap(claim, source) >= 0.55

    def test_a_claim_that_adds_a_negation_does_not(self):
        source = "Ejection fraction was 45 to 50 percent."
        assert _overlap("Ejection fraction was not measured", source) == 0.0


CORPUS_WITH_A_NEGATION = """[enc/2026-03-09/echocardiogram] The question of an infiltrative process raised clinically is not answered by this study and further imaging is advised.
[enc/2026-03-02/ecg] Interpretation: paced rhythm, non-diagnostic for ischaemia.
[enc/2026-02-18/internal-medicine] Eighty-three year old woman reporting six weeks of increasing breathlessness on exertion."""

CORPUS_WITH_NUMBERS = """[enc/2026-04-06/emergency] Field glucose 48 mg/dL.
[lab/a1c-2026-05-19] 2026-05-19 Hemoglobin A1c 8.6 % 4.0-5.6
[med/insulin-glargine] Insulin glargine 38 units subcutaneous once nightly
[med/metformin] Metformin 1000 mg oral twice daily"""


def _assertion_check(corpus: str, assertions: list[str]):
    from agents.offline.handlers import verification_verify_assertions
    from core.llm import LlmRequest

    block = "\n".join(f"- {a}" for a in assertions)
    prompt = (
        f"CHART EVIDENCE, complete:\n\n{corpus}\n\n{'=' * 60}\n"
        f"ASSERTIONS THE LETTER MAKES:\n{block}\n\nWhich are not supported?"
    )
    return verification_verify_assertions(
        LlmRequest(
            agent="verification",
            operation="verify_assertions",
            system="",
            prompt=prompt,
            model="offline",
        )
    )


class TestEvidenceIsCheckedPerQuote:
    """The corpus union made both guards useless.

    `_overlap` refuses a claim that introduces a negation its source lacks, or a
    number its source does not contain. With every quote joined into one blob,
    the source contains every negation and every number in the whole case — so
    one "not" in an unrelated note unlocked every claim in the letter, and a
    number lifted from a locator date licensed any dose.
    """

    def test_an_honest_claim_still_passes(self):
        honest = (
            "The record states the question of an infiltrative process is not "
            "answered by this study"
        )
        assert _assertion_check(CORPUS_WITH_A_NEGATION, [honest]).ungrounded_assertions == []

    @pytest.mark.parametrize(
        "assertion",
        [
            "The record states the study did answer the question of an infiltrative "
            "process and no further imaging is needed",
            "The record states paced rhythm, diagnostic for ischaemia, and the study "
            "is not non-diagnostic",
            "The record states the patient does not have breathlessness and is not "
            "short of breath on exertion",
        ],
    )
    def test_one_unrelated_negation_does_not_unlock_the_letter(self, assertion):
        result = _assertion_check(CORPUS_WITH_A_NEGATION, [assertion])
        assert result.ungrounded_assertions == [assertion]

    @pytest.mark.parametrize(
        "assertion",
        [
            # 38 is real — it is the glargine dose. Severe hypoglycaemia becomes normal.
            "The record at enc/2026-04-06/emergency states: Field glucose 38 mg/dL",
            # 11 is real — it came from a locator date.
            "The record at lab/a1c-2026-05-19 states: Hemoglobin A1c 11 %",
            # 1000 is real — it is the metformin dose. A ten-fold insulin overdose.
            "The record at med/insulin-glargine states: Insulin glargine 1000 units "
            "subcutaneous once nightly",
        ],
    )
    def test_a_number_from_elsewhere_in_the_case_does_not_license_a_claim(self, assertion):
        result = _assertion_check(CORPUS_WITH_NUMBERS, [assertion])
        assert result.ungrounded_assertions == [assertion]

    def test_the_true_number_passes(self):
        honest = "The record at enc/2026-04-06/emergency states: Field glucose 48 mg/dL"
        assert _assertion_check(CORPUS_WITH_NUMBERS, [honest]).ungrounded_assertions == []


class TestHonestParaphrasesSurvive:
    """The guards must not reject a faithful restatement.

    A fatal finding on ordinary wording sends a qualifying case to a human
    three attempts running, which is the same harm as missing a fabrication —
    arriving from the other side.
    """

    def test_at_least_is_not_an_inverted_threshold(self):
        claim = (
            "a hemoglobin A1c result of at least 7.0 percent documented within the "
            "twelve months preceding the request"
        )
        assert _overlap(claim, CRITERION) >= 0.55

    def test_a1c_is_not_split_into_a_phantom_number(self):
        assert "a1c" in _content_words("hemoglobin A1c result of 7.0 percent")
        assert "1" not in _content_words("hemoglobin A1c result")

    def test_a_section_reference_is_not_read_as_a_threshold(self):
        from agents.offline.handlers import _thresholds

        assert _thresholds("documented under section 3.2 of the policy") == frozenset()


# --------------------------------------------------------------------------- #
# Restatements, and the cost of rejecting one
# --------------------------------------------------------------------------- #
#
# Everything above is about a verifier that is too easily satisfied. This is the
# same failure arriving from the other side, and it is the one that actually
# happened: on CASE-003 the live model rejected a word-for-word restatement of
# NBH-CARD-014-3.5 by objecting to a requirement the letter never asserted, and
# again on 3.2 over where a modifier attaches. Three attempts, an attempt cap
# that fails closed, and a well-founded appeal on a human's desk.
#
# `is_faithful_restatement` settles that class of claim without a model. It can
# only pass a citation, never fail one, so nothing below can make the verifier
# more permissive about anything it does not recognise.


def _criterion(criterion_id: str) -> str:
    """Verbatim criterion text, read from the corpus rather than retyped.

    Retyping it here would make these tests agree with themselves rather than
    with `data/policies`, which is the document the claim is checked against.
    """
    for section in load_corpus():
        for criterion in section.criteria:
            if criterion.criterion_id == criterion_id:
                return criterion.text
    raise AssertionError(f"{criterion_id} is not in the corpus")


class TestTheCaseThreeRestatements:
    """The two claims that were rejected, and were correct."""

    def test_a_verbatim_restatement_prefixed_with_requires_that_passes(self):
        source = _criterion("NBH-CARD-014-3.5")
        claim = (
            "Requires that there is no contraindication to magnetic resonance "
            "imaging, or, where a relative contraindication exists, the medical "
            "record documents that it has been addressed."
        )
        assert is_faithful_restatement(claim, source)

    def test_the_modifier_scope_claim_on_three_two_passes(self):
        source = _criterion("NBH-CARD-014-3.2")
        assert is_faithful_restatement(f"Requires that {source}", source)

    @pytest.mark.parametrize(
        "prefix",
        [
            "Requires that ",
            "requires that ",
            "This section provides that ",
            "The policy states that ",
            "Provides that ",
            "The criterion requires ",
        ],
    )
    def test_framing_a_criterion_does_not_change_it(self, prefix):
        source = _criterion("NBH-CARD-014-3.5")
        assert is_faithful_restatement(prefix + source, source)

    def test_reflowed_whitespace_is_not_a_different_claim(self):
        source = _criterion("NBH-CARD-014-3.5")
        assert is_faithful_restatement("  ".join(source.split()), source)

    def test_the_letter_may_quote_the_source_outright(self):
        source = _criterion("NBH-CARD-014-3.1")
        assert is_faithful_restatement(source, source)


class TestWhatIsNotARestatement:
    """The guard is narrow on purpose.

    A claim it does not recognise is not condemned — it goes to the model
    exactly as before. So these assert that the fast path stays out of the way,
    not that the claim is false.
    """

    def test_a_truncation_that_steps_over_a_negation_is_not_recognised(self):
        """Verbatim, and says something the source does not.

        "where a relative contraindication exists, the medical record documents
        that it has been addressed" is word-for-word out of 3.5 and drops the
        "There is no contraindication ... or" that makes it one branch of a
        disjunction rather than a standing documentation requirement.
        """
        source = _criterion("NBH-CARD-014-3.5")
        claim = (
            "Requires that where a relative contraindication exists, the medical "
            "record documents that it has been addressed."
        )
        assert is_faithful_restatement(claim, source) is False

    def test_a_paraphrase_is_left_to_the_model(self):
        source = _criterion("NBH-CARD-014-3.5")
        claim = "Requires that any contraindication to MRI be documented as addressed."
        assert is_faithful_restatement(claim, source) is False

    def test_a_fabricated_requirement_is_not_recognised(self):
        source = _criterion("NBH-CARD-014-3.5")
        claim = (
            "Requires that the ordering clinician's request is itself sufficient "
            "to deem any relative contraindication addressed."
        )
        assert is_faithful_restatement(claim, source) is False

    def test_a_fragment_too_short_to_mean_anything_is_not_recognised(self):
        source = _criterion("NBH-CARD-014-3.2")
        assert is_faithful_restatement("Requires an electrocardiogram", source) is False

    def test_an_inverted_threshold_is_not_recognised(self):
        source = _criterion("NBH-ENDO-031-3.4")
        claim = "a hemoglobin A1c result of 7.0 percent or less"
        assert is_faithful_restatement(claim, source) is False


class TestTheVerifierAsAWhole:
    """The fast path in place, against the checks it must not weaken."""

    @pytest.fixture
    def verifier(self):
        """The real agent over the real corpus, with a backend that counts calls."""
        backend = install(ScriptedBackend())
        sections = [
            RetrievedSection(**section.model_dump(), similarity=0.9, matched_query="q")
            for section in load_corpus()
            if section.policy_id == "NBH-CARD-014"
        ]
        retrieval = RetrievalResult(query="q", sections=sections, top_similarity=0.9)
        matrix = CriteriaMatrix(
            case_id="CASE-003",
            verdicts=[
                CriterionVerdict(
                    criterion_id="NBH-CARD-014-3.1",
                    criterion_text=_criterion("NBH-CARD-014-3.1"),
                    section_id="NBH-CARD-014-3",
                    verdict=CriterionVerdictValue.SATISFIED,
                    evidence=[
                        ChartEvidence(
                            locator="enc/2026-04-21/cardiology",
                            quote="progressive exertional dyspnoea since February",
                        )
                    ],
                    reasoning="documented",
                    confidence=0.9,
                )
            ],
        )
        agent = VerificationAgent(
            build_deps(MemoryStore(), AgentName.VERIFICATION, LlmClient(backend))
        )
        return agent, backend, retrieval, matrix

    @staticmethod
    def _draft(citation: Citation) -> AppealDraft:
        return AppealDraft(
            case_id="CASE-003",
            attempt=1,
            subject_line="Appeal of adverse benefit determination",
            body="body",
            citations=[citation],
            clinical_assertions=[
                "The record states progressive exertional dyspnoea since February"
            ],
        )

    def test_a_verbatim_restatement_passes_without_a_model_call(self, verifier):
        agent, backend, retrieval, matrix = verifier
        draft = self._draft(
            Citation(
                section_id="NBH-CARD-014-3.1",
                claim=f"Requires that {_criterion('NBH-CARD-014-3.1')}",
                supporting_criterion_ids=["NBH-CARD-014-3.1"],
            )
        )
        result = agent.run(
            "CASE-003",
            VerificationRequest(draft=draft, retrieval=retrieval, matrix=matrix),
        )
        assert result.passed
        assert [call.operation for call in backend.calls] == ["verify_assertions"], (
            "the claim is the criterion's own words; there is nothing to ask a model"
        )

    def test_a_fabricated_section_still_fails(self, verifier):
        agent, _backend, retrieval, matrix = verifier
        draft = self._draft(
            Citation(
                section_id="NBH-CARD-014-9.9",
                claim="This section provides that the study is deemed covered.",
            )
        )
        result = agent.run(
            "CASE-003",
            VerificationRequest(draft=draft, retrieval=retrieval, matrix=matrix),
        )
        assert result.passed is False
        assert result.citations_nonexistent == ["NBH-CARD-014-9.9"]

    def test_a_citation_resting_on_an_unsatisfied_criterion_still_fails(self, verifier):
        """3.5 restated perfectly, resting on a criterion the chart does not meet.

        The restatement is accurate and the argument is still worthless, and
        those are two different checks. Nothing about the first may excuse the
        second.
        """
        agent, _backend, retrieval, matrix = verifier
        draft = self._draft(
            Citation(
                section_id="NBH-CARD-014-3.5",
                claim=f"Requires that {_criterion('NBH-CARD-014-3.5')}",
                supporting_criterion_ids=["NBH-CARD-014-3.5"],
            )
        )
        result = agent.run(
            "CASE-003",
            VerificationRequest(draft=draft, retrieval=retrieval, matrix=matrix),
        )
        assert result.passed is False


class TestTheCaseOneCatchSurvives:
    """The regression that matters most.

    Attempt 1 of CASE-001 called the 14 July 2026 "interim review" a telehealth
    evaluation. The chart never says how that encounter was conducted. Nothing
    about restatements may make that claim passable — and it cannot, because it
    is an assertion about a patient, checked against the chart, and the
    restatement path only ever looks at policy text.
    """

    CORPUS = (
        "[enc/2026-07-14/endocrinology] Diabetes management discussed in full: "
        "insulin regimen unchanged, fingerstick frequency unchanged at two to "
        "three per day."
    )

    def test_the_telehealth_overclaim_is_still_ungrounded(self):
        assertion = (
            "The 14 July 2026 encounter was a telehealth evaluation at which "
            "diabetes management was addressed"
        )
        assert _assertion_check(self.CORPUS, [assertion]).ungrounded_assertions == [assertion]

    def test_restating_the_criterion_that_mentions_telehealth_is_still_fine(self):
        """The overclaim was about the patient, not about the policy.

        NBH-ENDO-031-3.3 says "in-person or telehealth evaluation" in terms, so
        a letter restating the criterion is quoting the payer. That has always
        been allowed and must stay allowed; what was caught was the assertion
        that this patient's encounter was one.
        """
        source = _criterion("NBH-ENDO-031-3.3")
        assert is_faithful_restatement(f"Requires that {source}", source)
