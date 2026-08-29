"""Measure what each screening layer actually catches.

Run against the deployed project to reproduce `docs/SCREENING_LAYERS.md`. The
interesting column is Model Armor's, and the interesting result is that it does
not flag the injection — which is the whole reason the consequence of a finding
is decided in Python rather than deferred to any one detector.
"""

from __future__ import annotations

import sys
from pathlib import Path

from agents.sentinel.armor import build_armor
from agents.sentinel.rules import decide_quarantine, scan
from core.config import get_settings

REPO = Path(__file__).resolve().parents[1]
DENIALS = REPO / "data" / "denials"


def main() -> int:
    settings = get_settings()
    armor = build_armor(settings)
    print(f"Model Armor client: {armor.name}")
    if armor.name != "enabled":
        print("  (set OVERTURN_RUNTIME_MODE=cloud and OVERTURN_MODEL_ARMOR_TEMPLATE)")
    print()

    documents = sorted(DENIALS.glob("CASE-*.txt")) + sorted((DENIALS / "attacks").glob("*.txt"))
    header = f"{'document':34} {'armor':>7} {'rules':>7}  quarantined"
    print(header)
    print("-" * len(header))

    for path in documents:
        text = path.read_text()
        try:
            armor_findings = armor.screen(text)
            armor_count: object = len(armor_findings)
        except Exception as exc:  # a guardrail that crashes is worse than one that misses
            armor_count = f"err({type(exc).__name__})"
        rule_findings = scan(text)
        name = path.name if path.parent == DENIALS else f"attacks/{path.name}"
        print(
            f"{name:34} {str(armor_count):>7} {len(rule_findings):>7}  "
            f"{decide_quarantine(rule_findings)}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
