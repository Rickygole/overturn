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
