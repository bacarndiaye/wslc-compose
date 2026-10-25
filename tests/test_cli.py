from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from wslc_compose import cli
from wslc_compose.engine import WslcError
from wslc_compose.loader import ComposeError
from wslc_compose.model import Project, Service


def test_ps_accepts_all_option():
    parser = cli.build_parser()

    assert parser.parse_args(["ps"]).all is False
    assert parser.parse_args(["ps", "-a"]).all is True
    assert parser.parse_args(["ps", "--all"]).all is True


def test_ps_uses_all_flag_when_requested(monkeypatch):
    calls = []
    project = SimpleNamespace(name="demo")
    ns = SimpleNamespace(all=True, services=[], quiet=False)

    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(
        cli,
        "_project_containers",
        lambda _, all_states=False: calls.append(all_states) or [],
    )

    assert cli.cmd_ps(ns) == 0
    assert calls == [True]


def test_ps_excludes_stopped_by_default(monkeypatch):
    calls = []
    project = SimpleNamespace(name="demo")
    ns = SimpleNamespace(all=False, services=[], quiet=False)

    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(
        cli,
        "_project_containers",
        lambda _, all_states=False: calls.append(all_states) or [],
    )

    assert cli.cmd_ps(ns) == 0
    assert calls == [False]


def test_down_still_includes_stopped_containers(monkeypatch):
    calls = []
    project = SimpleNamespace(name="demo", networks={}, volumes={})
    ns = SimpleNamespace(timeout=10, dry_run=False, volumes=False)

    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(
        cli,
        "_project_containers",
        lambda _, all_states=False: calls.append(all_states) or [],
    )
    monkeypatch.setattr(cli.engine, "network_names", lambda: [])

    assert cli.cmd_down(ns) == 0
    assert calls == [True]


def test_up_starts_unchanged_stopped_shared_container(monkeypatch):
    service = Service(name="web", image="fixture")
    project = Project(name="demo", directory=".", services={"web": service})
    inventory_calls = []
    starts = []
    ns = SimpleNamespace(
        services=[], profile=[], scale=[], dry_run=False, build=False,
        force_recreate=False, timeout=10, detach=True,
    )

    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(cli, "_project_containers", lambda _, all_states=False: inventory_calls.append(all_states) or [{
        "name": "demo-web-1", "service": "web", "index": 1,
        "hash": service.config_hash(), "running": False,
    }])
    monkeypatch.setattr(cli.engine, "run_retried", lambda args, **_: starts.append(args))

    assert cli.cmd_up(ns) == 0
    assert inventory_calls == [True]
    assert starts == [["start", "demo-web-1"]]


def test_extra_hosts_are_applied_after_start(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.engine, "run", lambda args, **kwargs: calls.append((args, kwargs)))

    cli._apply_extra_hosts("demo-web-1", ["db:10.0.0.2", "host:host-gateway"], False)

    args, kwargs = calls[0]
    assert args[:6] == ["exec", "--user", "0", "demo-web-1", "sh", "-c"]
    assert "/proc/net/route" in args[6]
    assert "10.0.0.2 db" in args[6]
    assert "$GW host" in args[6]
    assert kwargs["check"] is False




def test_project_containers_accepts_wslc_list_field_names(monkeypatch):
    monkeypatch.setattr(cli, "_format_created", lambda value: "3 seconds ago")
    monkeypatch.setattr(
        cli.engine,
        "list_project_containers",
        lambda project, all_states=False: [
            {
                "ID": "a53e9fdf3408",
                "Names": "oracle",
                "Image": "example/oracle:slim",
                "Command": "oracle-xe",
                "CreatedAt": "2026-10-23 16:19:41 +0200 CEST",
                "Status": "Up 6 seconds",
                "Ports": "127.0.0.1:1521->1521/tcp",
            }
        ],
    )
    inspected = []

    def fake_inspect(identifier):
        inspected.append(identifier)
        return {
            "Labels": {
                "com.wslc-compose.service": "oracle",
                "com.wslc-compose.container-number": "1",
                "com.wslc-compose.config-hash": "abc123",
            },
            "State": {"Running": True, "Status": "running"},
        }

    monkeypatch.setattr(cli.engine, "inspect", fake_inspect)

    containers = cli._project_containers(Project(name="user-service", directory="."))

    assert inspected == ["a53e9fdf3408"]
    assert containers == [
        {
            "id": "a53e9fdf3408",
            "name": "oracle",
            "image": "example/oracle:slim",
            "command": "oracle-xe",
            "service": "oracle",
                "index": 1,
                "hash": "abc123",
                "running": True,
            "created": "3 seconds ago",
            "status": "Up 6 seconds",
            "ports": "127.0.0.1:1521->1521/tcp",
            "networks": None,
            "network_mode": None,
        }
    ]
    assert cli._format_ports(containers[0]["ports"]) == "127.0.0.1:1521->1521/tcp"


def test_format_created_matches_docker_compose_relative_output():
    now = datetime(2026, 10, 23, 14, 19, 44, tzinfo=timezone.utc)

    assert cli._format_created("2026-10-23 16:19:41 +0200 CEST", now) == "3 seconds ago"


