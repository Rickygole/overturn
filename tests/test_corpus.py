"""Tests for the policy corpus.

Citations are only checkable because identifiers are stable and unique. These
tests defend that property, which the entire Verification agent depends on.
"""

from __future__ import annotations

import re

import pytest

from agents.retrieval.corpus import (
    all_identifiers,
    corpus_index,
    load_corpus,
    parse_policy_file,
)
from core.schemas.policy import SECTION_ID_PATTERN, PolicySection

CORPUS = load_corpus()


class TestCorpusShape:
    def test_all_six_policies_load(self):
        assert len({s.policy_id for s in CORPUS}) == 6

    def test_every_policy_has_coverage_criteria(self):
        """A policy with no numbered criteria cannot be appealed against."""
        by_policy: dict[str, int] = {}
        for section in CORPUS:
            by_policy[section.policy_id] = by_policy.get(section.policy_id, 0) + len(
                section.criteria
            )
        for policy_id, count in by_policy.items():
            assert count >= 4, f"{policy_id} has only {count} criteria"

    def test_every_policy_states_appeal_rights(self):
        """The appeal window drives the lifecycle deadline, so it must be there."""
        for policy_id in {s.policy_id for s in CORPUS}:
            sections = [s for s in CORPUS if s.policy_id == policy_id]
            headings = " ".join(s.section_heading for s in sections)
            assert "Appeal Rights" in headings, policy_id

    def test_every_policy_states_a_response_window(self):
        for policy_id in {s.policy_id for s in CORPUS}:
            appeal = next(
                s
                for s in CORPUS
                if s.policy_id == policy_id and s.section_heading == "Appeal Rights"
            )
            assert "30 calendar days" in appeal.text


class TestIdentifiers:
    def test_identifiers_are_unique(self):
        ids = [s.section_id for s in CORPUS]
        assert len(ids) == len(set(ids))

    def test_criterion_ids_are_unique_across_the_whole_corpus(self):
        criterion_ids = [c.criterion_id for s in CORPUS for c in s.criteria]
        assert len(criterion_ids) == len(set(criterion_ids))

    def test_every_identifier_matches_the_documented_pattern(self):
        pattern = re.compile(SECTION_ID_PATTERN)
        for identifier in all_identifiers(CORPUS):
            assert pattern.match(identifier), identifier

    def test_criterion_ids_nest_under_their_section(self):
        for section in CORPUS:
            for criterion in section.criteria:
                assert criterion.criterion_id.startswith(section.section_id + ".")


class TestVerbatimText:
    def test_section_text_contains_its_criteria_verbatim(self):
        """Verification quotes section text to check a claim.

        If the stored section text dropped its criteria, that check would be
        comparing a claim against a heading.
        """
        for section in CORPUS:
            for criterion in section.criteria:
                first_line = criterion.text.splitlines()[0].strip()
                assert first_line in section.text, criterion.criterion_id

    def test_no_section_is_a_stub(self):
        for section in CORPUS:
            assert len(section.text) > 60, section.section_id

    def test_source_uri_points_at_the_readable_file(self):
        for section in CORPUS:
            assert section.source_uri is not None
            assert section.source_uri.startswith("data/policies/")
            assert section.section_id in section.source_uri


class TestIndexing:
    def test_index_covers_every_section(self):
        index = corpus_index(CORPUS)
        assert len(index) == len(CORPUS)
        assert index["MHP-ENDO-031-3"].section_heading == "Coverage Criteria"

    def test_citable_identifier_count_is_stable(self):
        """A guard against silently losing criteria to a parser change."""
        assert len(all_identifiers(CORPUS)) == 113


class TestParserRejectsBadInput:
    def test_file_without_a_title_line_raises(self, tmp_path):
        bad = tmp_path / "MHP-BAD-001.md"
        bad.write_text("## MHP-BAD-001-1 — Scope\nSome text that is long enough.\n")
        with pytest.raises(Exception, match="no policy title"):
            parse_policy_file(bad)

    def test_file_with_no_sections_raises(self, tmp_path):
        bad = tmp_path / "MHP-BAD-002.md"
        bad.write_text("# MHP-BAD-002 — Nothing\nEffective: 2026-01-01 | Version: 1.0\n")
        with pytest.raises(Exception, match="zero sections"):
            parse_policy_file(bad)


class TestSchemaConformance:
    def test_every_section_is_a_valid_contract_instance(self):
        for section in CORPUS:
            assert isinstance(section, PolicySection)
            PolicySection.model_validate(section.to_firestore())
