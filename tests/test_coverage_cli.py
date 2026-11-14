"""Coverage-driven behaviour tests for cli.py, shim.py and __main__.py.

Every external effect (wslc, subprocess, project loading) is faked; the tests
assert the exact wslc argv, outputs and exit codes.
"""

import importlib
import os
import runpy
import signal
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import yaml

from wslc_compose import LABEL_PROJECT, __version__, cli, engine, shim
from wslc_compose.engine import WslcError
from wslc_compose.loader import ComposeError
from wslc_compose.model import (
    BuildConfig,
    Network,
    Project,
    Service,
    Volume,
    VolumeMount,
)

# --- project loading -----------------------------------------------------------


def test_compose_file_is_found_in_a_parent_directory(tmp_path, monkeypatch):
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}\n")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.delenv("COMPOSE_FILE", raising=False)
    monkeypatch.chdir(nested)

    found = cli._locate_compose_files(None)

    assert len(found) == 1
    assert os.path.samefile(found[0], compose)


def test_missing_compose_file_error_after_reaching_the_root(tmp_path, monkeypatch):
    visited = []
    monkeypatch.delenv("COMPOSE_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "find_compose_file", lambda d: visited.append(d) or None)

    with pytest.raises(ComposeError, match="no compose file found"):
        cli._locate_compose_files([])

    assert visited[0] == os.getcwd()
    assert os.path.dirname(visited[-1]) == visited[-1]  # stopped at the filesystem root
    assert len(visited) == len(set(visited))


def test_load_passes_options_and_prints_project_warnings(tmp_path, monkeypatch, capsys):
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}\n")
    calls = []
    project = Project(name="demo", directory=str(tmp_path), warnings=["first", "second"])

    def fake_load_project(files, project_name=None, env_file=None):
        calls.append((files, project_name, env_file))
        return project

    monkeypatch.setattr(cli, "load_project", fake_load_project)
    ns = cli.build_parser().parse_args(
        ["-f", str(compose), "-p", "demo", "--env-file", "x.env", "config"])

    assert cli._load(ns) is project
    assert calls == [([str(compose)], "demo", "x.env")]
    assert capsys.readouterr().err.splitlines() == [
        "wslc-compose: warning: first", "wslc-compose: warning: second"]


def test_normalized_caps_strips_prefix_and_deduplicates():
    assert cli._normalized_caps(["net_admin", "CAP_NET_ADMIN", "mknod"]) == ["NET_ADMIN", "MKNOD"]
    assert cli._normalized_caps(None) == []


def test_select_services_rejects_unknown_names():
    project = Project(name="demo", directory=".", services={"web": Service(name="web", image="x")})
    with pytest.raises(ComposeError, match="no such service: nope"):
        cli._select_services(project, ["nope"], [])


def test_select_services_honours_profiles():
    web = Service(name="web", image="x")
    debug = Service(name="debug", image="x", profiles=["dev"])
    project = Project(name="demo", directory=".", services={"web": web, "debug": debug})

    assert [s.name for s in cli._select_services(project, [], [])] == ["web"]
    assert sorted(s.name for s in cli._select_services(project, [], ["dev"])) == ["debug", "web"]
    # naming a service explicitly enables it regardless of its profiles
    assert [s.name for s in cli._select_services(project, ["debug"], [])] == ["debug"]


# --- container queries -----------------------------------------------------------


def test_project_containers_skips_anonymous_entries_and_rebuilds_command(monkeypatch):
    monkeypatch.setattr(cli.engine, "list_project_containers", lambda name, all_states=False: [
        {"Status": "weird"},  # neither id nor name: ignored
        {"Name": "demo-web-1", "CreatedAt": "not a date"},
    ])
    inspected = []

    def fake_inspect(identifier):
        inspected.append(identifier)
        return {
            "Name": "ignored", "Image": "nginx",
            "Config": {"Entrypoint": ["/docker-entrypoint.sh"], "Cmd": ["nginx", "-g"]},
            "Labels": {"com.wslc-compose.service": "web", "com.wslc-compose.container-number": "2"},
            "State": {"Status": "exited"},
            "HostConfig": {"RestartPolicy": {"Name": "always"}, "CapAdd": ["NET_ADMIN"]},
        }

    monkeypatch.setattr(cli.engine, "inspect", fake_inspect)

    [entry] = cli._project_containers(Project(name="demo", directory="."))

    assert inspected == ["demo-web-1"]
    assert entry["command"] == "/docker-entrypoint.sh nginx -g"
    assert entry["name"] == "demo-web-1"
    assert entry["image"] == "nginx"
    assert entry["index"] == 2
    assert entry["running"] is False
    assert entry["status"] == "exited"
    assert entry["created"] == "not a date"
    assert entry["restart_policy"] == "always"
    assert entry["cap_add"] == ["NET_ADMIN"]


