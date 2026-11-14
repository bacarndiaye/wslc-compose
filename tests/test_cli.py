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
            "host_gateway": False,
            "local_config": None,
            "restart_policy": None,
            "cap_add": None,
            "cap_drop": None,
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
def test_up_fails_when_engine_ignores_static_ip(monkeypatch, capsys, requested, actual):
    service = Service(name="web", image="fixture", networks=["front"],
                      network_ipv4_addresses={"front": "10.0.0.51"})
    project = Project(name="demo", directory=".", services={"web": service})
    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(cli, "_project_containers", lambda *_, **__: [])
    monkeypatch.setattr(cli.engine, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.engine, "inspect", lambda _: {
        "State": {"Running": True}, "NetworkSettings": {"Networks": {"front": {
            "IPAddress": actual, "IPAMConfig": {"IPv4Address": requested}}}}})
    assert cli.cmd_up(cli.build_parser().parse_args(["up", "-d"])) == 1
    assert "did not apply" in capsys.readouterr().err


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


def test_up_waits_for_conditional_dependency_before_starting_dependent(monkeypatch):
    db = Service(name="db", image="fixture")
    web = Service(name="web", image="fixture", depends_on=["db"],
                  depends_on_conditions={"db": "service_healthy"})
    project = Project(name="demo", directory=".", services={"db": db, "web": web})
    events = []
    ns = SimpleNamespace(
        services=[], profile=[], scale=[], dry_run=False, build=False,
        force_recreate=False, timeout=10, detach=True, wait_timeout=42,
    )
    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(cli, "_project_containers", lambda *_, **__: [])
    monkeypatch.setattr(cli.engine, "run", lambda args, **_: events.append(("run", args[-1] if args[0] != "run" else "run")))
    monkeypatch.setattr(cli.host_gateway, "reconcile", lambda *a, **k: None)
    monkeypatch.setattr(cli.networking, "verify_container", lambda *a, **k: None)
    monkeypatch.setattr(cli.dependencies, "wait_for_dependencies",
                        lambda proj, svc, selected, **kw: events.append(("wait", svc.name, kw["timeout"])))

    assert cli.cmd_up(ns) == 0
    kinds = [e[0] for e in events]
    assert kinds == ["run", "wait", "run"]  # db started, then wait, then web started
    assert events[1] == ("wait", "web", 42)


def test_up_dry_run_does_not_wait(monkeypatch, capsys):
    db = Service(name="db", image="fixture")
    web = Service(name="web", image="fixture", depends_on=["db"],
                  depends_on_conditions={"db": "service_healthy"})
    project = Project(name="demo", directory=".", services={"db": db, "web": web})
    ns = SimpleNamespace(
        services=[], profile=[], scale=[], dry_run=True, build=False,
        force_recreate=False, timeout=10, detach=True,
    )
    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(cli.dependencies, "wait_for_dependencies",
                        lambda *a, **k: pytest.fail("dry run must not wait"))
    assert cli.cmd_up(ns) == 0
    assert "would wait for db: service_healthy" in capsys.readouterr().out


def _opts(monkeypatch, *names):
    monkeypatch.setattr(cli.engine, "run_options", lambda: frozenset(names))


def test_runtime_warnings_on_old_wslc(monkeypatch):
    svc = Service(name="web", image="x", restart="always", cap_add=["NET_ADMIN"])
    _opts(monkeypatch)
    messages = cli._runtime_options_warnings([svc])
    assert any("restart: always" in m and "3.0.5" in m for m in messages)
    assert any("cap_add/cap_drop" in m for m in messages)


def test_no_runtime_warnings_when_wslc_supports_options(monkeypatch):
    svc = Service(name="web", image="x", restart="always", cap_add=["NET_ADMIN"])
    _opts(monkeypatch, "--restart", "--cap-add", "--cap-drop")
    assert cli._runtime_options_warnings([svc]) == []


