from datetime import datetime, timezone
from types import SimpleNamespace

from wslc_compose import cli
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
        }
    ]
    assert cli._format_ports(containers[0]["ports"]) == "127.0.0.1:1521->1521/tcp"


def test_format_created_matches_docker_compose_relative_output():
    now = datetime(2026, 10, 23, 14, 19, 44, tzinfo=timezone.utc)

    assert cli._format_created("2026-10-23 16:19:41 +0200 CEST", now) == "3 seconds ago"


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
