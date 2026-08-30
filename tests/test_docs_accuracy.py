"""Tests that documentation matches the code it describes.

A README is a claim and an architecture diagram is a claim. Both are read by
people deciding whether to trust the system, and a permission table that has
quietly drifted from the policy it depicts is worse than no table — it is a
confident, checkable, wrong statement about who can touch patient data.

These tests are cheap and they are the difference between documentation that is
true and documentation that was true once.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.gateway import POLICY, Access
from core.schemas.enums import AgentName

DOCS = Path(__file__).resolve().parents[1] / "docs"
REPO = Path(__file__).resolve().parents[1]

ACCESS_WORDS = {
    "read": Access.READ,
    "append": Access.APPEND,
    "write": Access.WRITE,
    "—": Access.NONE,
    "-": Access.NONE,
}


def _permission_table() -> tuple[list[str], dict[str, dict[str, Access]]]:
    """Parse the agent/collection grid out of ARCHITECTURE.md."""
    lines = (DOCS / "ARCHITECTURE.md").read_text().splitlines()
    header_index = next(
        i for i, line in enumerate(lines) if line.startswith("| Agent |") and "cases" in line
    )
    columns = [c.strip().strip("`") for c in lines[header_index].split("|")[1:-1]]
    collections = [c for c in columns if c in {c2 for grants in POLICY.values() for c2 in grants}]

    parsed: dict[str, dict[str, Access]] = {}
    for line in lines[header_index + 2 :]:
        if not line.startswith("|"):
            break
        cells = [c.strip().strip("*").strip("`") for c in line.split("|")[1:-1]]
        agent = cells[0]
        row: dict[str, Access] = {}
        for column, cell in zip(columns, cells, strict=False):
            if column in collections:
                row[column] = ACCESS_WORDS.get(cell, Access.NONE)
        parsed[agent] = row
    return collections, parsed


class TestPermissionTable:
    def test_every_agent_appears(self):
        _, table = _permission_table()
        assert {a.value for a in AgentName} <= set(table)

    @pytest.mark.parametrize("agent", list(AgentName))
    def test_the_documented_grants_match_the_enforced_ones(self, agent):
        collections, table = _permission_table()
        documented = table[agent.value]
        for collection in collections:
            actual = POLICY[agent].get(collection, Access.NONE)
            assert documented[collection] is actual, (
                f"ARCHITECTURE.md says {agent.value} has "
                f"{documented[collection].value!r} on {collection!r}; "
                f"core/gateway.py enforces {actual.value!r}"
            )

    def test_the_two_headline_absences_are_still_absent(self):
        """The claims the diagram is built around.

        If either of these ever becomes true, the architecture story changes and
        the diagram has to change with it.
        """
        assert POLICY[AgentName.DRAFTING].get("policy_sections", Access.NONE) is Access.NONE
        assert POLICY[AgentName.VERIFICATION].get("cases") is Access.READ


class TestDiagram:
    def test_the_svg_is_well_formed(self):
        import xml.dom.minidom

        xml.dom.minidom.parse(str(DOCS / "architecture.svg"))

    def test_it_references_nothing_external(self):
        """A strict viewer, or no network, must not change what a judge sees."""
        content = (DOCS / "architecture.svg").read_text()
        stripped = content.replace("http://www.w3.org", "")
        assert "http" not in stripped
        assert "<image" not in stripped

    def test_it_is_embedded_in_the_architecture_document(self):
        assert "architecture.svg" in (DOCS / "ARCHITECTURE.md").read_text()


class TestReadmeClaims:
    def test_the_payer_is_never_the_real_one(self):
        """The rename that had to happen. Case-insensitive, because the first
        pass missed the letterheads."""
        for path in REPO.rglob("*"):
            if path.suffix not in {".md", ".py", ".txt", ".json", ".sh", ".html"}:
                continue
            if any(part in {".venv", ".git", "__pycache__"} for part in path.parts):
                continue
            if path.name in {"rename_payer.py", Path(__file__).name}:
                # The rename script documents the old name in its docstring, and
                # this test has to name what it is looking for.
                continue
            assert "meridian" not in path.read_text(errors="ignore").lower(), path

    def test_the_documented_commands_exist(self):
        readme = (REPO / "README.md").read_text()
        for script in re.findall(r"uv run python (scripts/[\w_]+\.py)", readme):
            assert (REPO / script).exists(), f"README references missing {script}"
        for script in re.findall(r"bash (infra/[\w_]+\.sh)", readme):
            assert (REPO / script).exists(), f"README references missing {script}"

    def test_the_scorecard_matches_the_manifest(self):
        """The published table must cover every case that exists."""
        import json

        readme = (REPO / "README.md").read_text()
        manifest = json.loads((REPO / "data" / "cases.json").read_text())
        for entry in manifest["cases"]:
            assert f"`{entry['case_id']}`" in readme, (
                f"{entry['case_id']} is in the manifest but not in the README scorecard"
            )

    def test_the_published_numbers_are_the_numbers_it_produces(self):
        """The earlier version of this test only checked that case ids appeared.

        It was written to stop the scorecard rotting and it did not: the README
        went on claiming 36 citations while the harness printed 29, and 189
        tests while 399 ran. Both are the first things a judge sees and both are
        checkable in one command, which is the worst combination.
        """
        import re
        import subprocess

        readme = (REPO / "README.md").read_text()
        result = subprocess.run(
            ["uv", "run", "python", "scripts/evaluate.py"],
            capture_output=True,
            text=True,
            cwd=REPO,
        )
        for line in result.stdout.splitlines():
            match = re.match(
                r"^(outcomes correct|cases fully grounded|citations checked|"
                r"citations not in the corpus|chart quotes with no locator)\s+(\S+)",
                line,
            )
            if match:
                label, value = match.group(1), match.group(2)
                assert f"{label}" in readme, f"README does not report {label!r}"
                published = re.search(rf"{re.escape(label)}\s+(\S+)", readme)
                assert published and published.group(1) == value, (
                    f"README says {label} = {published.group(1) if published else '?'}, "
                    f"the harness prints {value}"
                )

    def test_the_quickstart_test_count_is_current(self):
        import re
        import subprocess

        readme = (REPO / "README.md").read_text()
        collected = subprocess.run(
            ["uv", "run", "pytest", "--collect-only", "-q"],
            capture_output=True,
            text=True,
            cwd=REPO,
        ).stdout
        actual = re.search(r"(\d+) tests collected", collected)
        assert actual, "could not determine the test count"
        claimed = re.search(r"# (\d+) tests", readme)
        assert claimed, "README quickstart no longer states a test count"
        assert claimed.group(1) == actual.group(1), (
            f"README quickstart says {claimed.group(1)} tests, {actual.group(1)} run"
        )


# --------------------------------------------------------------------------- #
# The published site
# --------------------------------------------------------------------------- #
#
# Everything above guards the markdown. The site under docs/*.html is the
# surface a judge actually reads, and until 29 August it had no harness at all
# -- which is how it came to assert a provenance feature that does not exist,
# an ADK responsibility our own code disclaims, and a test count seven behind
# the truth. A claim is not safer for being written in HTML.

SITE_PAGES = sorted((DOCS).glob("*.html"))


def _site_text() -> dict[Path, str]:
    return {page: page.read_text() for page in SITE_PAGES}


def test_there_is_a_site_to_check():
    """A guard on the guard: if the pages move, these tests must not pass vacuously.

    The site is one page now -- the explanatory pages were removed, because a
    product's signed-out surface is a door, not a brochure. These checks still
    matter: the front door names the model tier and the grounding result, and
    those are exactly the claims that drifted before.
    """
    assert SITE_PAGES, "no docs/*.html found — the tests below would be checking nothing"


@pytest.mark.parametrize("page", SITE_PAGES, ids=lambda p: p.name)
def test_the_site_does_not_claim_source_offsets(page: Path):
    """`DenialExtraction` carries no offset, span, or locator.

    Intake returns typed fields, not character positions. Provenance is this
    project's whole thesis, which makes a fabricated provenance feature the
    single worst claim the site could carry.
    """
    text = page.read_text().lower()
    for phrase in ("character offset", "source offset", "offset in the source"):
        assert phrase not in text, f"{page.name} claims {phrase!r}; DenialExtraction has no such field"


@pytest.mark.parametrize("page", SITE_PAGES, ids=lambda p: p.name)
def test_the_site_does_not_credit_adk_with_orchestration(page: Path):
    """`agents/adk_fleet.py` disclaims exactly this.

    ADK owns every model call. The orchestration is ours, deliberately, because
    a Runner session cannot survive the multi-week gap a real appeal takes.
    """
    text = page.read_text().lower()
    if "agent development kit" not in text and "adk" not in text:
        return
    for phrase in ("tool binding", "orchestration between the seven"):
        assert phrase not in text, (
            f"{page.name} credits ADK with {phrase!r}; adk_fleet.py says the opposite "
            f"and binds no tools to any agent"
        )


@pytest.mark.parametrize("page", SITE_PAGES, ids=lambda p: p.name)
def test_the_site_test_count_matches_the_readme(page: Path):
    """The count is allowed to appear on the site; it is not allowed to drift.

    The README count is already pinned to the collected total by
    ``test_the_quickstart_test_count_is_current``, so matching the README is
    enough to keep the site honest without collecting twice.
    """
    claimed = re.findall(r"([0-9]{3,4})\s+tests\b", page.read_text())
    if not claimed:
        return
    readme = re.search(r"([0-9]{3,4})\s+tests", (REPO / "README.md").read_text())
    assert readme, "README no longer states a test count for the site to match"
    for number in claimed:
        assert number == readme.group(1), (
            f"{page.name} says {number} tests; README says {readme.group(1)}"
        )


@pytest.mark.parametrize("page", SITE_PAGES, ids=lambda p: p.name)
def test_the_site_names_the_cloud_run_services_that_exist(page: Path):
    """infra/deploy.sh deploys overturn-ingest, overturn, overturn-scheduler.

    There is no orchestrator service. Naming one also erases the scheduler,
    which is the service that makes the multi-week escalation claim real.
    """
    text = page.read_text().lower()
    match = re.search(r"three services:([^<.]*)", text)
    if not match:
        return
    listed = match.group(1)
    assert "orchestrator" not in listed, (
        f"{page.name} lists an 'orchestrator' Cloud Run service; deploy.sh deploys "
        f"overturn-ingest, overturn, overturn-scheduler"
    )
    assert "scheduler" in listed, (
        f"{page.name} enumerates three Cloud Run services without the scheduler -- "
        f"the one that makes the multi-week escalation claim real"
    )