def test_format_ports_from_structured_list():
    ports = [
        {"Protocol": 6, "HostPort": 8080, "ContainerPort": 80},
        {"Protocol": 17, "BindingAddress": "127.0.0.1", "HostPort": 53, "ContainerPort": 53},
        {"Protocol": "sctp", "HostPort": 1, "ContainerPort": 2},
    ]
    assert cli._format_ports(ports) == (
        "0.0.0.0:8080->80/tcp, 127.0.0.1:53->53/udp, 0.0.0.0:1->2/sctp")
    assert cli._format_ports([]) == ""


NOW = datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)


def _ago(**delta):
    return (NOW - timedelta(**delta)).isoformat().replace("+00:00", "Z")


@pytest.mark.parametrize("value, expected", [
    # docker/go-units HumanDuration, as printed by `docker compose ps`
    (_ago(seconds=0), "Less than a second ago"),
    (_ago(seconds=1), "1 second ago"),
    (_ago(seconds=30), "30 seconds ago"),
    (_ago(seconds=60), "About a minute ago"),
    (_ago(seconds=119), "About a minute ago"),
    (_ago(minutes=5), "5 minutes ago"),
    (_ago(minutes=50), "50 minutes ago"),
    (_ago(minutes=70), "About an hour ago"),
    (_ago(minutes=100), "2 hours ago"),
    (_ago(hours=3), "3 hours ago"),
    (_ago(hours=30), "30 hours ago"),
    (_ago(hours=47), "47 hours ago"),
    (_ago(days=3), "3 days ago"),
    (_ago(days=13), "13 days ago"),
    (_ago(days=14), "2 weeks ago"),
    (_ago(days=59), "8 weeks ago"),
    (_ago(days=60), "2 months ago"),
    (_ago(days=729), "24 months ago"),
    (_ago(days=730), "2 years ago"),
])
def test_format_created_buckets(value, expected):
    assert cli._format_created(value, NOW) == expected


@pytest.mark.parametrize("value", ["", "garbage", "2026-13-45 99:99:99 +0000 UTC", "2026-10-23Tnope"])
def test_format_created_returns_unparseable_values_unchanged(value):
    assert cli._format_created(value, NOW) == value


@pytest.mark.parametrize("value", [_ago(seconds=100), _ago(minutes=100), _ago(hours=44)])
def test_format_created_never_says_one_plural_unit(value):
    assert not cli._format_created(value, NOW).startswith("1 ")


# --- up --------------------------------------------------------------------------


def _up_ns(*extra):
    return cli.build_parser().parse_args(["up", *extra])


def _fake_engine(monkeypatch, project, containers=()):
    """Record every wslc call made by `up` and friends."""
    calls = []
    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(cli, "_project_containers", lambda *_, **__: [dict(c) for c in containers])
    monkeypatch.setattr(cli.engine, "run", lambda args, **kw: calls.append(("run", args, kw)))
    monkeypatch.setattr(cli.engine, "run_retried", lambda args, **kw: calls.append(("retried", args, kw)))
    monkeypatch.setattr(cli.engine, "to_host_path", lambda path: path)
    monkeypatch.setattr(cli.host_gateway, "reconcile", lambda *a, **k: None)
    monkeypatch.setattr(cli.networking, "verify_container", lambda *a, **k: None)
    return calls


def test_up_with_nothing_selected_fails(monkeypatch, capsys):
    project = Project(name="demo", directory=".",
                      services={"dbg": Service(name="dbg", image="x", profiles=["dev"])})
    monkeypatch.setattr(cli, "_load", lambda _: project)

    assert cli.cmd_up(_up_ns("-d")) == 1
    assert capsys.readouterr().err == "wslc-compose: no services to start\n"


def test_up_prints_runtime_option_warnings(monkeypatch, capsys):
    project = Project(name="demo", directory=".",
                      services={"web": Service(name="web", image="x", restart="always")})
    _fake_engine(monkeypatch, project)

    assert cli.cmd_up(_up_ns("-d")) == 0
    assert "wslc-compose: warning: web: 'restart: always' needs wslc >= 3.0.5" in capsys.readouterr().err


@pytest.mark.parametrize("spec", ["web", "web=two", "web=-1"])
def test_up_rejects_malformed_scale(monkeypatch, spec):
    project = Project(name="demo", directory=".", services={"web": Service(name="web", image="x")})
    monkeypatch.setattr(cli, "_load", lambda _: project)
    with pytest.raises(ComposeError, match="invalid --scale value"):
        cli.cmd_up(_up_ns("-d", "--scale", spec))


