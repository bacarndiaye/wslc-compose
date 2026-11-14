"""Behavioural tests for the runtime modules' less travelled paths.

Every wslc interaction is faked: `subprocess.run`/`Popen`, `engine.run`,
`engine.capture_json` and `engine.inspect` are monkeypatched per test.
"""

import json
import os
import subprocess
import types

import pytest

from wslc_compose import (
    LABEL_CREATE_OPERATION,
    LABEL_ENV_FILE,
    LABEL_HOST_GATEWAY,
    LABEL_PROJECT,
    LABEL_SERVICE,
    engine,
    flags,
    host_gateway,
    metadata,
    networking,
)
from wslc_compose.engine import WslcError
from wslc_compose.model import (
    BuildConfig,
    Network,
    PortMapping,
    Project,
    Service,
    VolumeMount,
)

# Captured at collection time, before conftest replaces `engine.run_options` per test.
REAL_RUN_OPTIONS = engine.run_options
REAL_FIND_WSLC = engine.find_wslc
REAL_NEEDS_PATH_TRANSLATION = engine.needs_path_translation
REAL_TO_HOST_PATH = engine.to_host_path


def _clear_caches():
    for function in (REAL_RUN_OPTIONS, REAL_FIND_WSLC, REAL_NEEDS_PATH_TRANSLATION, REAL_TO_HOST_PATH):
        function.cache_clear()


@pytest.fixture(autouse=True)
def fresh_caches():
    _clear_caches()
    yield
    _clear_caches()


def _done(argv, code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, code, stdout, stderr)


def _fail(*_args, **_kwargs):
    pytest.fail("unexpected call")


def _values(args, flag):
    return [args[i + 1] for i, value in enumerate(args) if value == flag]


def _without_labels(args):
    result, skip = [], False
    for value in args:
        if skip:
            skip = False
        elif value == "-l":
            skip = True
        else:
            result.append(value)
    return result


# --- engine: locating our own wrapper -----------------------------------------


def test_is_self_matches_the_running_script(monkeypatch, tmp_path):
    script = tmp_path / "bin" / "wslc"
    monkeypatch.setattr(engine.sys, "argv", [str(script)])
    monkeypatch.setattr(engine.sys, "executable", str(tmp_path / "python" / "python3"))
    assert engine._is_self(str(script)) is True


def test_is_self_matches_the_interpreter(monkeypatch, tmp_path):
    interpreter = tmp_path / "python" / "wslc.exe"
    monkeypatch.setattr(engine.sys, "argv", [str(tmp_path / "elsewhere" / "cli.py")])
    monkeypatch.setattr(engine.sys, "executable", str(interpreter))
    assert engine._is_self(str(interpreter)) is True


def test_is_self_matches_an_exe_launcher_containing_the_script(monkeypatch, tmp_path):
    # pip/zipapp launchers run `...\Scripts\wslc.exe\__main__.py`: the .exe is ourselves
    launcher = tmp_path / "Scripts" / "wslc.exe"
    monkeypatch.setattr(engine.sys, "argv", [str(launcher / "__main__.py")])
    monkeypatch.setattr(engine.sys, "executable", str(tmp_path / "python" / "python3"))
    assert engine._is_self(str(launcher)) is True


def test_is_self_rejects_a_foreign_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(engine.sys, "argv", [str(tmp_path / "other.exe" / "__main__.py")])
    monkeypatch.setattr(engine.sys, "executable", str(tmp_path / "python" / "python3"))
    assert engine._is_self(str(tmp_path / "WSL" / "wslc.exe")) is False


def test_is_self_treats_unresolvable_paths_as_foreign(monkeypatch):
    def broken(path):
        raise OSError("loop")

    monkeypatch.setattr(engine.os.path, "realpath", broken)
    assert engine._is_self("/usr/bin/wslc") is False


# --- engine: find_wslc ---------------------------------------------------------


def test_find_wslc_override_is_trusted_without_probing(monkeypatch):
    monkeypatch.setenv("WSLC_COMPOSE_BIN", "/opt/custom/wslc")
    monkeypatch.setattr(engine.os.path, "isfile", _fail)
    monkeypatch.setattr(engine.shutil, "which", _fail)
    assert engine.find_wslc() == "/opt/custom/wslc"


def test_find_wslc_uses_first_existing_fallback(monkeypatch, tmp_path):
    present = tmp_path / "wslc.exe"
    present.write_text("")
    monkeypatch.delenv("WSLC_COMPOSE_BIN", raising=False)
    monkeypatch.setattr(engine, "WSLC_FALLBACKS", (str(tmp_path / "missing.exe"), str(present)))
    monkeypatch.setattr(engine.shutil, "which", _fail)
    assert engine.find_wslc() == str(present)


