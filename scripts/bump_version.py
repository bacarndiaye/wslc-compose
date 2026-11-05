#!/usr/bin/env python3
"""Keep every place that mentions the current release in sync (issue #27).

    python scripts/bump_version.py 0.5.0     # bump everything to 0.5.0
    python scripts/bump_version.py --check   # fail if a doc pin differs from pyproject.toml

Touched files: pyproject.toml, src/wslc_compose/__init__.py, README.md,
docs/MIGRATION.md, docs/index.html and a CHANGELOG.md heading for the new version.
install.sh / install.ps1 stay on ``wslc-compose==0.*`` on purpose.
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = r"\d+\.\d+\.\d+"

PYPROJECT = ROOT / "pyproject.toml"
INIT = ROOT / "src" / "wslc_compose" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"

# (file, regex with the version in group "v") — every match must equal the release.
DOC_PINS = [
    (ROOT / "README.md", rf'wslc-compose==(?P<v>{VERSION})'),
    (ROOT / "README.md", rf'`(?P<v>{VERSION})` is the latest release'),
    (ROOT / "docs" / "MIGRATION.md", rf'wslc-compose==(?P<v>{VERSION})'),
    (ROOT / "docs" / "index.html", rf'wslc-compose==<span class="ver">(?P<v>{VERSION})</span>'),
    (ROOT / "docs" / "index.html", rf'id="latest-version">v(?P<v>{VERSION})<'),
]
SOURCES = [
    (PYPROJECT, rf'(?m)^version = "(?P<v>{VERSION})"'),
    (INIT, rf'(?m)^__version__ = "(?P<v>{VERSION})"'),
]


def current_version() -> str:
    match = re.search(SOURCES[0][1], PYPROJECT.read_text(encoding="utf-8"))
    if not match:
        raise SystemExit("cannot find the version in pyproject.toml")
    return match["v"]


def _substitute(path: Path, pattern: str, new: str) -> int:
    text = path.read_text(encoding="utf-8")

    def swap(match: re.Match) -> str:
        whole, start = match[0], match.start()
        return whole[: match.start("v") - start] + new + whole[match.end("v") - start:]

    updated, count = re.subn(pattern, swap, text)
    if count:
        path.write_text(updated, encoding="utf-8")
    return count


def problems(expected: str) -> list:
    """Every pinned version that is not `expected`, plus pins that vanished."""
    found = []
    for path, pattern in SOURCES + DOC_PINS:
        text = path.read_text(encoding="utf-8")
        matches = list(re.finditer(pattern, text))
        if not matches:
            found.append(f"{path.relative_to(ROOT)}: pattern not found: {pattern}")
        for match in matches:
            if match["v"] != expected:
                found.append(f"{path.relative_to(ROOT)}: {match['v']} != {expected}")
    if f"## {expected} " not in CHANGELOG.read_text(encoding="utf-8"):
        found.append(f"CHANGELOG.md: no '## {expected} — …' heading")
    return found


def bump(new: str) -> None:
    if not re.fullmatch(VERSION, new):
        raise SystemExit(f"invalid version {new!r} (expected X.Y.Z)")
    for path, pattern in SOURCES + DOC_PINS:
        if not _substitute(path, pattern, new):
            raise SystemExit(f"{path.relative_to(ROOT)}: pattern not found: {pattern}")
    changelog = CHANGELOG.read_text(encoding="utf-8")
    if f"## {new} " not in changelog:
        heading = f"## {new} — {datetime.date.today().isoformat()}\n\n### Added\n- \n\n"
        CHANGELOG.write_text(changelog.replace("# Changelog\n\n", "# Changelog\n\n" + heading, 1),
                             encoding="utf-8")
        print(f"CHANGELOG.md: added a '## {new}' section — fill it in")
    print(f"bumped to {new}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", nargs="?", help="new version X.Y.Z")
    parser.add_argument("--check", action="store_true", help="only verify that everything is in sync")
    args = parser.parse_args(argv)
    if args.check or not args.version:
        errors = problems(current_version())
        for line in errors:
            print(line, file=sys.stderr)
        if errors:
            print("run: python scripts/bump_version.py <version>", file=sys.stderr)
        return 1 if errors else 0
    bump(args.version)
    return 1 if problems(args.version) else 0


if __name__ == "__main__":
    raise SystemExit(main())