def test_up_scale_zero_skips_static_address_reservation(monkeypatch):
    # Two services ask for the same static IP, but one is scaled to 0: no conflict.
    a = Service(name="a", image="x", networks=["demo_front"], network_ipv4_addresses={"demo_front": "10.0.0.5"})
    b = Service(name="b", image="x", networks=["demo_front"], network_ipv4_addresses={"demo_front": "10.0.0.5"})
    project = Project(name="demo", directory=".", services={"a": a, "b": b})
    calls = _fake_engine(monkeypatch, project)
    monkeypatch.setattr(cli.engine, "ensure_network", lambda *a, **k: False)
    monkeypatch.setattr(cli.networking, "verify_network", lambda net: {})
    monkeypatch.setattr(cli.networking, "validate_static_addresses", lambda *a: None)

    assert cli.cmd_up(_up_ns("-d", "--scale", "a=0")) == 0
    names = [args[args.index("--name") + 1] for kind, args, _ in calls if args[0] == "run"]
    assert names == ["demo-b-1"]


def test_up_reconciles_networks(monkeypatch, capsys):
    svc = Service(name="web", image="x", networks=["demo_front"])
    other = Service(name="db", image="x", networks=["demo_back", "shared"])
    project = Project(name="demo", directory=".", services={"web": svc, "db": other}, networks={
        "front": Network(key="front", name="demo_front", internal=True),
        "back": Network(key="back", name="demo_back"),
        "shared": Network(key="shared", name="shared", external=True),
        "unused": Network(key="unused", name="demo_unused"),
    })
    _fake_engine(monkeypatch, project)
    monkeypatch.setattr(cli.networking, "create_and_start", lambda *a, **k: None)
    ensured = []
    verified = []
    monkeypatch.setattr(cli.engine, "network_names", lambda: ["shared"])
    monkeypatch.setattr(cli.engine, "ensure_network", lambda name, **kw: ensured.append((name, kw)) or name == "demo_front")
    monkeypatch.setattr(cli.networking, "verify_network", lambda net: verified.append(net.name) or {"Name": net.name})
    monkeypatch.setattr(cli.networking, "validate_static_addresses",
                        lambda service, states: verified.append((service.name, sorted(states))))

    assert cli.cmd_up(_up_ns("-d")) == 0
    assert [name for name, _ in ensured] == ["demo_front", "demo_back"]
    front_kw = ensured[0][1]
    assert front_kw["internal"] is True and front_kw["dry_run"] is False
    assert front_kw["labels"][LABEL_PROJECT] == "demo"
    assert verified[:3] == ["demo_front", "demo_back", "shared"]
    assert ("web", ["demo_back", "demo_front", "shared"]) in verified
    assert capsys.readouterr().out.splitlines()[0] == "Network demo_front created"


def test_up_missing_external_network_is_an_error(monkeypatch):
    svc = Service(name="web", image="x", networks=["shared"])
    project = Project(name="demo", directory=".", services={"web": svc},
                      networks={"shared": Network(key="shared", name="shared", external=True)})
    _fake_engine(monkeypatch, project)
    monkeypatch.setattr(cli.engine, "network_names", lambda: [])
    with pytest.raises(ComposeError, match="external network 'shared' not found"):
        cli.cmd_up(_up_ns("-d"))


def test_up_dry_run_does_not_inspect_a_network_it_would_create(monkeypatch, capsys):
    svc = Service(name="web", image="x", networks=["demo_front"])
    project = Project(name="demo", directory=".", services={"web": svc},
                      networks={"front": Network(key="front", name="demo_front")})
    calls = _fake_engine(monkeypatch, project)
    monkeypatch.setattr(cli.engine, "ensure_network", lambda name, **kw: kw["dry_run"])
    monkeypatch.setattr(cli.networking, "verify_network", lambda net: pytest.fail("inspected"))

    assert cli.cmd_up(cli.build_parser().parse_args(["--dry-run", "up", "-d"])) == 0
    assert "Network demo_front created" in capsys.readouterr().out
    assert [args[0] for _, args, _ in calls] == ["run"]
    assert all(kw["dry_run"] is True for _, _, kw in calls)


def test_up_creates_only_needed_volumes(monkeypatch, capsys):
    svc = Service(name="web", image="x", volumes=[
        VolumeMount(type="volume", source="demo_data", target="/data"),
        VolumeMount(type="volume", source="cache", target="/cache"),
        VolumeMount(type="volume", source="demo_old", target="/old"),
    ])
    project = Project(name="demo", directory=".", services={"web": svc}, volumes={
        "data": Volume(key="data", name="demo_data", driver="local", driver_opts={"o": "bind"},
                       labels={"team": "a"}),
        "old": Volume(key="old", name="demo_old"),
        "cache": Volume(key="cache", name="cache", external=True),
        "unused": Volume(key="unused", name="demo_unused"),
    })
    _fake_engine(monkeypatch, project)
    ensured = []
    monkeypatch.setattr(cli.engine, "volume_names", lambda: ["cache"])
    monkeypatch.setattr(cli.engine, "ensure_volume",
                        lambda name, **kw: ensured.append((name, kw)) or name == "demo_data")

    assert cli.cmd_up(_up_ns("-d")) == 0
    assert [name for name, _ in ensured] == ["demo_data", "demo_old"]
    kw = ensured[0][1]
    assert kw["driver"] == "local" and kw["driver_opts"] == {"o": "bind"}
    assert kw["labels"]["team"] == "a" and kw["labels"][LABEL_PROJECT] == "demo"
    out = capsys.readouterr().out
    assert "Volume demo_data created" in out
    assert "Volume demo_old created" not in out  # already existed