def test_find_wslc_searches_each_path_entry_for_both_names(monkeypatch, tmp_path):
    first, second = str(tmp_path / "a"), str(tmp_path / "b")
    wrapper, native = os.path.join(first, "wslc"), os.path.join(second, "wslc.exe")
    calls = []

    def fake_which(name, path):
        calls.append((path, name))
        return {(first, "wslc"): wrapper, (second, "wslc.exe"): native}.get((path, name))

    monkeypatch.delenv("WSLC_COMPOSE_BIN", raising=False)
    monkeypatch.setattr(engine, "WSLC_FALLBACKS", (str(tmp_path / "missing.exe"),))
    monkeypatch.setattr(engine.os, "get_exec_path", lambda: [first, second])
    monkeypatch.setattr(engine.shutil, "which", fake_which)
    monkeypatch.setattr(engine, "_is_self", lambda path: path == wrapper)

    assert engine.find_wslc() == native
    assert calls == [(first, "wslc.exe"), (first, "wslc"), (second, "wslc.exe")]


def test_find_wslc_reports_a_missing_cli(monkeypatch, tmp_path):
    monkeypatch.delenv("WSLC_COMPOSE_BIN", raising=False)
    monkeypatch.setattr(engine, "WSLC_FALLBACKS", (str(tmp_path / "missing.exe"),))
    monkeypatch.setattr(engine.os, "get_exec_path", lambda: [str(tmp_path)])
    monkeypatch.setattr(engine.shutil, "which", lambda name, path: None)
    with pytest.raises(WslcError, match="wslc CLI not found.*WSLC_COMPOSE_BIN"):
        engine.find_wslc()


# --- engine: WSL detection and path translation --------------------------------


def _uname(release):
    return lambda: types.SimpleNamespace(release=release)


def test_windows_host_is_never_wsl(monkeypatch):
    monkeypatch.setattr(engine.sys, "platform", "win32")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.setattr(engine.os, "uname", _fail, raising=False)
    assert engine._running_in_wsl() is False


def test_wsl_detected_from_distro_variable(monkeypatch):
    monkeypatch.setattr(engine.sys, "platform", "linux")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.setattr(engine.os, "uname", _fail, raising=False)
    assert engine._running_in_wsl() is True


@pytest.mark.parametrize("release, expected", [
    ("6.6.87.2-microsoft-standard-WSL2", True),
    ("4.4.0-19041-Microsoft", True),
    ("6.8.0-45-generic", False),
])
def test_wsl_detected_from_kernel_release(monkeypatch, release, expected):
    monkeypatch.setattr(engine.sys, "platform", "linux")
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.setattr(engine.os, "uname", _uname(release), raising=False)
    assert engine._running_in_wsl() is expected


@pytest.mark.parametrize("in_wsl, binary, expected", [
    (True, "/mnt/c/Program Files/WSL/wslc.EXE", True),
    (True, "/usr/local/bin/wslc", False),
    (False, "C:\\Program Files\\WSL\\wslc.exe", False),
])
def test_path_translation_only_for_windows_cli_called_from_wsl(monkeypatch, in_wsl, binary, expected):
    monkeypatch.setattr(engine, "_running_in_wsl", lambda: in_wsl)
    monkeypatch.setattr(engine, "find_wslc", lambda: binary)
    assert engine.needs_path_translation() is expected


def test_needs_path_translation_does_not_locate_wslc_outside_wsl(monkeypatch):
    monkeypatch.setattr(engine, "_running_in_wsl", lambda: False)
    monkeypatch.setattr(engine, "find_wslc", _fail)
    assert engine.needs_path_translation() is False


def test_to_host_path_is_identity_without_translation(monkeypatch):
    monkeypatch.setattr(engine, "needs_path_translation", lambda: False)
    monkeypatch.setattr(engine.subprocess, "run", _fail)
    assert engine.to_host_path("/home/me/app") == "/home/me/app"


@pytest.mark.parametrize("path", ["C:\\Users\\me", "d:/data", "\\\\wsl.localhost\\Ubuntu\\srv"])
def test_to_host_path_keeps_windows_paths(monkeypatch, path):
    monkeypatch.setattr(engine, "needs_path_translation", lambda: True)
    monkeypatch.setattr(engine.subprocess, "run", _fail)
    assert engine.to_host_path(path) == path


