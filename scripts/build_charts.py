"""Merge Synthea output with authored encounters into patient charts.

Synthea generates statistically plausible populations with no connection to any
real person, which is exactly what is needed for demographics, problem lists and
longitudinal labs. What it does not generate is a record that lines up against
one fictional payer's specific numbered criteria, because no generator could.

So the charts here are a merge, and the merge is explicit: every record carries
``provenance`` saying whether Synthea produced it or it was authored for this
project. Nothing is silently blended.

Usage::

    uv run python scripts/build_charts.py --synthea ~/synthea/output/csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from core.schemas.chart import (
    ChartMedication,
    ChartProblem,
    Encounter,
    LabResult,
    PatientChart,
    Provenance,
)

REPO = Path(__file__).resolve().parents[1]
OVERLAY_DIR = REPO / "data" / "charts" / "overlays"
OUTPUT_DIR = REPO / "data" / "charts"
CASES_FILE = REPO / "data" / "cases.json"

# Synthea appends numeric suffixes to names so that generated people are never
# mistaken for real ones. Keep them: they are a feature, not noise to clean up.
LAB_WHITELIST = {
    "Hemoglobin A1c/Hemoglobin.total in Blood": "Hemoglobin A1c",
    "Body mass index (BMI) [Ratio]": "Body mass index",
    "Systolic Blood Pressure": "Systolic blood pressure",
    "Glucose": "Glucose",
    "Creatinine": "Creatinine",
}


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def load_synthea(csv_dir: Path) -> dict[str, dict[str, Any]]:
    """Index Synthea CSV output by patient display name."""
    patients_path = csv_dir / "patients.csv"
    if not patients_path.exists():
        raise SystemExit(
            f"no Synthea output at {csv_dir}. Generate it with:\n"
            f"  java -jar synthea-with-dependencies.jar -p 8 -s 20260822 "
            f"--exporter.csv.export=true Massachusetts"
        )

    patients: dict[str, dict[str, Any]] = {}
    by_id: dict[str, str] = {}
    for row in csv.DictReader(patients_path.open()):
        name = f"{row['FIRST']} {row['LAST']}"
        patients[name] = {
            "id": row["Id"],
            "name": name,
            "dob": _parse_date(row["BIRTHDATE"]),
            "sex": row["GENDER"],
            "problems": [],
            "medications": [],
            "labs": [],
        }
        by_id[row["Id"]] = name

    conditions = defaultdict(list)
    for row in csv.DictReader((csv_dir / "conditions.csv").open()):
        conditions[row["PATIENT"]].append(row)
    for patient_id, rows in conditions.items():
        if (name := by_id.get(patient_id)) is None:
            continue
        seen: set[str] = set()
        for row in rows:
            if row["DESCRIPTION"] in seen:
                continue
            seen.add(row["DESCRIPTION"])
            patients[name]["problems"].append(
                ChartProblem(
                    description=row["DESCRIPTION"],
                    onset_date=_parse_date(row["START"]),
                    code=row.get("CODE") or None,
                    active=not row.get("STOP"),
                    provenance=Provenance.GENERATED,
                )
            )

    meds = defaultdict(list)
    for row in csv.DictReader((csv_dir / "medications.csv").open()):
        meds[row["PATIENT"]].append(row)
    for patient_id, rows in meds.items():
        if (name := by_id.get(patient_id)) is None:
            continue
        seen_meds: set[str] = set()
        for index, row in enumerate(rows[-12:]):
            if row["DESCRIPTION"] in seen_meds:
                continue
            seen_meds.add(row["DESCRIPTION"])
            patients[name]["medications"].append(
                ChartMedication(
                    name=row["DESCRIPTION"],
                    start_date=_parse_date(row["START"]),
                    stop_date=_parse_date(row.get("STOP", "")),
                    locator=f"med/synthea-{index:02d}",
                    provenance=Provenance.GENERATED,
                )
            )

    observations = defaultdict(list)
    for row in csv.DictReader((csv_dir / "observations.csv").open()):
        if row["DESCRIPTION"] in LAB_WHITELIST:
            observations[row["PATIENT"]].append(row)
    for patient_id, rows in observations.items():
        if (name := by_id.get(patient_id)) is None:
            continue
        for row in rows[-8:]:
            observed = _parse_date(row["DATE"])
            if observed is None:
                continue
            label = LAB_WHITELIST[row["DESCRIPTION"]]
            patients[name]["labs"].append(
                LabResult(
                    name=label,
                    value=row["VALUE"],
                    unit=row.get("UNITS") or None,
                    observed_date=observed,
                    locator=f"lab/synthea-{label.lower().replace(' ', '-')}-{observed}",
                    provenance=Provenance.GENERATED,
                )
            )

    return patients


def apply_overlay(base: dict[str, Any], overlay: dict[str, Any]) -> PatientChart:
    """Layer authored records on top of a generated patient."""
    problems = list(base["problems"])
    problems = [ChartProblem.model_validate(p) for p in overlay.get("problems", [])] + problems

    medications = [
        ChartMedication.model_validate(m) for m in overlay.get("medications", [])
    ] + list(base["medications"])

    labs = [LabResult.model_validate(x) for x in overlay.get("labs", [])] + list(base["labs"])

    encounters = [Encounter.model_validate(e) for e in overlay.get("encounters", [])]

    return PatientChart(
        patient_id=base["id"],
        name=base["name"],
        date_of_birth=base["dob"],
        sex="M" if base["sex"] == "M" else "F",
        member_id=overlay["member_id"],
        problems=problems,
        medications=medications,
        labs=labs,
        encounters=encounters,
        generator="synthea",
        authored_note=overlay.get("authored_note"),
    )


def build(csv_dir: Path) -> list[tuple[str, PatientChart]]:
    synthea = load_synthea(csv_dir)
    manifest = json.loads(CASES_FILE.read_text())

    built: list[tuple[str, PatientChart]] = []
    for case in manifest["cases"]:
        overlay_path = OVERLAY_DIR / f"{case['case_id']}.json"
        if not overlay_path.exists():
            continue
        overlay = json.loads(overlay_path.read_text())
        patient_name = overlay["patient"]
        if patient_name not in synthea:
            raise SystemExit(
                f"{overlay_path.name} references {patient_name!r}, which is not in the "
                f"Synthea output. Regenerate with the recorded seed: -s 20260822"
            )
        built.append((case["case_id"], apply_overlay(synthea[patient_name], overlay)))
    return built


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--synthea",
        type=Path,
        default=Path.home() / "synthea" / "output" / "csv",
        help="Directory holding Synthea CSV output.",
    )
    args = parser.parse_args()

    for case_id, chart in build(args.synthea):
        out = OUTPUT_DIR / f"{case_id}.json"
        out.write_text(json.dumps(chart.to_firestore(), indent=2) + "\n")
        authored = sum(1 for e in chart.encounters if e.provenance == Provenance.AUTHORED)
        print(
            f"{case_id}  {chart.name:26} {len(chart.problems):>2} problems  "
            f"{len(chart.labs):>2} labs  {len(chart.encounters):>2} encounters "
            f"({authored} authored)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