def test_runtime_matches_detects_policy_and_capability_drift(monkeypatch):
    _opts(monkeypatch, "--restart", "--cap-add", "--cap-drop")
    svc = Service(name="web", image="x", restart="always", cap_add=["NET_ADMIN"], cap_drop=["MKNOD"])
    good = {"restart_policy": "always", "cap_add": ["CAP_NET_ADMIN"], "cap_drop": ["MKNOD"]}
    assert cli._runtime_matches(svc, good)
    assert not cli._runtime_matches(svc, {**good, "restart_policy": "no"})
    assert not cli._runtime_matches(svc, {**good, "cap_add": []})
    assert not cli._runtime_matches(svc, {**good, "cap_drop": None})
    # restart removed from the compose file: the old container must be recreated
    assert not cli._runtime_matches(Service(name="web", image="x", cap_add=["NET_ADMIN"], cap_drop=["MKNOD"]), good)


def test_runtime_matches_ignores_unsupported_options(monkeypatch):
    _opts(monkeypatch)  # wslc 3.0.3: never loop on recreate
    svc = Service(name="web", image="x", restart="always", cap_add=["NET_ADMIN"])
    assert cli._runtime_matches(svc, {})


def test_plain_service_matches_container_without_runtime_fields(monkeypatch):
    _opts(monkeypatch, "--restart", "--cap-add", "--cap-drop")
    assert cli._runtime_matches(Service(name="web", image="x"), {"restart_policy": "no", "cap_add": [], "cap_drop": []})
    assert cli._runtime_matches(Service(name="web", image="x"), {})


def _up_failure_fixture(monkeypatch, project, failing):
    """Fake engine where `wslc run` fails for the containers named in `failing`."""
    runs = []

    def fake_run(args, **_):
        if args[0] == "run":
            name = args[args.index("--name") + 1]
            if name in failing:
                raise WslcError("wslc run -d failed (exit 1)")
            runs.append(name)

    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(cli, "_project_containers", lambda *_, **__: [])
    monkeypatch.setattr(cli.engine, "run", fake_run)
    monkeypatch.setattr(cli.host_gateway, "reconcile", lambda *a, **k: None)
    monkeypatch.setattr(cli.networking, "verify_container", lambda *a, **k: None)
    return runs


def test_up_keeps_starting_other_services_when_one_fails(monkeypatch, capsys):
    # issue #38: a host port already taken must not stop unrelated services
    services = {name: Service(name=name, image="fixture") for name in ("a", "b", "c")}
    project = Project(name="demo", directory=".", services=services)
    runs = _up_failure_fixture(monkeypatch, project, failing={"demo-b-1"})

    assert cli.cmd_up(cli.build_parser().parse_args(["up", "-d"])) == 1
    assert runs == ["demo-a-1", "demo-c-1"]
    err = capsys.readouterr().err
    assert "Container demo-b-1 failed to start" in err
    assert "1 service failed to start: b (" in err


def test_up_skips_dependents_of_a_failed_service(monkeypatch, capsys):
    db = Service(name="db", image="fixture")
    web = Service(name="web", image="fixture", depends_on=["db"])
    worker = Service(name="worker", image="fixture", depends_on=["db"],
                     depends_on_optional=["db"])
    other = Service(name="other", image="fixture")
    project = Project(name="demo", directory=".",
                      services={"db": db, "web": web, "worker": worker, "other": other})
    runs = _up_failure_fixture(monkeypatch, project, failing={"demo-db-1"})

    assert cli.cmd_up(cli.build_parser().parse_args(["up", "-d"])) == 1
    assert sorted(runs) == ["demo-other-1", "demo-worker-1"]  # required: false still starts
    err = capsys.readouterr().err
    assert "Skipping web: dependency 'db' failed to start" in err
    assert "2 services failed to start" in err