def test_to_host_path_uses_wslpath(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return _done(argv, stdout="\\\\wsl.localhost\\Ubuntu\\home\\me\n")

    monkeypatch.setattr(engine, "needs_path_translation", lambda: True)
    monkeypatch.setattr(engine.subprocess, "run", fake_run)
    assert engine.to_host_path("/home/me") == "\\\\wsl.localhost\\Ubuntu\\home\\me"
    assert calls == [(["wslpath", "-w", "/home/me"], {"capture_output": True, "text": True, "check": True})]


def _raise_oserror(argv, **kwargs):
    raise FileNotFoundError("wslpath")


def _raise_called_process_error(argv, **kwargs):
    raise subprocess.CalledProcessError(1, argv)


@pytest.mark.parametrize("fake_run", [
    _raise_oserror,
    _raise_called_process_error,
    lambda argv, **kwargs: _done(argv, stdout="  \n"),
])
@pytest.mark.parametrize("path, expected", [
    ("/mnt/c/Users/me/app", "C:\\Users\\me\\app"),
    ("/mnt/d", "D:\\"),
])
def test_to_host_path_falls_back_to_drive_mounts(monkeypatch, capsys, fake_run, path, expected):
    monkeypatch.setattr(engine, "needs_path_translation", lambda: True)
    monkeypatch.setattr(engine.subprocess, "run", fake_run)
    assert engine.to_host_path(path) == expected
    assert capsys.readouterr().err == ""


def test_to_host_path_warns_when_untranslatable(monkeypatch, capsys):
    monkeypatch.setattr(engine, "needs_path_translation", lambda: True)
    monkeypatch.setattr(engine.subprocess, "run", _raise_oserror)
    assert engine.to_host_path("/home/me") == "/home/me"
    assert capsys.readouterr().err == (
        "wslc-compose: warning: cannot translate path '/home/me' for wslc.exe; passing as-is\n")


# --- engine: running wslc -------------------------------------------------------


def test_run_dry_run_prints_without_executing(monkeypatch, capsys):
    monkeypatch.setattr(engine, "find_wslc", lambda: "wslc")
    monkeypatch.setattr(engine.subprocess, "run", _fail)
    proc = engine.run(["rm", "-f", "web"], dry_run=True)
    assert (proc.args, proc.returncode) == (["wslc", "rm", "-f", "web"], 0)
    assert capsys.readouterr().out == "+ wslc rm -f web\n"


def test_run_failure_includes_captured_stderr(monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "find_wslc", lambda: "wslc")
    monkeypatch.setattr(engine.subprocess, "run",
                        lambda argv, **kw: calls.append(kw) or _done(argv, 3, "", "  no such container\n"))
    with pytest.raises(WslcError) as info:
        engine.run(["rm", "web", "extra"], capture=True)
    assert str(info.value) == "wslc rm web failed (exit 3): no such container"
    assert calls[0]["text"] is True and calls[0]["capture_output"] is True


@pytest.mark.parametrize("capture, stderr", [(False, None), (True, "")])
def test_run_failure_without_detail(monkeypatch, capture, stderr):
    calls = []
    monkeypatch.setattr(engine, "find_wslc", lambda: "wslc")
    monkeypatch.setattr(engine.subprocess, "run",
                        lambda argv, **kw: calls.append(kw) or _done(argv, 2, None, stderr))
    with pytest.raises(WslcError) as info:
        engine.run(["start", "web"], capture=capture)
    assert str(info.value) == "wslc start web failed (exit 2)"
    if not capture:
        assert calls[0]["encoding"] is None and calls[0]["errors"] is None


def test_run_without_check_returns_the_failed_process(monkeypatch):
    monkeypatch.setattr(engine, "find_wslc", lambda: "wslc")
    monkeypatch.setattr(engine.subprocess, "run", lambda argv, **kw: _done(argv, 1, "", "boom"))
    proc = engine.run(["inspect", "x"], capture=True, check=False)
    assert (proc.returncode, proc.stderr) == (1, "boom")


def test_run_retried_always_makes_at_least_one_attempt(monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "run", lambda args, **_: calls.append(args) or "ok")
    assert engine.run_retried(["start", "web"], retries=0) == "ok"
    assert calls == [["start", "web"]]

    def transient(args, **_):
        calls.append(args)
        raise WslcError("ERROR_SHARING_VIOLATION")

    calls.clear()
    monkeypatch.setattr(engine, "run", transient)
    with pytest.raises(WslcError):
        engine.run_retried(["start", "web"], retries=0, delay=0)
    assert len(calls) == 1


def test_run_retried_reports_progress_on_stderr(monkeypatch, capsys):
    attempts, sleeps = [], []

    def flaky(args, capture=False, check=True, dry_run=False):
        attempts.append(dry_run)
        if len(attempts) < 3:
            raise WslcError("ERROR_SHARING_VIOLATION")
        return _done(args)

    monkeypatch.setattr(engine, "run", flaky)
    monkeypatch.setattr(engine.time, "sleep", sleeps.append)
    assert engine.run_retried(["start", "web"], dry_run=True, retries=3, delay=0.5).returncode == 0
    assert attempts == [True, True, True] and sleeps == [0.5, 0.5]
    assert "retrying in 0.5s (2/2)" in capsys.readouterr().err


@pytest.mark.parametrize("kwargs, expected", [
    ({"text": True}, {"text": True, "encoding": "utf-8", "errors": "replace"}),
    ({"universal_newlines": True}, {"universal_newlines": True, "encoding": "utf-8", "errors": "replace"}),
    ({"text": True, "encoding": "cp1252"}, {"text": True, "encoding": "cp1252", "errors": "replace"}),
    ({"stdout": -1}, {"stdout": -1}),
])
def test_popen_decodes_text_streams_as_utf8(monkeypatch, kwargs, expected):
    calls = []
    monkeypatch.setattr(engine, "find_wslc", lambda: "wslc")
    monkeypatch.setattr(engine.subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw)) or "proc")
    assert engine.popen(["logs", "-f", "web"], **kwargs) == "proc"
    assert calls == [(["wslc", "logs", "-f", "web"], expected)]