def test_up_missing_external_volume_is_an_error(monkeypatch):
    svc = Service(name="web", image="x", volumes=[VolumeMount(type="volume", source="cache", target="/c")])
    project = Project(name="demo", directory=".", services={"web": svc},
                      volumes={"cache": Volume(key="cache", name="cache", external=True)})
    _fake_engine(monkeypatch, project)
    monkeypatch.setattr(cli.engine, "volume_names", lambda: [])
    with pytest.raises(ComposeError, match="external volume 'cache' not found"):
        cli.cmd_up(_up_ns("-d"))


@pytest.mark.parametrize("flag, present, expect_build", [
    ([], False, True),     # image absent: built
    ([], True, False),     # image present: reused
    (["--build"], True, True),
])
def test_up_builds_when_asked_or_image_missing(monkeypatch, tmp_path, capsys, flag, present, expect_build):
    svc = Service(name="app", build=BuildConfig(context=str(tmp_path)))
    project = Project(name="demo", directory=str(tmp_path), services={"app": svc})
    calls = _fake_engine(monkeypatch, project)
    probed = []
    monkeypatch.setattr(cli.engine, "image_exists", lambda name: probed.append(name) or present)

    assert cli.cmd_up(_up_ns("-d", *flag)) == 0
    builds = [args for _, args, _ in calls if args[0] == "build"]
    if expect_build:
        assert builds == [["build", "-t", "demo-app", str(tmp_path)]]
        assert "Building app ..." in capsys.readouterr().out
    else:
        assert builds == []
    assert probed == ([] if flag else ["demo-app"])


def test_up_recreates_a_stopped_outdated_container_without_stopping_it(monkeypatch, capsys):
    svc = Service(name="web", image="x")
    project = Project(name="demo", directory=".", services={"web": svc})
    calls = _fake_engine(monkeypatch, project, containers=[
        {"name": "demo-web-1", "service": "web", "index": 1, "hash": "stale", "running": False}])

    assert cli.cmd_up(_up_ns("-d")) == 0
    assert [args[0] for _, args, _ in calls] == ["remove", "run"]
    assert calls[0][1] == ["remove", "-f", "demo-web-1"]
    assert calls[1][1][calls[1][1].index("--name") + 1] == "demo-web-1"
    assert "Recreating demo-web-1 ..." in capsys.readouterr().out


def test_up_removes_surplus_replicas_only_of_the_scaled_service(monkeypatch, capsys):
    web = Service(name="web", image="x", replicas=2)
    db = Service(name="db", image="x", replicas=2)
    project = Project(name="demo", directory=".", services={"web": web, "db": db})
    current = [
        {"name": f"demo-{svc.name}-{i}", "service": svc.name, "index": i,
         "hash": svc.config_hash(project), "running": True}
        for svc in (web, db) for i in (1, 2)
    ]
    calls = _fake_engine(monkeypatch, project, containers=current)

    assert cli.cmd_up(_up_ns("-d", "--scale", "web=1")) == 0
    removed = [args for _, args, _ in calls if args[0] == "remove"]
    assert removed == [["remove", "-f", "demo-web-2"]]
    out = capsys.readouterr().out
    assert "Removing surplus demo-web-2 ..." in out
    assert "demo-db-2 is up-to-date" in out


def test_up_attaches_to_logs_when_not_detached(monkeypatch, capsys):
    web = Service(name="web", image="x")
    project = Project(name="demo", directory=".", services={"web": web})
    _fake_engine(monkeypatch, project)
    followed = []
    monkeypatch.setattr(cli, "_follow_logs",
                        lambda proj, service_names, follow: followed.append((proj, service_names, follow)) or 7)

    assert cli.cmd_up(_up_ns()) == 7
    assert followed == [(project, ["web"], True)]
    assert "Attaching to logs" in capsys.readouterr().out


def test_up_without_new_containers_does_not_attach(monkeypatch):
    web = Service(name="web", image="x")
    project = Project(name="demo", directory=".", services={"web": web})
    _fake_engine(monkeypatch, project, containers=[
        {"name": "demo-web-1", "service": "web", "index": 1,
         "hash": web.config_hash(project), "running": True}])
    monkeypatch.setattr(cli, "_follow_logs", lambda *a, **k: pytest.fail("attached"))

    assert cli.cmd_up(_up_ns()) == 0


