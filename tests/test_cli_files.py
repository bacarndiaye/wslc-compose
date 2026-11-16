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


def test_override_file_is_overlaid_on_the_detected_file(tmp_path, monkeypatch):
    # Issue #59: like Docker Compose, compose.override.* next to the detected file is loaded.
    monkeypatch.delenv("COMPOSE_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "compose.yaml").write_text("services:\n  a:\n    image: x\n")
    assert cli._locate_compose_files(None) == [str(tmp_path / "compose.yaml")]

    (tmp_path / "docker-compose.override.yaml").write_text("services: {}\n")
    (tmp_path / "compose.override.yml").write_text("services:\n  a:\n    image: y\n")
    files = cli._locate_compose_files(None)
    assert files == [str(tmp_path / "compose.yaml"), str(tmp_path / "compose.override.yml")]
    assert cli.load_project(files).services["a"].image == "y"


def test_override_file_is_not_added_to_explicit_files(tmp_path, monkeypatch):
    a = tmp_path / "compose.yaml"
    a.write_text("services: {}")
    (tmp_path / "compose.override.yaml").write_text("services: {}")
    assert cli._locate_compose_files([str(a)]) == [str(a)]
    monkeypatch.setenv("COMPOSE_FILE", str(a))
    assert cli._locate_compose_files(None) == [str(a)]