def test_popen_reports_unstartable_binary(monkeypatch):
    def broken(argv, **kw):
        raise PermissionError("denied")

    monkeypatch.setattr(engine, "find_wslc", lambda: "wslc")
    monkeypatch.setattr(engine.subprocess, "Popen", broken)
    with pytest.raises(WslcError, match="cannot run wslc: denied"):
        engine.popen(["logs", "web"])


def test_session_script_dry_run_hides_the_rules(monkeypatch, capsys):
    monkeypatch.setattr(engine, "find_wslc", lambda: "wslc")
    monkeypatch.setattr(engine.subprocess, "run", _fail)
    result = engine.run_session_script("iptables -L", dry_run=True)
    assert result.args == ["wslc", "system", "session", "run", "sh", "-c", "iptables -L"]
    assert result.returncode == 0
    assert capsys.readouterr().out == "+ wslc system session run sh -c <project host-gateway compatibility rules>\n"


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("wslc", 30), OSError("gone")])
def test_session_script_timeout_or_launch_failure(monkeypatch, error):
    def broken(argv, **kw):
        raise error

    monkeypatch.setattr(engine, "find_wslc", lambda: "wslc")
    monkeypatch.setattr(engine.subprocess, "run", broken)
    with pytest.raises(WslcError, match="failed or timed out") as info:
        engine.run_session_script("true")
    assert info.value.__cause__ is error


@pytest.mark.parametrize("stdout, stderr, detail", [
    ("ignored", " flock: timeout \n", "flock: timeout"),
    (" iptables: missing \n", "", "iptables: missing"),
])
def test_session_script_failure_prefers_command_output(monkeypatch, stdout, stderr, detail):
    monkeypatch.setattr(engine, "find_wslc", lambda: "wslc")
    monkeypatch.setattr(engine.subprocess, "run", lambda argv, **kw: _done(argv, 71, stdout, stderr))
    with pytest.raises(WslcError) as info:
        engine.run_session_script("true")
    assert str(info.value) == "host-gateway compatibility failed: " + detail


# --- engine: JSON queries ----------------------------------------------------------


def _stdout(monkeypatch, text, calls=None):
    def fake_run(args, capture=False):
        if calls is not None:
            calls.append((args, capture))
        return _done(args, stdout=text)

    monkeypatch.setattr(engine, "run", fake_run)


def test_capture_json_empty_output(monkeypatch):
    calls = []
    _stdout(monkeypatch, " \n", calls)
    assert engine.capture_json(["list"]) == []
    assert calls == [(["list"], True)]


def test_capture_json_rejects_non_json(monkeypatch):
    _stdout(monkeypatch, "ID  NAME\nabc web")
    with pytest.raises(WslcError, match="unexpected non-JSON output from wslc list --format json"):
        engine.capture_json(["list", "--format", "json"])


def test_run_options_parses_help_from_both_streams(monkeypatch):
    calls = []

    def fake_run(args, capture=False, check=True):
        calls.append((args, capture, check))
        return _done(args, stdout="Usage: wslc run [OPTIONS]\n  --restart string\n  --cap-add list\n  -d",
                     stderr="  --health-start-period duration\n  --Bad")

    monkeypatch.setattr(engine, "run", fake_run)
    assert REAL_RUN_OPTIONS() == frozenset({"--restart", "--cap-add", "--health-start-period"})
    assert calls == [(["run", "--help"], True, False)]


@pytest.mark.parametrize("error", [WslcError("wslc CLI not found"), OSError("gone")])
def test_run_options_empty_when_wslc_unavailable(monkeypatch, error):
    def broken(*args, **kwargs):
        raise error

    monkeypatch.setattr(engine, "run", broken)
    assert REAL_RUN_OPTIONS() == frozenset()


def test_run_options_tolerates_missing_streams(monkeypatch):
    monkeypatch.setattr(engine, "run", lambda args, **kw: _done(args, 0, None, None))
    assert REAL_RUN_OPTIONS() == frozenset()


def test_inspect_returns_first_record(monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "capture_json", lambda args: calls.append(args) or [{"Id": "a"}, {"Id": "b"}])
    assert engine.inspect("web") == {"Id": "a"}
    assert calls == [["inspect", "web"]]


def test_inspect_missing_object(monkeypatch):
    monkeypatch.setattr(engine, "capture_json", lambda args: [])
    assert engine.inspect("web") is None


