"""The version pinned in the docs must match pyproject.toml (issue #27)."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bump_version.py"


def _load():
    spec = importlib.util.spec_from_file_location("bump_version", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_doc_pins_match_the_released_version():
    bump = _load()
    assert bump.problems(bump.current_version()) == [], (
        "docs are out of sync with pyproject.toml; run: python scripts/bump_version.py <version>"
    )


def test_check_detects_a_stale_pin():
    bump = _load()
    assert any("!=" in line for line in bump.problems("99.0.0"))


def test_invalid_version_is_rejected():
    bump = _load()
    try:
        bump.bump("1.2")
    except SystemExit as exc:
        assert "invalid version" in str(exc)
    else:
        raise AssertionError("expected SystemExit")


def test_changelog_unreleased_section_becomes_the_release():
    bump = _load()
    text = "# Changelog\n\n## Unreleased\n\n### Fixed\n- x\n\n## 0.1.0 — 2026-01-01\n"
    assert bump.changelog_with_heading(text, "0.2.0", "2026-02-02") == (
        "# Changelog\n\n## 0.2.0 — 2026-02-02\n\n### Fixed\n- x\n\n## 0.1.0 — 2026-01-01\n")
    # without an Unreleased section, an empty one is added to fill in
    assert bump.changelog_with_heading("# Changelog\n\n## 0.1.0\n", "0.2.0", "2026-02-02") == (
        "# Changelog\n\n## 0.2.0 — 2026-02-02\n\n### Added\n- \n\n## 0.1.0\n")