# --- down --------------------------------------------------------------------------


def test_down_stops_removes_and_cleans_owned_resources(monkeypatch, capsys):
    project = Project(name="demo", directory=".", networks={
        "front": Network(key="front", name="demo_front"),
        "foreign": Network(key="foreign", name="demo_foreign"),
        "busy": Network(key="busy", name="demo_busy"),
        "ext": Network(key="ext", name="shared", external=True),
        "gone": Network(key="gone", name="demo_gone"),
    }, volumes={
        "data": Volume(key="data", name="demo_data"),
        "stuck": Volume(key="stuck", name="demo_stuck"),
        "ext": Volume(key="ext", name="cache", external=True),
        "gone": Volume(key="gone", name="demo_gone"),
    })
    calls = []
    reconciled = []

    def fake_run(args, **kw):
        calls.append(args)
        if args == ["volume", "remove", "demo_stuck"]:
            raise WslcError("in use")

    def fake_inspect_network(name):
        if name == "demo_busy":
            raise WslcError("boom")
        return {"Labels": {LABEL_PROJECT: "other" if name == "demo_foreign" else "demo"}}

    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(cli, "_project_containers", lambda _, all_states=False: [
        {"name": "demo-web-1", "running": True, "host_gateway": True},
        {"name": "demo-job-1", "running": False},
    ])
    monkeypatch.setattr(cli.engine, "run", fake_run)
    monkeypatch.setattr(cli.engine, "network_names",
                        lambda: ["demo_front", "demo_foreign", "demo_busy", "shared"])
    monkeypatch.setattr(cli.engine, "volume_names", lambda: ["demo_data", "demo_stuck", "cache"])
    monkeypatch.setattr(cli.networking, "inspect_network", fake_inspect_network)
    monkeypatch.setattr(cli.host_gateway, "reconcile", lambda p, dry, **kw: reconciled.append((dry, kw)))

    ns = cli.build_parser().parse_args(["down", "-v", "-t", "3"])
    assert cli.cmd_down(ns) == 0
    assert calls == [
        ["stop", "-t", "3", "demo-web-1"],
        ["remove", "-f", "demo-web-1"],
        ["remove", "-f", "demo-job-1"],
        ["network", "remove", "demo_front"],
        ["volume", "remove", "demo_data"],
        ["volume", "remove", "demo_stuck"],
    ]
    assert reconciled == [(False, {"cleanup": True, "previously_enabled": True})]
    err = capsys.readouterr().err
    assert "preserving network demo_foreign (project ownership is unverified)" in err
    assert "could not remove network demo_busy (still in use?)" in err
    assert "could not remove volume demo_stuck" in err


def test_down_keeps_volumes_by_default(monkeypatch):
    project = Project(name="demo", directory=".", volumes={"data": Volume(key="data", name="demo_data")})
    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(cli, "_project_containers", lambda *_, **__: [])
    monkeypatch.setattr(cli.engine, "network_names", lambda: [])
    monkeypatch.setattr(cli.engine, "volume_names", lambda: pytest.fail("volumes listed"))
    monkeypatch.setattr(cli.engine, "run", lambda *a, **k: pytest.fail("engine run"))

    assert cli.cmd_down(cli.build_parser().parse_args(["down"])) == 0


# --- ps ------------------------------------------------------------------------------


def _entry(name, service, index=1, **extra):
    return {"id": f"id-{name}", "name": name, "image": "img", "command": "cmd", "service": service,
            "index": index, "created": "1 second ago", "status": "running", "ports": [], **extra}


