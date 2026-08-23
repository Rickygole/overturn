"""What the verifier can and cannot be fooled by.

A red team got four different false claims past this and all four shared one
shape: keep the vocabulary, change the meaning. Word overlap is blind to
exactly that, because a sentence and its opposite share almost every word.

Each test below is one of those attacks.
"""

from __future__ import annotations

import pytest

from agents.offline.handlers import _content_words, _overlap

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