def test_up_skips_service_whose_dependency_condition_fails(monkeypatch, capsys):
    db = Service(name="db", image="fixture")
    web = Service(name="web", image="fixture", depends_on=["db"],
                  depends_on_conditions={"db": "service_healthy"})
    other = Service(name="other", image="fixture")
    project = Project(name="demo", directory=".", services={"db": db, "web": web, "other": other})
    runs = _up_failure_fixture(monkeypatch, project, failing=set())

    def unhealthy(proj, svc, selected, **kw):
        raise WslcError("dependency 'db' is unhealthy (container demo-db-1)")

    monkeypatch.setattr(cli.dependencies, "wait_for_dependencies", unhealthy)

    assert cli.cmd_up(cli.build_parser().parse_args(["up", "-d"])) == 1
    assert sorted(runs) == ["demo-db-1", "demo-other-1"]
    assert "Skipping web: dependency 'db' is unhealthy" in capsys.readouterr().err


def test_up_does_not_attach_logs_after_a_failure(monkeypatch):
    services = {name: Service(name=name, image="fixture") for name in ("a", "b")}
    project = Project(name="demo", directory=".", services=services)
    _up_failure_fixture(monkeypatch, project, failing={"demo-a-1"})
    monkeypatch.setattr(cli, "_follow_logs", lambda *a, **k: pytest.fail("must not attach"))

    assert cli.cmd_up(cli.build_parser().parse_args(["up"])) == 1


# --- audit fixes (#41 #44 #46 #50 #51 #52) ---------------------------------------


def test_circular_depends_on_is_a_clean_error(tmp_path, capsys):
    compose = tmp_path / "compose.yaml"
    compose.write_text("services:\n  a: {image: x, depends_on: [b]}\n  b: {image: x, depends_on: [a]}\n")
    assert cli.main(["-f", str(compose), "--dry-run", "up", "-d"]) == 1
    assert "circular depends_on" in capsys.readouterr().err


def test_restart_tolerates_container_of_removed_service(monkeypatch):
    project = Project(name="demo", directory=".", services={"web": Service(name="web", image="x")})
    calls = []
    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(cli, "_project_containers", lambda *_, **__: [
        {"name": "demo-old-1", "service": "old", "running": True}])
    monkeypatch.setattr(cli.engine, "run", lambda args, **_: calls.append(args))
    monkeypatch.setattr(cli.engine, "run_retried", lambda args, **_: calls.append(args))
    monkeypatch.setattr(cli.host_gateway, "reconcile", lambda *a, **k: None)
    ns = SimpleNamespace(services=[], timeout=10, dry_run=False)
    assert cli._lifecycle(ns, "restart") == 0
    assert [c[0] for c in calls] == ["stop", "start"]


def test_scale_is_validated_before_anything_is_created(monkeypatch):
    services = {"a": Service(name="a", image="x"),
                "b": Service(name="b", image="x", container_name="fixed")}
    project = Project(name="demo", directory=".", services=services)
    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(cli.engine, "run", lambda *a, **k: pytest.fail("engine accessed"))
    monkeypatch.setattr(cli.engine, "ensure_network", lambda *a, **k: pytest.fail("network created"))
    with pytest.raises(ComposeError, match="container_name"):
        cli.cmd_up(cli.build_parser().parse_args(["up", "-d", "--scale", "b=2"]))
    with pytest.raises(ComposeError, match="no such service: typo"):
        cli.cmd_up(cli.build_parser().parse_args(["up", "-d", "--scale", "typo=2"]))


def test_up_does_not_wait_for_an_optional_dependency_that_failed(monkeypatch, capsys):
    migrate = Service(name="migrate", image="fixture")
    web = Service(name="web", image="fixture", depends_on=["migrate"],
                  depends_on_conditions={"migrate": "service_completed_successfully"},
                  depends_on_optional=["migrate"])
    project = Project(name="demo", directory=".", services={"migrate": migrate, "web": web})
    runs = _up_failure_fixture(monkeypatch, project, failing={"demo-migrate-1"})
    monkeypatch.setattr(cli.dependencies, "_wait_one", lambda *a, **k: pytest.fail("must not wait"))

    assert cli.cmd_up(cli.build_parser().parse_args(["up", "-d"])) == 1
    assert runs == ["demo-web-1"]
    assert "optional dependency 'migrate' failed to start; not waiting" in capsys.readouterr().err