def test_ps_quiet_prints_only_ids_of_selected_services(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_load", lambda _: None)
    monkeypatch.setattr(cli, "_project_containers", lambda *_, **__: [
        _entry("demo-web-1", "web"), _entry("demo-db-1", "db")])

    assert cli.cmd_ps(cli.build_parser().parse_args(["ps", "-q", "web"])) == 0
    assert capsys.readouterr().out == "id-demo-web-1\n"


def test_ps_table_sorted_by_service_and_index(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_load", lambda _: None)
    monkeypatch.setattr(cli, "_project_containers", lambda *_, **__: [
        _entry("demo-web-2", "web", 2), _entry("demo-web-1", "web", 1),
        _entry("demo-db-1", "db", ports=[{"Protocol": 6, "HostPort": 5432, "ContainerPort": 5432}]),
    ])

    assert cli.cmd_ps(cli.build_parser().parse_args(["ps"])) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in lines[1:]] == ["demo-db-1", "demo-web-1", "demo-web-2"]
    assert lines[1].endswith("0.0.0.0:5432->5432/tcp")


# --- logs ----------------------------------------------------------------------------


class _Proc:
    def __init__(self, lines, code=0, interrupt=False):
        self.stdout = iter(lines)
        self.code = code
        self.interrupt = interrupt
        self.signals = []

    def wait(self):
        if self.interrupt:
            raise KeyboardInterrupt
        return self.code

    def send_signal(self, sig):
        self.signals.append(sig)


def test_follow_logs_builds_wslc_logs_argv_and_prefixes_output(monkeypatch, capsys):
    containers = [
        {"name": "demo-web-1", "service": "web", "running": True},
        {"name": "demo-db-1", "service": "db", "running": False},
        {"name": "demo-x-1", "service": "x", "running": True},
    ]
    monkeypatch.setattr(cli, "_project_containers", lambda *_, **__: containers)
    popened = []

    def fake_popen(args, **kw):
        popened.append((args, kw))
        return _Proc([f"hello from {args[-1]}\n"])

    monkeypatch.setattr(cli.engine, "popen", fake_popen)

    assert cli._follow_logs(None, ["web", "db"], follow=True, tail=5, timestamps=True) == 0
    assert [args for args, _ in popened] == [
        ["logs", "-f", "-n", "5", "-t", "demo-web-1"],
        ["logs", "-n", "5", "-t", "demo-db-1"],  # stopped: -f would never return
    ]
    assert popened[0][1]["text"] is True
    out = capsys.readouterr().out
    assert "demo-web-1 | hello from demo-web-1\n" in out
    assert "demo-db-1  | hello from demo-db-1\n" in out


def test_follow_logs_without_containers(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_project_containers", lambda *_, **__: [
        {"name": "demo-web-1", "service": "web", "running": True}])
    assert cli._follow_logs(None, ["db"], follow=False) == 1
    assert capsys.readouterr().err == "wslc-compose: no containers found\n"


def test_follow_logs_ctrl_c_terminates_log_processes(monkeypatch):
    monkeypatch.setattr(cli, "_project_containers", lambda *_, **__: [
        {"name": "demo-web-1", "service": "web", "running": True},
        {"name": "demo-db-1", "service": "db", "running": True}])
    procs = []
    monkeypatch.setattr(cli.engine, "popen",
                        lambda args, **kw: procs.append(_Proc([], interrupt=True)) or procs[-1])

    assert cli._follow_logs(None, [], follow=True) == 0
    assert [p.signals for p in procs] == [[signal.SIGTERM], [signal.SIGTERM]]


def test_logs_command_passes_options(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "_load", lambda _: "project")
    monkeypatch.setattr(cli, "_follow_logs", lambda *a, **k: seen.append((a, k)) or 0)
    assert cli.main(["logs", "-f", "-n", "3", "-t", "web"]) == 0
    assert seen == [(("project",), {"service_names": ["web"], "follow": True, "tail": 3, "timestamps": True})]


# --- exec ------------------------------------------------------------------------------


def test_exec_unknown_service(monkeypatch, capsys):
    project = Project(name="demo", directory=".", services={"db": Service(name="db", image="x")})
    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(cli.engine, "run", lambda *a, **k: pytest.fail("engine run"))

    assert cli.main(["exec", "web", "sh"]) == 1
    assert capsys.readouterr().err == "wslc-compose: no such service: web\n"


def test_exec_forwards_user_workdir_env_and_exit_code(monkeypatch):
    project = Project(name="demo", directory=".", services={"db": Service(name="db", image="x")})
    calls = []
    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(cli.engine, "run",
                        lambda args, **kw: calls.append((args, kw)) or SimpleNamespace(returncode=5))

    assert cli.main(["--dry-run", "exec", "-T", "-u", "postgres", "-w", "/srv", "-e", "A=1", "-e", "B=2",
                     "--index", "2", "db", "psql", "-c", "select 1"]) == 5
    assert calls == [(["exec", "-i", "-u", "postgres", "-w", "/srv", "-e", "A=1", "-e", "B=2",
                       "demo-db-2", "psql", "-c", "select 1"], {"check": False, "dry_run": True})]


# --- helpers -----------------------------------------------------------------------


def test_command_reset_pulls_image_when_entrypoint_unknown(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli.engine, "run", lambda args, **kw: calls.append(args))
    monkeypatch.setattr(cli.engine, "image_entrypoint", lambda image: None)
    project = Project(name="demo", directory=".")

    cli._pull_for_command_reset(project, Service(name="web", image="nginx", command=[]), False)

    assert calls == [["pull", "nginx"]]
    assert capsys.readouterr().out == "Pulling nginx ...\n"