def test_inspect_failure_means_absent(monkeypatch):
    def broken(args):
        raise WslcError("not found")

    monkeypatch.setattr(engine, "capture_json", broken)
    assert engine.inspect("web") is None


def test_inspect_many_drops_absent_objects(monkeypatch):
    records = {"a": {"Id": "a"}, "b": None, "c": {"Id": "c"}}
    monkeypatch.setattr(engine, "inspect", records.get)
    assert engine.inspect_many(["a", "b", "c"]) == {"a": {"Id": "a"}, "c": {"Id": "c"}}


def test_names_from_json_skips_unnamed_entries(monkeypatch):
    monkeypatch.setattr(engine, "capture_json", lambda args: [{"Name": "a"}, {"Name": ""}, {"ID": "x"}])
    assert engine.network_names() == ["a"]


@pytest.mark.parametrize("images, expected", [
    ([{"Config": {"Entrypoint": ["/entry.sh", "-x"]}}], ["/entry.sh", "-x"]),
    ([{"Config": {"Entrypoint": None}}], []),
    ([{"Config": None}], []),
    ([{}], []),
    ([], None),
    (["not-an-object"], None),
])
def test_image_entrypoint(monkeypatch, images, expected):
    calls = []
    monkeypatch.setattr(engine, "capture_json", lambda args: calls.append(args) or images)
    assert engine.image_entrypoint("app:1") == expected
    assert calls == [["image", "inspect", "app:1"]]


def test_image_entrypoint_of_missing_image(monkeypatch):
    def broken(args):
        raise WslcError("no such image")

    monkeypatch.setattr(engine, "capture_json", broken)
    assert engine.image_entrypoint("app:1") is None


def test_image_exists_false_when_listing_fails(monkeypatch):
    def broken(args):
        raise WslcError("daemon down")

    monkeypatch.setattr(engine, "capture_json", broken)
    assert engine.image_exists("nginx") is False


@pytest.mark.parametrize("name, expected", [
    ("app@sha256:abc", ("app", "latest")),
    ("registry:5000/team/app:2@sha256:abc", ("registry:5000/team/app", "2")),
    ("index.docker.io/library/redis", ("index.docker.io/library/redis", "latest")),
])
def test_split_image_name(name, expected):
    assert engine.split_image_name(name) == expected


@pytest.mark.parametrize("repo, short", [
    ("docker.io/bitnami/redis", "bitnami/redis"),
    ("index.docker.io/library/redis", "redis"),
    ("library/redis", "redis"),
    ("ghcr.io/library/redis", "ghcr.io/library/redis"),
])
def test_short_repository(repo, short):
    assert engine._short_repository(repo) == short


def test_ensure_network_keeps_existing_network(monkeypatch):
    monkeypatch.setattr(engine, "network_names", lambda: ["front"])
    monkeypatch.setattr(engine, "run", _fail)
    assert engine.ensure_network("front", {"a": "b"}, internal=True, driver="bridge") is False


def test_ensure_network_with_every_setting(monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "network_names", lambda: [])
    monkeypatch.setattr(engine, "run", lambda args, dry_run=False: calls.append((args, dry_run)))
    assert engine.ensure_network("front", {"p": "demo"}, internal=True, dry_run=True, driver="bridge",
                                 driver_opts={"mtu": "1400"}, gateway="10.1.0.1") is True
    assert calls == [(["network", "create", "-l", "p=demo", "--internal", "--driver", "bridge",
                       "--opt", "mtu=1400", "--gateway", "10.1.0.1", "front"], True)]


def test_ensure_volume_keeps_existing_volume(monkeypatch):
    monkeypatch.setattr(engine, "volume_names", lambda: ["data"])
    monkeypatch.setattr(engine, "run", _fail)
    assert engine.ensure_volume("data", {"a": "b"}, driver="local") is False


def test_ensure_volume_with_every_setting(monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "volume_names", lambda: [])
    monkeypatch.setattr(engine, "run", lambda args, dry_run=False: calls.append((args, dry_run)))
    assert engine.ensure_volume("data", {"p": "demo"}, driver="local", driver_opts={"o": "bind"},
                                dry_run=True) is True
    assert calls == [(["volume", "create", "--driver", "local", "--opt", "o=bind", "-l", "p=demo", "data"], True)]


# --- flags -------------------------------------------------------------------------


