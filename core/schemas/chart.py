"""The patient chart, as the Mapping agent sees it.

Two design decisions worth stating.

**Everything carries a locator.** Every encounter, lab and medication has a
short human-readable string saying where in the record it lives. Mapping quotes
that locator as chart evidence, so a clerk reading a verdict can open the chart
at that spot and check. A verdict without a locator is an assertion; a verdict
with one is a citation.

**Everything carries provenance.** ``generated`` means Synthea produced it;
``authored`` means it was written for this project because a policy criterion
turns on it and a general-purpose generator does not produce records aligned to
one fictional payer's numbered criteria. Mixing the two silently would be the
dishonest option, so the field is required rather than optional.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import Field

from core.schemas.base import OverturnModel


class Provenance(StrEnum):
    GENERATED = "generated"  # Synthea output, unmodified
    AUTHORED = "authored"  # written for this project


class ChartProblem(OverturnModel):
    """One entry on the problem list."""

    description: str
    onset_date: date | None = None
    code: str | None = Field(default=None, description="SNOMED or ICD-10 code.")
    active: bool = True
    provenance: Provenance = Provenance.GENERATED


class ChartMedication(OverturnModel):
    """One medication, current or historical."""

    name: str
    dose: str | None = None
    route: str | None = None
    frequency: str | None = None
    start_date: date | None = None
    stop_date: date | None = None
    locator: str
    provenance: Provenance = Provenance.GENERATED


class LabResult(OverturnModel):
    """One result. Values stay strings so units and qualifiers survive intact."""

    name: str
    value: str
    unit: str | None = None
    observed_date: date
    reference_range: str | None = None
    abnormal: bool = False
    locator: str
    provenance: Provenance = Provenance.GENERATED


class Encounter(OverturnModel):
    """One visit and the note written at it."""

    encounter_id: str
    date: date
    encounter_type: str = Field(description="Office visit, telehealth, imaging, and so on.")
    clinician: str | None = None
    specialty: str | None = None
    reason: str | None = None
    note: str = Field(description="The narrative a reviewer would actually read.")
    locator: str = Field(description="Cited verbatim by Mapping as chart evidence.")
    provenance: Provenance = Provenance.GENERATED


class PatientChart(OverturnModel):
    """Everything the Mapping agent is given about one patient."""

    patient_id: str
    name: str
    date_of_birth: date
    sex: str
    member_id: str = Field(description="Northbeck Health Plan member identifier.")

    problems: list[ChartProblem] = Field(default_factory=list)
    medications: list[ChartMedication] = Field(default_factory=list)
    labs: list[LabResult] = Field(default_factory=list)
    encounters: list[Encounter] = Field(default_factory=list)

    generator: str = Field(
        default="synthea",
        description="Where the demographic and background record came from.",
    )
    authored_note: str | None = Field(
        default=None,
        description="What was written for this project and why, stated per chart.",
    )

    def age_on(self, when: date) -> int:
        born = self.date_of_birth
        return when.year - born.year - ((when.month, when.day) < (born.month, born.day))

    def locators(self) -> set[str]:
        """Every citable location in this chart.

        Mapping is checked against this set the same way Drafting is checked
        against the policy identifiers: an evidence locator that does not exist
        is a fabricated citation, and it is caught the same way.
        """
        found = {e.locator for e in self.encounters}
        found |= {lab.locator for lab in self.labs}
        found |= {m.locator for m in self.medications}
        return found

    def render(self) -> str:
        """Flatten the chart into the text handed to the Mapping agent.

        Locators are printed inline so the model cites a real one rather than
        inventing a plausible-looking reference.
        """
        lines: list[str] = [
            f"PATIENT: {self.name}",
            f"DOB: {self.date_of_birth.isoformat()}  SEX: {self.sex}  MEMBER ID: {self.member_id}",
            "",
            "PROBLEM LIST",
        ]
        for problem in self.problems:
            onset = f" (onset {problem.onset_date})" if problem.onset_date else ""
            status = "" if problem.active else " [resolved]"
            lines.append(f"  - {problem.description}{onset}{status}")

        if self.medications:
            lines += ["", "MEDICATIONS"]
            for med in self.medications:
                detail = " ".join(x for x in (med.dose, med.route, med.frequency) if x)
                lines.append(f"  - [{med.locator}] {med.name} {detail}".rstrip())

        if self.labs:
            lines += ["", "RESULTS"]
            for lab in sorted(self.labs, key=lambda x: x.observed_date):
                flag = " *ABNORMAL*" if lab.abnormal else ""
                unit = f" {lab.unit}" if lab.unit else ""
                lines.append(
                    f"  - [{lab.locator}] {lab.observed_date} {lab.name}: {lab.value}{unit}{flag}"
                )

        lines += ["", "ENCOUNTERS"]
        for enc in sorted(self.encounters, key=lambda e: e.date):
            who = f" — {enc.clinician}" if enc.clinician else ""
            spec = f" ({enc.specialty})" if enc.specialty else ""
            lines += [
                "",
                f"  [{enc.locator}] {enc.date} {enc.encounter_type}{spec}{who}",
            ]
            if enc.reason:
                lines.append(f"    Reason: {enc.reason}")
            for para in enc.note.strip().splitlines():
                lines.append(f"    {para.strip()}")

        return "\n".join(lines)
