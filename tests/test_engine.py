import subprocess

import pytest

from wslc_compose import engine
from wslc_compose.engine import WslcError


def _ok(args):
    return subprocess.CompletedProcess(args, 0, "", "")


def test_run_retried_recovers_from_transient_error(monkeypatch):
    calls = []

    def fake_run(args, capture=False, check=True, dry_run=False):
        calls.append(list(args))
        if len(calls) == 1:
            raise WslcError(
                "wslc start foo failed (exit 1): Impossible de créer un fichier "
                "déjà existant. Code d'erreur : ERROR_ALREADY_EXISTS"
            )
        return _ok(args)

    monkeypatch.setattr(engine, "run", fake_run)
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)

    proc = engine.run_retried(["start", "foo"])
    assert proc.returncode == 0
    assert len(calls) == 2


def test_run_retried_gives_up_after_max_attempts(monkeypatch):
    calls = []

    def fake_run(args, capture=False, check=True, dry_run=False):
        calls.append(list(args))
        raise WslcError("wslc start foo failed (exit 1): ERROR_SHARING_VIOLATION")

    monkeypatch.setattr(engine, "run", fake_run)
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)

    with pytest.raises(WslcError):
        engine.run_retried(["start", "foo"], retries=3)
    assert len(calls) == 3


def test_run_retried_does_not_retry_other_errors(monkeypatch):
    calls = []

    def fake_run(args, capture=False, check=True, dry_run=False):
        calls.append(list(args))
        raise WslcError("wslc start foo failed (exit 1): no such container")

    monkeypatch.setattr(engine, "run", fake_run)
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)

    with pytest.raises(WslcError):
        engine.run_retried(["start", "foo"])
    assert len(calls) == 1


def test_run_uses_utf8_for_captured_output(monkeypatch):
    captured = {}

    def fake_subprocess_run(*args, **kwargs):
        captured.update(kwargs)
        return _ok(args[0])

    monkeypatch.setattr(engine, "find_wslc", lambda: "wslc.exe")
    monkeypatch.setattr(engine.subprocess, "run", fake_subprocess_run)

    engine.run(["list"], capture=True)

    assert captured["check"] is False
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("[]", []),
        ('{"Id": "one"}', [{"Id": "one"}]),
        ('{"Id": "one"}\n{"Id": "two"}', [{"Id": "one"}, {"Id": "two"}]),
    ],
)
def test_capture_json_accepts_json_and_ndjson(monkeypatch, output, expected):
    monkeypatch.setattr(
        engine,
        "run",
        lambda args, capture=True: subprocess.CompletedProcess(args, 0, output, ""),
    )

    assert engine.capture_json(["list", "--format", "json"]) == expected


def test_network_names_uses_json_inventory(monkeypatch):
    calls = []

    def fake_capture_json(args):
        calls.append(args)
        return [{"Name": "purchase-service"}, {"Name": "bridge"}]

    monkeypatch.setattr(engine, "capture_json", fake_capture_json)

    assert engine.network_names() == ["purchase-service", "bridge"]
    assert calls == [["network", "list", "--format", "json"]]


def test_volume_names_uses_json_inventory(monkeypatch):
    monkeypatch.setattr(engine, "capture_json", lambda args: [{"Name": "purchase-service_data"}])

    assert engine.volume_names() == ["purchase-service_data"]


def test_find_wslc_prefers_explicit_override(monkeypatch):
    monkeypatch.setenv("WSLC_COMPOSE_BIN", "D:\\WSL\\wslc.exe")
    engine.find_wslc.cache_clear()

    assert engine.find_wslc() == "D:\\WSL\\wslc.exe"


def test_find_wslc_prefers_official_fallback_before_path(monkeypatch):
    monkeypatch.delenv("WSLC_COMPOSE_BIN", raising=False)
    monkeypatch.setattr(engine.os.path, "isfile", lambda path: path == engine.WSLC_FALLBACKS[1])
    monkeypatch.setattr(
        engine.shutil,
        "which",
        lambda *args, **kwargs: pytest.fail("PATH must not be searched when the fallback exists"),
    )
    engine.find_wslc.cache_clear()

    assert engine.find_wslc() == engine.WSLC_FALLBACKS[1]


def test_find_wslc_skips_wrapper_and_checks_remaining_path_entries(monkeypatch):
    wrapper = "C:\\tools\\wslc.exe"
    native = "D:\\tools\\wslc.exe"
    monkeypatch.delenv("WSLC_COMPOSE_BIN", raising=False)
    monkeypatch.setattr(engine.os.path, "isfile", lambda path: False)
    monkeypatch.setattr(engine.os, "get_exec_path", lambda: ["C:\\tools", "D:\\tools"])
    monkeypatch.setattr(engine, "_is_self", lambda path: path == wrapper)

    def fake_which(name, path):
        if name == "wslc.exe" and path == "C:\\tools":
            return wrapper
        if name == "wslc.exe" and path == "D:\\tools":
            return native
        return None

    monkeypatch.setattr(engine.shutil, "which", fake_which)
    engine.find_wslc.cache_clear()

    assert engine.find_wslc() == native
