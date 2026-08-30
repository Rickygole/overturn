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

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
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


# --------------------------------------------------------------------------- #
# Artifacts the contest rules require a judge to be able to reach
# --------------------------------------------------------------------------- #
#
# A judge scoring this submission recorded a Stage One FAIL: the architecture
# diagram was unreachable. It was being served the whole time and nothing linked
# to it, because the pages that used to link to it were deleted. Stage One is
# pass/fail on whether the required artifacts are present, so an unreachable
# diagram is worth exactly as much as no diagram.

def test_the_architecture_diagram_exists():
    assert (DOCS / "architecture.svg").is_file()


def test_the_diagram_is_reachable_from_the_front_door():
    """One click from the root, not one clone of the repository."""
    index = (DOCS / "index.html").read_text()
    reference = (DOCS / "system.html")
    assert reference.is_file(), "the page carrying the required artifacts is gone"
    assert "system.html" in index, "nothing on the front door links to it"
    assert "architecture.svg" in reference.read_text()


def test_the_reference_page_carries_setup_instructions():
    """The rules ask for reproducible spin-up steps a judge can find."""
    page = (DOCS / "system.html").read_text()
    for token in ("git clone", "uv sync", "scripts/evaluate.py"):
        assert token in page, f"{token!r} missing from the reference page"


def test_the_front_door_states_both_measures_not_only_the_flattering_one():
    """8/8 grounded and 6/8 outcomes measure different things.

    Publishing only the grounding figure, on a project whose credibility rests
    on having published the unflattering one, is the worst available trade.
    """
    index = (DOCS / "index.html").read_text()
    if "8/8" in index:
        assert "6/8" in index, "the front door publishes 8/8 without 6/8"


# --------------------------------------------------------------------------- #
# The same judge, second finding: navigation is a cul-de-sac
# --------------------------------------------------------------------------- #
#
# "From the root page the only destination is the queue. From the queue, cases.
# From a case, back to the queue. There is no path to architecture, evaluation,
# findings, or anything that explains the system." A reader who signs in first
# would never learn the system had been evaluated at all -- including the Model
# Armor negative result, which the same judge called worth more than any feature
# on the list. Linking out of the review flow was previously asserted nowhere.

TEMPLATES = ROOT / "services" / "approval_ui" / "templates"


def test_every_page_behind_the_door_offers_a_way_to_the_reference_page():
    """The masthead link lives in the base template, so every page inherits it."""
    base = (TEMPLATES / "base.html").read_text()
    assert "/system.html" in base, "no exit from the review flow to the reference page"


def test_the_footer_reaches_the_diagram_and_the_measurements():
    base = (TEMPLATES / "base.html").read_text()
    for target in ("/architecture.svg", "/system.html#measured"):
        assert target in base, f"{target} unreachable from inside the app"


def test_the_reference_page_carries_the_anchors_those_links_point_at():
    """A deep link to a heading that does not exist lands at the top silently."""
    page = (DOCS / "system.html").read_text()
    for anchor in ("measured", "architecture", "screening", "run"):
        assert f'id="{anchor}"' in page, f"#{anchor} is not a heading on the page"


def test_nothing_behind_the_door_links_off_host():
    """The exits are same-origin.

    Two rendering tests assert the review screens load no external asset and
    invent no outbound link. A footer link to the repository broke both, which
    is the correct outcome: the reference page carries outbound links, the
    screens where somebody signs a letter do not.
    """
    base = (TEMPLATES / "base.html").read_text()
    assert "https://" not in base, "the base template links off-host"


# --------------------------------------------------------------------------- #
# The economic premise, which was asserted without a number
# --------------------------------------------------------------------------- #
#
# "You tell me appeals are rarely filed and often won. You never say how many,
# or how often, or cite anything." Both surfaces now carry the KFF figures, and
# a statistic without its source is the failure mode this project exists to
# argue against.

def test_the_premise_is_quantified_and_sourced_on_both_surfaces():
    """"34%" was never KFF's number -- their own word is "upheld," not
    "overturned," and the two figures we used to print side by side did not
    even reconcile (262,982 minus KFF's 165,863 upheld is 36.9%, not 34%).
    Checked against "66% upheld," their actual verbatim framing, instead.

    "34%" is allowed to survive inside an HTML comment explaining the
    correction -- docs/index.html does exactly that -- so comments are
    stripped before the negative check, the same way a reader's browser
    would never render them.
    """
    for text in ((DOCS / "index.html").read_text(), (ROOT / "README.md").read_text()):
        assert "262,982" in text, "the appeal count is missing"
        assert "66%" in text, "the sourced uphold rate is missing"
        assert "kff.org" in text, "the figures are stated without their source"
        rendered = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
        assert "34%" not in rendered, "the old, unreconciled overturn figure is back on the page"


# --------------------------------------------------------------------------- #
# The evaluation has to be reachable, not merely written
# --------------------------------------------------------------------------- #
#
# The remaining job on this submission is the video and "getting the
# architecture and evaluation back somewhere a judge can actually click to."
# The diagram is served from the deployment. The evaluation is 337 lines of
# markdown that, until now, needed a clone to read: the reference page carried
# a summary of it and nothing that led to the thing itself.

def test_the_reference_page_leads_to_the_full_evaluation():
    page = (DOCS / "system.html").read_text()
    assert "docs/EVALUATION.md" in page, "no route from the summary to the evaluation"
    assert (DOCS / "EVALUATION.md").is_file()


def test_the_reference_page_leads_to_the_architecture_prose():
    page = (DOCS / "system.html").read_text()
    assert "docs/ARCHITECTURE.md" in page
    assert (DOCS / "ARCHITECTURE.md").is_file()


def test_the_video_script_does_not_promise_a_login_that_no_longer_appears():
    """Reading the queue stopped needing a password; signing still does.

    The script is a checklist for a one-take recording. It told the presenter
    the queue sat behind the app password, which stopped being true when the
    queue was opened deliberately, and a presenter waiting for a login wall at
    /queue is waiting for a screen that will not render.
    """
    script = (DOCS / "VIDEO_SCRIPT.md").read_text()
    assert "review queue at `/queue` behind the app password" not in script