def test_run_args_maps_every_remaining_service_setting():
    service = Service(
        name="web", image="img:1", labels={"tier": "front"},
        extra_hosts=["host.docker.internal: Host-Gateway "],
        volumes=[
            VolumeMount("tmpfs", None, "/cache", options=["tmpfs-size=64m"], structured=True),
            VolumeMount("bind", "/src", "/app", structured=True),
            VolumeMount("volume", "data", "/data", structured=True),
            VolumeMount("tmpfs", None, "/run"),
        ],
        tmpfs=["/tmp"], hostname="web01", domainname="example.org", dns=["1.1.1.1"],
        dns_search=["corp.local"], dns_opt=["ndots:2"], shm_size="64m",
        ulimits=["nofile=1024:2048"], stop_signal="SIGINT",
    )
    project = Project(name="demo", directory="/p", services={"web": service})
    args = flags.run_args(project, service, path_mapper=lambda p: "M:" + p)

    labels = _values(args, "-l")
    assert labels[0] == "tier=front"
    assert labels[-1] == f"{LABEL_HOST_GATEWAY}=v1"
    assert _without_labels(args) == [
        "run", "-d", "--name", "demo-web-1",
        "--mount", "type=tmpfs,target=/cache,tmpfs-size=64m",
        "--mount", "type=bind,source=M:/src,target=/app",
        "--mount", "type=volume,source=data,target=/data",
        "--tmpfs", "/run",
        "--tmpfs", "/tmp",
        "-h", "web01", "--domainname", "example.org", "--dns", "1.1.1.1",
        "--dns-search", "corp.local", "--dns-option", "ndots:2",
        "--shm-size", "64m", "--ulimit", "nofile=1024:2048", "--stop-signal", "SIGINT",
        "img:1",
    ]


def test_run_args_without_host_gateway_has_no_compat_label():
    service = Service(name="web", image="img", extra_hosts=["db:10.0.0.3"])
    args = flags.run_args(Project(name="demo", directory="."), service)
    assert not any(label.startswith(LABEL_HOST_GATEWAY) for label in _values(args, "-l"))


def test_network_connect_args_without_static_address():
    service = Service(name="web", networks=["front", "back"], network_aliases={"back": ["api", "api"]})
    assert flags.network_connect_args(service, "back", "cid") == [
        "network", "connect", "--network-alias", "api", "--network-alias", "web", "back", "cid"]


def test_build_args_minimal_with_pull():
    service = Service(name="job", build=BuildConfig(context="/ctx", pull=True))
    project = Project(name="demo", directory="/p")
    assert flags.build_args(project, service, path_mapper=lambda p: "M:" + p) == [
        "build", "-t", "demo-job", "--pull", "M:/ctx"]


def test_build_args_keeps_absolute_dockerfile_outside_context(tmp_path):
    dockerfile = str(tmp_path / "docker" / "Dockerfile.prod")
    service = Service(name="job", image="team/job:2",
                      build=BuildConfig(context=str(tmp_path / "ctx"), dockerfile=dockerfile, no_cache=True))
    assert flags.build_args(Project(name="demo", directory="."), service) == [
        "build", "-t", "team/job:2", "-f", dockerfile, "--no-cache", str(tmp_path / "ctx")]


# --- host_gateway ------------------------------------------------------------------


def _gateway_project(protocol="tcp", published="1521"):
    client = Service(name="keycloak", networks=["net"], extra_hosts=["host.docker.internal:host-gateway"])
    server = Service(name="oracle", networks=["net"], ports=[PortMapping(1521, published, protocol)])
    return Project(name="demo", directory=".", services={"keycloak": client, "oracle": server})


def _server(**networks):
    return {"Id": "server-id", "Config": {"Labels": {LABEL_PROJECT: "demo", LABEL_SERVICE: "oracle"}},
            "State": {"Running": True}, "HostConfig": {"NetworkMode": "net"},
            "NetworkSettings": {"Networks": networks or {"net": {"Gateway": "172.18.0.1", "IPAddress": "172.18.0.3"}}},
            "Ports": {"1521/tcp": [{"HostPort": "1521"}], "1521/udp": [{"HostPort": "1521"}]}}


def test_gateways_only_come_from_client_networks():
    data = _server(net={"IPAddress": "172.18.0.3"},
                   isolated={"Gateway": "10.9.0.1", "IPAddress": "10.9.0.3"})
    other = _server(net={"Gateway": "172.18.0.1", "IPAddress": "172.18.0.4"})
    other["Config"]["Labels"][LABEL_SERVICE] = "keycloak"
    assert host_gateway.mappings(_gateway_project(), [data, other]) == [
        ("172.18.0.1", "tcp", 1521, "172.18.0.3", 1521)]


def test_containers_without_an_address_are_skipped():
    detached = _server()
    detached["Config"]["Labels"][LABEL_SERVICE] = "oracle"
    detached["NetworkSettings"]["Networks"] = {}
    detached["HostConfig"] = {}
    assert host_gateway.mappings(_gateway_project(), [_server(), detached]) == [
        ("172.18.0.1", "tcp", 1521, "172.18.0.3", 1521)]


@pytest.mark.parametrize("published", ["0.0.0.0:1521", ":1521", None])
def test_all_interface_publications_are_forwarded(published):
    assert host_gateway.mappings(_gateway_project(published=published), [_server()]) == [
        ("172.18.0.1", "tcp", 1521, "172.18.0.3", 1521)]


def test_udp_publications_are_forwarded():
    assert host_gateway.mappings(_gateway_project("UDP"), [_server()]) == [
        ("172.18.0.1", "udp", 1521, "172.18.0.3", 1521)]