@pytest.mark.parametrize("svc, dry_run, entrypoint", [
    (Service(name="web", image="nginx", command=[]), False, ["/entry"]),  # already local
    (Service(name="web", image="nginx", command=[]), True, None),          # dry run
    (Service(name="web", image="nginx", command=["x"]), False, None),      # no reset
    (Service(name="web", image="nginx", command=[], entrypoint=["sh"]), False, None),
])
def test_command_reset_does_not_pull(monkeypatch, svc, dry_run, entrypoint):
    monkeypatch.setattr(cli.engine, "run", lambda *a, **k: pytest.fail("pulled"))
    monkeypatch.setattr(cli.engine, "image_entrypoint", lambda image: entrypoint)
    cli._pull_for_command_reset(Project(name="demo", directory="."), svc, dry_run)


def test_extra_hosts_skips_malformed_entries(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.engine, "run", lambda args, **kw: calls.append(args))

    cli._apply_extra_hosts("c", ["nocolon", ":1.2.3.4", "name:", " db : 10.0.0.2 "], True)
    cli._apply_extra_hosts("c", ["nocolon"], False)

    assert len(calls) == 1
    script = calls[0][-1]
    assert script.endswith('; echo "10.0.0.2 db" >> /etc/hosts')
    assert script.count(">> /etc/hosts") == 1


# --- start / stop / restart ------------------------------------------------------------


def test_stop_only_selected_running_services(monkeypatch, capsys):
    project = Project(name="demo", directory=".", services={"web": Service(name="web", image="x")})
    calls = []
    states = []
    reconciled = []
    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(cli, "_project_containers", lambda _, all_states=False: states.append(all_states) or [
        {"name": "demo-web-1", "service": "web", "running": True},
        {"name": "demo-web-2", "service": "web", "running": False},
        {"name": "demo-db-1", "service": "db", "running": True}])
    monkeypatch.setattr(cli.engine, "run", lambda args, **kw: calls.append(args))
    monkeypatch.setattr(cli.engine, "run_retried", lambda *a, **k: pytest.fail("started"))
    monkeypatch.setattr(cli.host_gateway, "reconcile", lambda *a, **kw: reconciled.append(kw))

    assert cli.main(["stop", "-t", "4", "web"]) == 0
    assert states == [False]
    assert calls == [["stop", "-t", "4", "demo-web-1"]]
    assert reconciled == [{"previously_enabled": False}]


def test_start_applies_extra_hosts(monkeypatch):
    project = Project(name="demo", directory=".",
                      services={"web": Service(name="web", image="x", extra_hosts=["db:10.0.0.2"])})
    calls = []
    states = []
    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(cli, "_project_containers", lambda _, all_states=False: states.append(all_states) or [
        {"name": "demo-web-1", "service": "web", "running": False}])
    monkeypatch.setattr(cli.engine, "run", lambda args, **kw: calls.append(args[:4]))
    monkeypatch.setattr(cli.engine, "run_retried", lambda args, **kw: calls.append(args))
    monkeypatch.setattr(cli.host_gateway, "reconcile", lambda *a, **kw: None)

    assert cli.main(["start"]) == 0
    assert states == [True]
    assert calls == [["start", "demo-web-1"], ["exec", "--user", "0", "demo-web-1"]]


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_lifecycle_without_containers(monkeypatch, capsys, action):
    monkeypatch.setattr(cli, "_load", lambda _: Project(name="demo", directory="."))
    monkeypatch.setattr(cli, "_project_containers", lambda *_, **__: [
        {"name": "demo-web-1", "service": "web", "running": True}])

    assert cli.main([action, "db"]) == 1
    assert capsys.readouterr().err == "wslc-compose: no containers found\n"


# --- pull / build / config / version -------------------------------------------------


def test_pull_only_services_with_an_image(monkeypatch, capsys):
    project = Project(name="demo", directory=".", services={
        "web": Service(name="web", image="nginx"),
        "app": Service(name="app", build=BuildConfig(context=".")),
    })
    calls = []
    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(cli.engine, "run", lambda args, **kw: calls.append((args, kw)))

    assert cli.main(["--dry-run", "pull"]) == 0
    assert calls == [(["pull", "nginx"], {"dry_run": True})]
    assert capsys.readouterr().out == "Pulling nginx ...\n"


def test_build_only_services_with_a_build_section(monkeypatch, tmp_path, capsys):
    project = Project(name="demo", directory=str(tmp_path), services={
        "web": Service(name="web", image="nginx"),
        "app": Service(name="app", build=BuildConfig(context=str(tmp_path))),
    })
    calls = []
    mapped = []
    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(cli.engine, "run", lambda args, **kw: calls.append((args, kw)))
    monkeypatch.setattr(cli.engine, "to_host_path", lambda p: mapped.append(p) or p)

    assert cli.main(["build", "--no-cache"]) == 0
    assert calls == [(["build", "-t", "demo-app", "--no-cache", str(tmp_path)], {"dry_run": False})]
    assert mapped == [str(tmp_path)]
    assert capsys.readouterr().out == "Building app ...\n"