def _env_file_fixture(monkeypatch, tmp_path, recorded):
    env_file = tmp_path / "app.env"
    env_file.write_text("A=1\n")
    service = Service(name="web", image="fixture", env_files=[str(env_file)])
    project = Project(name="demo", directory=".", services={"web": service})
    calls = []
    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(cli, "_project_containers", lambda *_, **__: [{
        "name": "demo-web-1", "service": "web", "index": 1, "running": True,
        "hash": service.config_hash(project), "local_config": recorded(env_file)}])
    monkeypatch.setattr(cli.engine, "run", lambda args, **_: calls.append(args[0]))
    monkeypatch.setattr(cli.host_gateway, "reconcile", lambda *a, **k: None)
    monkeypatch.setattr(cli.networking, "verify_container", lambda *a, **k: None)
    return env_file, calls


def test_up_recreates_when_an_env_file_changes(monkeypatch, tmp_path):
    from wslc_compose.model import local_config_digest
    env_file, calls = _env_file_fixture(monkeypatch, tmp_path, lambda f: local_config_digest(Service(name="web", image="fixture", env_files=[str(f)])))
    assert cli.cmd_up(cli.build_parser().parse_args(["up", "-d"])) == 0
    assert calls == []  # unchanged: up-to-date
    snapshot = local_config_digest(Service(name="web", image="fixture", env_files=[str(env_file)]))
    env_file.write_text("A=2\n")
    monkeypatch.setattr(cli, "_project_containers", lambda *_, **__: [{
        "name": "demo-web-1", "service": "web", "index": 1, "running": True,
        "hash": cli._load(None).services["web"].config_hash(cli._load(None)), "local_config": snapshot}])
    assert cli.cmd_up(cli.build_parser().parse_args(["up", "-d"])) == 0
    assert calls == ["stop", "remove", "run"]


def test_up_keeps_containers_created_before_env_file_tracking(monkeypatch, tmp_path):
    _, calls = _env_file_fixture(monkeypatch, tmp_path, lambda f: None)  # no label (<= 0.5.1)
    assert cli.cmd_up(cli.build_parser().parse_args(["up", "-d"])) == 0
    assert calls == []


def test_logs_include_stopped_containers_and_report_failures(monkeypatch):
    project = Project(name="demo", directory=".", services={"web": Service(name="web", image="x")})
    seen = {}
    monkeypatch.setattr(cli, "_load", lambda _: project)

    def containers(_, all_states=False):
        seen["all_states"] = all_states
        return [{"name": "demo-web-1", "service": "web", "running": False}]

    class Proc:
        stdout = iter(["boom\n"])
        def wait(self):
            return 3

    monkeypatch.setattr(cli, "_project_containers", containers)
    monkeypatch.setattr(cli.engine, "popen", lambda args, **_: Proc())
    ns = SimpleNamespace(services=[], follow=False, tail=None, timestamps=False)
    assert cli.cmd_logs(ns) == 3
    assert seen["all_states"] is True


@pytest.mark.parametrize("no_tty, isatty, expected", [
    (True, True, ["exec", "-i", "demo-db-1", "psql"]),
    (False, False, ["exec", "-i", "demo-db-1", "psql"]),
    (False, True, ["exec", "-i", "-t", "demo-db-1", "psql"]),
])
def test_exec_keeps_stdin_attached(monkeypatch, no_tty, isatty, expected):
    project = Project(name="demo", directory=".", services={"db": Service(name="db", image="x")})
    calls = []
    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: isatty, raising=False)
    monkeypatch.setattr(cli.engine, "run", lambda args, **_: calls.append(args) or SimpleNamespace(returncode=0))
    ns = SimpleNamespace(service="db", index=1, no_tty=no_tty, user=None, workdir=None, env=[],
                         command=["psql"], dry_run=False)
    assert cli.cmd_exec(ns) == 0
    assert calls == [expected]
