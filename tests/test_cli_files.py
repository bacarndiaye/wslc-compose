import os

import pytest

from wslc_compose import cli
from wslc_compose.loader import ComposeError


def test_repeated_f_and_colon_separated(tmp_path, monkeypatch):
    a, b = tmp_path / "a.yaml", tmp_path / "b.yaml"
    a.write_text("services: {}")
    b.write_text("services: {}")
    sep = os.pathsep
    ns = cli.build_parser().parse_args(["-f", str(a), "-f", str(b), "ps"])
    assert cli._locate_compose_files(ns.file) == [str(a), str(b)]
    ns = cli.build_parser().parse_args(["-f", f"{a}{sep}{b}", "ps"])
    assert cli._locate_compose_files(ns.file) == [str(a), str(b)]


def test_compose_file_env_and_missing(tmp_path, monkeypatch):
    a = tmp_path / "a.yaml"
    a.write_text("services: {}")
    monkeypatch.setenv("COMPOSE_FILE", str(a))
    assert cli._locate_compose_files(None) == [str(a)]
    with pytest.raises(ComposeError, match="not found"):
        cli._locate_compose_files([str(tmp_path / "nope.yaml")])
