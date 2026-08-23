"""Rename the fictional payer across the entire corpus, in one command.

This exists because the first name chosen for the fictional payer turned out to
belong to a real health insurer operating in the same line of business. That is
a trademark problem, a hackathon rules problem, and — since every demo scenario
depicts the payer denying care — a defamation-shaped problem. The fix had to be
mechanical and complete, and it has to stay easy to redo if the replacement name
also turns out to be taken.

Usage::

    uv run python scripts/rename_payer.py --from-name "Meridian" --to-name "Northbeck" \\
        --from-prefix MHP --to-prefix NBH

Always re-run the test suite afterwards. ``tests/test_corpus.py`` asserts on
concrete identifiers and will catch a partial rename.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Files that may mention the payer. Deliberately explicit: a blanket walk of the
# repository would rewrite the .git directory and the virtualenv.
SEARCH_GLOBS = [
    "data/policies/*.md",
    "data/charts/overlays/*.json",
    "data/charts/*.json",
    "data/denials/*.txt",
    "data/*.json",
    "core/**/*.py",
    "agents/**/*.py",
    "services/**/*.py",
    "scripts/*.py",
    "tests/*.py",
    "docs/*.md",
    "infra/*.sh",
    "README.md",
    "pyproject.toml",
]


def iter_files() -> list[Path]:
    seen: set[Path] = set()
    for pattern in SEARCH_GLOBS:
        for path in REPO.glob(pattern):
            if path.is_file() and ".venv" not in path.parts:
                if path.name == Path(__file__).name:
                    continue  # this script documents the old name in its own docstring
                seen.add(path)
    return sorted(seen)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-name", required=True, help="e.g. Meridian")
    parser.add_argument("--to-name", required=True, help="e.g. Northbeck")
    parser.add_argument("--from-prefix", required=True, help="e.g. MHP")
    parser.add_argument("--to-prefix", required=True, help="e.g. NBH")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    replacements = [
        (args.from_name, args.to_name),
        (args.from_prefix + "-", args.to_prefix + "-"),
    ]

    changed: list[tuple[Path, int]] = []
    for path in iter_files():
        try:
            original = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        updated = original
        for old, new in replacements:
            updated = updated.replace(old, new)
        if updated != original:
            hits = sum(original.count(old) for old, _ in replacements)
            changed.append((path, hits))
            if not args.dry_run:
                path.write_text(updated, encoding="utf-8")

    # Policy documents are named after their identifier, so the files move too.
    renamed: list[tuple[str, str]] = []
    for path in sorted((REPO / "data" / "policies").glob(f"{args.from_prefix}-*.md")):
        target = path.with_name(path.name.replace(args.from_prefix + "-", args.to_prefix + "-", 1))
        renamed.append((path.name, target.name))
        if not args.dry_run:
            path.rename(target)

    verb = "would change" if args.dry_run else "changed"
    for path, hits in changed:
        print(f"  {verb:13} {path.relative_to(REPO)}  ({hits} occurrences)")
    for old_name, new_name in renamed:
        print(f"  {'would rename' if args.dry_run else 'renamed':13} {old_name} -> {new_name}")
    print(f"\n{len(changed)} files {verb}, {len(renamed)} renamed.")

    if not args.dry_run:
        print("\nNow run: uv run pytest -q")
    return 0


if __name__ == "__main__":
    sys.exit(main())