def test_sctp_publications_are_rejected():
    with pytest.raises(WslcError, match="TCP/UDP IPv4 publications only"):
        host_gateway.mappings(_gateway_project("sctp"), [_server()])


@pytest.mark.parametrize("key", ["ID", "Id", "Names", "Name"])
def test_reconcile_inspects_every_project_container(monkeypatch, key):
    listed, inspected, sessions = [], [], []
    monkeypatch.setattr(engine, "list_project_containers",
                        lambda name, all_states=False: listed.append((name, all_states)) or [{key: "server-id"}])
    monkeypatch.setattr(engine, "inspect", lambda ident: inspected.append(ident) or _server())
    monkeypatch.setattr(engine, "run_session_script",
                        lambda commands, dry_run=False: sessions.append((commands, dry_run)))
    host_gateway.reconcile(_gateway_project(), dry_run=True)
    assert listed == [("demo", True)]
    assert inspected == ["server-id"]
    assert sessions == [(host_gateway.script("demo", [("172.18.0.1", "tcp", 1521, "172.18.0.3", 1521)]), True)]


def test_reconcile_refuses_when_inventory_cannot_be_inspected(monkeypatch):
    monkeypatch.setattr(engine, "list_project_containers", lambda name, all_states=False: [{"ID": "gone"}])
    monkeypatch.setattr(engine, "inspect", lambda ident: None)
    monkeypatch.setattr(engine, "run_session_script", _fail)
    with pytest.raises(WslcError, match="cannot verify host-gateway publication inventory"):
        host_gateway.reconcile(_gateway_project())


def test_reconcile_with_no_containers_installs_empty_chains(monkeypatch):
    sessions = []
    monkeypatch.setattr(engine, "list_project_containers", lambda name, all_states=False: [])
    monkeypatch.setattr(engine, "inspect", _fail)
    monkeypatch.setattr(engine, "run_session_script", lambda commands, dry_run=False: sessions.append(commands))
    host_gateway.reconcile(_gateway_project())
    assert sessions == [host_gateway.script("demo", [])]


# --- networking --------------------------------------------------------------------


@pytest.mark.parametrize("records", [[], [{"Name": "net"}, {"Name": "net"}], ["net"], [{"Name": "other"}]])
def test_inspect_network_requires_one_matching_record(monkeypatch, records):
    monkeypatch.setattr(engine, "capture_json", lambda args: records)
    with pytest.raises(WslcError, match="'net': inspect did not return a usable network identity"):
        networking.inspect_network("net")


def test_inspect_network_returns_the_record(monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "capture_json", lambda args: calls.append(args) or [{"Name": "net", "Driver": "bridge"}])
    assert networking.inspect_network("net") == {"Name": "net", "Driver": "bridge"}
    assert calls == [["network", "inspect", "net"]]


def test_verify_network_without_ipam_settings(monkeypatch):
    actual = {"Name": "net", "Driver": "bridge", "Internal": True, "Options": {"mtu": "1400"}}
    monkeypatch.setattr(engine, "capture_json", lambda args: [actual])
    # external networks are not required to match `internal` unless it is requested
    assert networking.verify_network(Network(key="n", name="net", external=True, driver="bridge",
                                             driver_opts={"mtu": "1400"})) == actual


def test_verify_network_reports_internal_mismatch(monkeypatch):
    monkeypatch.setattr(engine, "capture_json", lambda args: [{"Name": "net", "Internal": True}])
    with pytest.raises(WslcError, match="conflicts with the requested settings: internal;"):
        networking.verify_network(Network(key="n", name="net"))


def test_static_address_skips_malformed_ipam_records():
    service = Service(name="db", network_ipv4_addresses={"back": "10.2.0.20"})
    networks = {"back": {"IPAM": {"Config": [{"Gateway": "10.2.0.1"}, {"Subnet": "bogus"},
                                             {"Subnet": "10.2.0.0/24", "Gateway": "10.2.0.1"}]}}}
    networking.validate_static_addresses(service, networks)


def test_static_address_unusable_when_only_malformed_records():
    service = Service(name="db", network_ipv4_addresses={"back": "10.2.0.20"})
    networks = {"back": {"IPAM": {"Config": [{"Gateway": "10.2.0.1"}, {"Subnet": "bogus"}]}}}
    with pytest.raises(WslcError, match="static IPv4 10.2.0.20 is not usable in network 'back'"):
        networking.validate_static_addresses(service, networks)


@pytest.mark.parametrize("service", [
    Service(name="web", networks=["front", "back"]),
    Service(name="web", networks=["front"], network_ipv4_addresses={"front": "10.1.0.10"}),
])
def test_unobservable_endpoints_cannot_be_verified(service):
    current = {"name": "demo-web-1", "running": True, "network_mode": None, "networks": None}
    with pytest.raises(WslcError, match="cannot verify network settings for 'demo-web-1'"):
        networking.configuration_matches(service, current)


