"""Loading patient charts from disk."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from core.schemas.chart import PatientChart

CHART_DIR = Path(__file__).resolve().parents[2] / "data" / "charts"


class ChartNotFound(FileNotFoundError):
    """No chart on file for this case."""


@lru_cache(maxsize=32)
def load_chart(case_id: str) -> PatientChart:
    """Load and validate one chart.

    Cached because Mapping is called once per policy section and the chart does
    not change between those calls.
    """
    path = CHART_DIR / f"{case_id}.json"
    if not path.exists():
        raise ChartNotFound(
            f"no chart at {path}. Build it with: uv run python scripts/build_charts.py"
        )
    return PatientChart.model_validate(json.loads(path.read_text()))


def available_charts() -> list[str]:
    return sorted(p.stem for p in CHART_DIR.glob("CASE-*.json"))