@pytest.mark.parametrize(
    ("desired", "requested", "actual", "running", "action"),
    [
        ("10.0.0.51", "10.0.0.51", "10.0.0.51", True, "keep"),
        ("10.0.0.51", "10.0.0.51", "", False, "start"),
        ("10.0.0.52", "10.0.0.51", "10.0.0.51", True, "recreate"),
        ("10.0.0.51", "10.0.0.51", "10.0.0.2", True, "recreate"),
        ("10.0.0.51", "10.0.0.51", "", True, "recreate"),
        ("10.0.0.51", None, "10.0.0.51", True, "recreate"),
        (None, "10.0.0.51", "10.0.0.51", True, "recreate"),
        (None, None, "10.0.0.2", True, "keep"),
    ],
)
def test_up_reconciles_ipv4_even_when_shared_hash_matches(
    monkeypatch, desired, requested, actual, running, action
):
    service = Service(name="web", image="fixture", networks=["front"],
                      network_ipv4_addresses={"front": desired} if desired else {})
    project = Project(name="demo", directory=".", services={"web": service})
    current = {"name": "demo-web-1", "service": "web", "index": 1,
               "hash": service.config_hash(project), "running": running,
               "networks": {"front": {"IPAddress": actual, "Aliases": ["web"],
                   "IPAMConfig": {"IPv4Address": requested} if requested else None}}}
    calls = []
    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(cli, "_project_containers", lambda *_, **__: [current])
    monkeypatch.setattr(cli.engine, "run", lambda args, **_: calls.append(args))
    monkeypatch.setattr(cli.engine, "run_retried", lambda args, **_: calls.append(args))
    monkeypatch.setattr(cli.engine, "inspect", lambda _: {
        "State": {"Running": True}, "NetworkSettings": {"Networks": {"front": {
            "IPAddress": desired, "Aliases": ["web"], "IPAMConfig": {"IPv4Address": desired}}}}})
    ns = cli.build_parser().parse_args(["up", "-d"])

    assert cli.cmd_up(ns) == 0
    commands = [args[0] for args in calls]
    assert commands == {"keep": [], "start": ["start"],
                        "recreate": (["stop"] if running else []) + ["remove", "run"]}[action]
    if action == "recreate" and desired:
        assert calls[-1][calls[-1].index("--ip") + 1] == desired


def test_up_rejects_static_ip_scaling_before_engine_access(monkeypatch):
    service = Service(name="web", image="fixture", networks=["front"],
                      network_ipv4_addresses={"front": "10.0.0.51"})
    project = Project(name="demo", directory=".", services={"web": service})
    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(cli.engine, "run", lambda *_args, **_kwargs: pytest.fail("engine accessed"))
    with pytest.raises(ComposeError, match="multiple replicas"):
        cli.cmd_up(cli.build_parser().parse_args(["up", "-d", "--scale", "web=2"]))


@pytest.mark.parametrize("requested, actual", [(None, "10.0.0.2"), ("10.0.0.51", "10.0.0.2")])
def test_up_fails_when_engine_ignores_static_ip(monkeypatch, requested, actual):
    service = Service(name="web", image="fixture", networks=["front"],
                      network_ipv4_addresses={"front": "10.0.0.51"})
    project = Project(name="demo", directory=".", services={"web": service})
    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(cli, "_project_containers", lambda *_, **__: [])
    monkeypatch.setattr(cli.engine, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.engine, "inspect", lambda _: {
        "State": {"Running": True}, "NetworkSettings": {"Networks": {"front": {
            "IPAddress": actual, "IPAMConfig": {"IPv4Address": requested}}}}})
    with pytest.raises(WslcError, match="did not apply"):
        cli.cmd_up(cli.build_parser().parse_args(["up", "-d"]))


def test_ps_matches_docker_compose_column_order(monkeypatch, capsys):
    ns = SimpleNamespace(all=False, services=[], quiet=False)
    project = SimpleNamespace(name="demo")
    container = {
        "id": "123",
        "name": "demo-web-1",
        "image": "nginx:alpine",
        "command": "nginx -g daemon off;",
        "service": "web",
        "index": 1,
        "created": "2026-10-23 16:19:41 +0200 CEST",
        "status": "running",
        "ports": [],
    }

    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(cli, "_project_containers", lambda _, all_states=False: [container])

    assert cli.cmd_ps(ns) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == [
        "NAME",
        "IMAGE",
        "COMMAND",
        "SERVICE",
        "CREATED",
        "STATUS",
        "PORTS",
    ]
    assert "demo-web-1" in lines[1]
    assert "nginx:alpine" in lines[1]
    assert "nginx -g daemon off;" in lines[1]


def test_ps_uses_docker_compose_minimum_column_width(capsys):
    cli._print_table([], ["NAME", "IMAGE", "COMMAND", "SERVICE", "CREATED", "STATUS", "PORTS"])

    assert capsys.readouterr().out == "NAME      IMAGE     COMMAND   SERVICE   CREATED   STATUS    PORTS\n"
