"""Publish the agent catalogue.

Run after any change to the gateway policy, the identity roster, or the ADK
definitions. Prints the table so a drift is visible immediately rather than
being discovered by whoever trusted the catalogue next.

    uv run python scripts/seed_registry.py            # print only
    uv run python scripts/seed_registry.py --publish  # write to the datastore
"""

from __future__ import annotations

import argparse
import sys

from core.registry import build_catalogue, render_table, seed
from core.store import build_store


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Write the catalogue to the datastore as well as printing it.",
    )
    args = parser.parse_args()

    catalogue = build_catalogue()
    print(render_table(catalogue))
    print()

    exposed = [a.agent_id for a in catalogue if a.handles_untrusted_input]
    print(f"Agents reading data from outside the organisation: {', '.join(exposed)}")
    scheduled = [a.agent_id for a in catalogue if a.invocation == "scheduled"]
    print(f"Agents running on a schedule rather than in the request path: {', '.join(scheduled)}")

    if args.publish:
        store = build_store()
        seed(store)
        print(f"\nPublished {len(catalogue)} entries to the agent registry.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
