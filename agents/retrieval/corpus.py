"""Parse the Northbeck policy corpus into retrievable sections.

The markdown files in ``data/policies`` are the source of truth, not a rendering
of some other source. The document a judge sees on screen when the video cuts to
"does that section really say that" is the same file this parser reads, which is
the point.

Format::

    # NBH-ENDO-031 — Continuous Glucose Monitoring Systems
    Effective: 2026-01-01 | Version: 3.1 | ...

    ## NBH-ENDO-031-3 — Coverage Criteria
    Prose introducing the criteria.

    ### NBH-ENDO-031-3.1
    The text of one numbered criterion.

Section text is stored verbatim, criteria included. Verification quotes from it
to check whether a citation says what the letter claims it says, so a
paraphrase here would quietly undermine the one check that matters most.
"""

from __future__ import annotations

import re
from pathlib import Path

from core.schemas.policy import PolicyCriterion, PolicySection

POLICY_DIR = Path(__file__).resolve().parents[2] / "data" / "policies"

_TITLE = re.compile(r"^#\s+(?P<policy_id>[A-Z]{3}-[A-Z]{3,5}-\d{3})\s+[—-]\s+(?P<title>.+)$")
_SECTION = re.compile(
    r"^##\s+(?P<section_id>[A-Z]{3}-[A-Z]{3,5}-\d{3}-\d+)\s+[—-]\s+(?P<heading>.+)$"
)
_CRITERION = re.compile(r"^###\s+(?P<criterion_id>[A-Z]{3}-[A-Z]{3,5}-\d{3}-\d+(?:\.\d+)+)\s*$")
_META = re.compile(r"Effective:\s*(?P<effective>[\d-]+)\s*\|\s*Version:\s*(?P<version>[\w.]+)")


class CorpusError(ValueError):
    """The corpus does not parse. Always a corpus bug, never a runtime condition."""


def parse_policy_file(path: Path) -> list[PolicySection]:
    """Turn one policy document into its sections."""
    lines = path.read_text(encoding="utf-8").splitlines()

    policy_id: str | None = None
    policy_title = ""
    effective: str | None = None
    version: str | None = None

    sections: list[PolicySection] = []
    current: dict | None = None
    current_criterion: str | None = None
    criterion_lines: list[str] = []

    def close_criterion() -> None:
        nonlocal current_criterion, criterion_lines
        if current_criterion and current is not None:
            text = "\n".join(criterion_lines).strip()
            if text:
                current["criteria"].append(
                    PolicyCriterion(criterion_id=current_criterion, text=text)
                )
        current_criterion = None
        criterion_lines = []

    def close_section() -> None:
        nonlocal current
        close_criterion()
        if current is not None:
            body = "\n".join(current["body"]).strip()
            sections.append(
                PolicySection(
                    section_id=current["section_id"],
                    policy_id=policy_id or "",
                    policy_title=policy_title,
                    section_heading=current["heading"],
                    text=body,
                    criteria=current["criteria"],
                    effective_date=effective,
                    version=version,
                    source_uri=f"data/policies/{path.name}#{current['section_id']}",
                )
            )
        current = None

    for line in lines:
        if match := _TITLE.match(line):
            policy_id = match.group("policy_id")
            policy_title = match.group("title").strip()
            continue

        if policy_id and (match := _META.search(line)):
            effective = match.group("effective")
            version = match.group("version")
            continue

        if match := _SECTION.match(line):
            close_section()
            current = {
                "section_id": match.group("section_id"),
                "heading": match.group("heading").strip(),
                "body": [],
                "criteria": [],
            }
            continue

        if match := _CRITERION.match(line):
            close_criterion()
            current_criterion = match.group("criterion_id")
            if current is not None:
                current["body"].append(line)
            continue

        if current is not None:
            current["body"].append(line)
            if current_criterion:
                criterion_lines.append(line)

    close_section()

    if policy_id is None:
        raise CorpusError(f"{path.name} has no policy title line")
    if not sections:
        raise CorpusError(f"{path.name} parsed to zero sections")
    return sections


def load_corpus(directory: Path | None = None) -> list[PolicySection]:
    """Every section in the corpus, sorted by identifier."""
    directory = directory or POLICY_DIR
    sections: list[PolicySection] = []
    for path in sorted(directory.glob("NBH-*.md")):
        sections.extend(parse_policy_file(path))

    seen: dict[str, str] = {}
    for section in sections:
        if section.section_id in seen:
            raise CorpusError(
                f"duplicate section id {section.section_id} in "
                f"{seen[section.section_id]} and {section.source_uri}"
            )
        seen[section.section_id] = section.source_uri or "?"

    sections.sort(key=lambda s: s.section_id)
    return sections


def corpus_index(sections: list[PolicySection]) -> dict[str, PolicySection]:
    """Section id to section, for direct lookup by Verification."""
    return {s.section_id: s for s in sections}


def all_identifiers(sections: list[PolicySection]) -> set[str]:
    """Every citable identifier in the corpus, sections and criteria alike."""
    ids: set[str] = set()
    for section in sections:
        ids.add(section.section_id)
        ids.update(c.criterion_id for c in section.criteria)
    return ids