def test_single_dynamic_network_needs_no_endpoint_details():
    current = {"name": "demo-web-1", "running": True, "network_mode": "front", "networks": None}
    assert networking.configuration_matches(Service(name="web", networks=["front"]), current) is True


@pytest.mark.parametrize("endpoint", [{"Aliases": "web"}, {"Aliases": ["web"], "IPAMConfig": ["10.1.0.10"]}])
def test_malformed_endpoint_cannot_be_verified(endpoint):
    current = {"name": "demo-web-1", "running": True, "network_mode": "front", "networks": {"front": endpoint}}
    with pytest.raises(WslcError, match="cannot verify endpoint settings for network 'front'"):
        networking.configuration_matches(Service(name="web", networks=["front"]), current)


class FakeEngine:
    """create succeeds and records ownership labels; start fails; inspect replays `records`."""

    def __init__(self, monkeypatch, start_error=None):
        self.calls = []
        self.labels = None
        self.inspections = 0
        self.start_error = start_error
        self.ids = ["first-id"]
        monkeypatch.setattr(engine, "run", self.run)
        monkeypatch.setattr(engine, "run_retried", self.start)
        monkeypatch.setattr(engine, "inspect", self.inspect)

    def run(self, args, **kwargs):
        self.calls.append(args)
        if args[0] == "create":
            self.labels = dict(args[i + 1].split("=", 1) for i, value in enumerate(args) if value == "-l")
        return _done(args)

    def start(self, args, **kwargs):
        self.calls.append(args)
        if self.start_error:
            raise self.start_error
        return _done(args)

    def inspect(self, name):
        identifier = self.ids[min(self.inspections, len(self.ids) - 1)]
        self.inspections += 1
        return {"Id": identifier, "Config": {"Labels": self.labels}}


def test_partial_container_replaced_concurrently_is_not_removed(monkeypatch):
    failure = WslcError("start failed")
    fake = FakeEngine(monkeypatch, failure)
    fake.ids = ["first-id", "second-id"]
    service = Service(name="web", image="img", networks=["front"])
    with pytest.raises(WslcError, match="partial container 'demo-web-1' changed identity") as info:
        networking.create_and_start(Project(name="demo", directory="."), service, 1, False)
    assert info.value.__cause__ is failure
    assert fake.calls[-1] == ["start", "first-id"]
    assert fake.labels[LABEL_CREATE_OPERATION]


def test_failed_dry_run_is_not_cleaned_up(monkeypatch):
    fake = FakeEngine(monkeypatch, WslcError("start failed"))
    monkeypatch.setattr(engine, "inspect", _fail)
    service = Service(name="web", image="img", networks=["front"])
    with pytest.raises(WslcError, match="start failed"):
        networking.create_and_start(Project(name="demo", directory="."), service, 1, True)
    assert [args[0] for args in fake.calls] == ["create", "start"]
    assert fake.calls[-1] == ["start", "demo-web-1"]


def test_single_network_create_and_start_skips_endpoint_verification(monkeypatch):
    fake = FakeEngine(monkeypatch)
    service = Service(name="web", image="img", networks=["front"])
    networking.create_and_start(Project(name="demo", directory="."), service, 1, False)
    assert fake.inspections == 1  # ownership only
    assert fake.calls[-1] == ["start", "first-id"]


# --- metadata and model ------------------------------------------------------------


def test_project_env_file_is_recorded_on_containers_and_resources():
    project = Project(name="demo", directory="/p", compose_files=["/p/compose.yaml"], env_file="/p/.env.prod")
    service = Service(name="web", image="img")
    assert metadata.container_labels(project, service, 2)[LABEL_ENV_FILE] == "/p/.env.prod"
    labels = metadata.resource_labels(project, "volume", "data", "demo_data")
    assert labels[LABEL_ENV_FILE] == "/p/.env.prod"
    assert json.loads(labels["com.wslc-compose.project.config_files"]) == ["/p/compose.yaml"]


def test_project_without_env_file_has_no_env_file_label():
    labels = metadata.container_labels(Project(name="demo", directory="/p"), Service(name="web"), 1)
    assert LABEL_ENV_FILE not in labels


def test_explicit_container_name_ignores_index():
    service = Service(name="web", container_name="my-web")
    assert Project(name="demo", directory=".").container_name(service, 3) == "my-web"


def test_selection_ignores_undefined_services_and_shared_dependencies():
    services = {
        "web": Service(name="web", depends_on=["db", "cache"]),
        "worker": Service(name="worker", depends_on=["db", "ghost"]),
        "db": Service(name="db"),
        "cache": Service(name="cache"),
        "unrelated": Service(name="unrelated"),
    }
    project = Project(name="demo", directory=".", services=services)
    order = [service.name for service in project.sorted_services(["web", "worker", "db", "missing"])]
    assert order == ["cache", "db", "web", "worker"]