def test_build_without_build_sections_fails(monkeypatch, capsys):
    project = Project(name="demo", directory=".", services={"web": Service(name="web", image="nginx")})
    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(cli.engine, "run", lambda *a, **k: pytest.fail("engine run"))

    assert cli.main(["build"]) == 1
    assert capsys.readouterr().err == "wslc-compose: no services with a build section\n"


def test_config_prints_resolved_project_as_yaml(tmp_path, capsys):
    compose = tmp_path / "compose.yaml"
    compose.write_text("services:\n  web:\n    image: nginx\n")

    assert cli.main(["-f", str(compose), "-p", "demo", "config"]) == 0
    printed = yaml.safe_load(capsys.readouterr().out)
    assert printed["name"] == "demo"
    assert printed["services"]["web"]["image"] == "nginx"


def test_version_prints_own_and_wslc_version(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli.engine, "run", lambda args, **kw: calls.append((args, kw)))

    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out == f"wslc-compose {__version__}\n"
    assert calls == [(["version"], {"check": False})]


def test_version_without_wslc_still_succeeds(monkeypatch, capsys):
    def missing(*a, **k):
        raise WslcError("wslc not found")

    monkeypatch.setattr(cli.engine, "run", missing)

    assert cli.main(["version"]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"wslc-compose {__version__}\n"
    assert captured.err == "wslc-compose: wslc not found\n"


def test_main_ctrl_c_exits_130(monkeypatch):
    def interrupted(ns):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "cmd_version", interrupted)
    assert cli.main(["version"]) == 130


@pytest.mark.filterwarnings("ignore:.*found in sys.modules:RuntimeWarning")
def test_cli_module_runs_as_a_script(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(engine, "run", lambda args, **kw: calls.append(args))
    monkeypatch.setattr(sys, "argv", ["wslc-compose", "version"])

    with pytest.raises(SystemExit) as exc:
        runpy.run_module("wslc_compose.cli", run_name="__main__")

    assert exc.value.code == 0
    assert calls == [["version"]]
    assert capsys.readouterr().out == f"wslc-compose {__version__}\n"


# --- shim ------------------------------------------------------------------------------


def test_shim_dispatches_compose_to_the_cli(monkeypatch):
    seen = []
    monkeypatch.setattr(shim.cli, "main", lambda args: seen.append(args) or 4)

    assert shim.main(["compose", "ps", "-a"]) == 4
    assert shim.main(["compose"]) == 4
    assert seen == [["ps", "-a"], ["--help"]]


def test_shim_reads_sys_argv_and_passes_through(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "argv", ["wslc", "list", "-a"])
    monkeypatch.setattr(shim.engine, "find_wslc", lambda: "/opt/wslc")
    monkeypatch.setattr(shim.subprocess, "call", lambda argv: calls.append(argv) or 3)

    assert shim.main() == 3
    assert calls == [["/opt/wslc", "list", "-a"]]


def test_shim_reports_missing_wslc(monkeypatch, capsys):
    def missing():
        raise WslcError("wslc not found on PATH")

    monkeypatch.setattr(shim.engine, "find_wslc", missing)
    monkeypatch.setattr(shim.subprocess, "call", lambda argv: pytest.fail("called"))

    assert shim.main(["list"]) == 1
    assert capsys.readouterr().err == "wslc: wslc not found on PATH\n"


def test_shim_ctrl_c_exits_130(monkeypatch):
    def interrupted(argv):
        raise KeyboardInterrupt

    monkeypatch.setattr(shim.engine, "find_wslc", lambda: "wslc")
    monkeypatch.setattr(shim.subprocess, "call", interrupted)

    assert shim.main(["logs", "-f", "x"]) == 130


@pytest.mark.filterwarnings("ignore:.*found in sys.modules:RuntimeWarning")
def test_shim_module_runs_as_a_script(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "main", lambda args: seen.append(args) or 2)
    monkeypatch.setattr(sys, "argv", ["wslc", "compose", "up", "-d"])

    with pytest.raises(SystemExit) as exc:
        runpy.run_module("wslc_compose.shim", run_name="__main__")

    assert exc.value.code == 2
    assert seen == [["up", "-d"]]


# --- python -m wslc_compose --------------------------------------------------------------


def test_package_runs_with_python_dash_m(monkeypatch):
    monkeypatch.setattr(cli, "main", lambda: 9)

    with pytest.raises(SystemExit) as exc:
        runpy.run_module("wslc_compose", run_name="__main__")

    assert exc.value.code == 9


def test_importing_dunder_main_does_not_run_the_cli(monkeypatch):
    monkeypatch.setattr(cli, "main", lambda: pytest.fail("ran on import"))
    monkeypatch.delitem(sys.modules, "wslc_compose.__main__", raising=False)

    module = importlib.import_module("wslc_compose.__main__")

    assert module.main is cli.main
