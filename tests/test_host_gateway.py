import copy
import hashlib
import subprocess

import pytest

from wslc_compose import LABEL_PROJECT, LABEL_SERVICE, cli, engine, host_gateway
from wslc_compose.engine import WslcError
from wslc_compose.model import PortMapping, Project, Service


def fixture():
    client = Service(name="keycloak", networks=["net"], extra_hosts=["host.docker.internal:host-gateway"])
    server = Service(name="oracle", networks=["net"], ports=[PortMapping(1521, "1521")])
    project = Project(name="demo", directory=".", services={"keycloak": client, "oracle": server})
    data = {"Id": "server-id", "Config": {"Labels": {LABEL_PROJECT: "demo", LABEL_SERVICE: "oracle"}},
            "State": {"Running": True}, "HostConfig": {"NetworkMode": "net"},
            "NetworkSettings": {"Networks": {"net": {"Gateway": "172.18.0.1", "IPAddress": "172.18.0.3"}}},
            "Ports": {"1521/tcp": [{"HostIp": "127.0.0.1", "HostPort": "1521"}]}}
    return project, data


def test_publication_maps_host_port_without_changing_application_environment():
    project, data = fixture()
    before = copy.deepcopy(project)
    assert host_gateway.mappings(project, [data]) == [("172.18.0.1", "tcp", 1521, "172.18.0.3", 1521)]
    assert project == before


def test_gateway_can_belong_to_secondary_network_when_primary_is_internal():
    project, data = fixture()
    project.services["keycloak"].networks = ["internal", "net"]
    assert host_gateway.mappings(project, [data])[0][0] == "172.18.0.1"


@pytest.mark.parametrize("published", ["127.0.0.1:1521", "192.168.1.2:1521", "2521"])
def test_explicit_interface_bindings_and_pending_port_changes_are_not_widened(published):
    project, data = fixture()
    project.services["oracle"].ports[0].published = published
    assert host_gateway.mappings(project, [data]) == []


@pytest.mark.parametrize("change", ["foreign", "stopped", "unknown-service"])
def test_only_running_owned_project_publications_are_forwarded(change):
    project, data = fixture()
    if change == "foreign":
        data["Config"]["Labels"][LABEL_PROJECT] = "other"
    elif change == "stopped":
        data["State"]["Running"] = False
    else:
        data["Config"]["Labels"][LABEL_SERVICE] = "foreign"
    assert host_gateway.mappings(project, [data]) == []


def test_script_matches_shared_vector_and_keeps_firewall_policy_before_compatibility():
    commands = host_gateway.script("demo", [("172.18.0.1", "tcp", 1521, "172.18.0.3", 1521)])
    assert hashlib.sha256(commands.encode()).hexdigest() == "6f67791185af72f734ba636753abe3298edefddb023233a021fc2c62ad495707"
    assert "--ctorigdst 172.18.0.1 --ctorigdstport 1521" in commands
    assert "-j DOCKER-USER" in commands
    assert "--noflush" in commands
    assert "-F DOCKER" not in commands
    assert "-P FORWARD" not in commands


def test_cleanup_only_removes_project_owned_chains_and_jumps():
    commands = host_gateway.script("demo", [], cleanup=True)
    assert "exit 71" in commands  # Reject an existing chain without our owner marker.
    assert "-X WCGP_" in commands and "-X WCGS_" in commands and "-X WCGF_" in commands
    assert "iptables-restore --wait" not in commands
    assert "-X DOCKER" not in commands


def test_stopped_and_recreated_publications_refresh_destination():
    project, data = fixture()
    other = copy.deepcopy(data)
    data["State"]["Running"] = False
    other["Id"] = "replacement"
    other["NetworkSettings"]["Networks"]["net"]["IPAddress"] = "172.18.0.44"
    assert host_gateway.mappings(project, [data, other])[0][-2:] == ("172.18.0.44", 1521)


def test_conflicting_publications_fail_before_changing_runtime_rules():
    project, data = fixture()
    other = copy.deepcopy(data)
    other["NetworkSettings"]["Networks"]["net"]["IPAddress"] = "172.18.0.44"
    with pytest.raises(WslcError, match="ambiguous"):
        host_gateway.mappings(project, [data, other])


@pytest.mark.parametrize("gateway, protocol, host_port, address, port", [
    ("172.18.0.1;exit", "tcp", 1521, "172.18.0.3", 1521),
    ("172.18.0.1", "tcp;exit", 1521, "172.18.0.3", 1521),
    ("172.18.0.1", "tcp", 0, "172.18.0.3", 1521),
    ("172.18.0.1", "tcp", 1521, "172.18.0.3", 65536),
])
def test_runtime_rule_parameters_cannot_inject_shell_commands(gateway, protocol, host_port, address, port):
    with pytest.raises((ValueError, WslcError)):
        host_gateway.script("demo", [(gateway, protocol, host_port, address, port)])


def test_projects_without_host_gateway_do_not_require_session_privileges(monkeypatch):
    project = Project(name="demo", directory=".")
    monkeypatch.setattr(engine, "run_session_script", lambda *_, **__: pytest.fail("unexpected session command"))
    host_gateway.reconcile(project)


def test_removing_host_gateway_cleans_previous_runtime_marker_without_reading_targets(monkeypatch):
    project = Project(name="demo", directory=".")
    calls = []
    monkeypatch.setattr(engine, "list_project_containers", lambda *_, **__: pytest.fail("cleanup should not inspect targets"))
    monkeypatch.setattr(engine, "run_session_script", lambda commands, **_: calls.append(commands))
    host_gateway.reconcile(project, previously_enabled=True)
    assert "-X WCGP_" in calls[0] and "-X WCGF_" in calls[0]


def test_session_api_gets_valid_redirected_stdin_instead_of_invalid_windows_handle(monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "find_wslc", lambda: "native-wslc.exe")
    monkeypatch.setattr(engine.subprocess, "run", lambda argv, **kwargs:
                        calls.append((argv, kwargs)) or subprocess.CompletedProcess(argv, 0, "", ""))
    engine.run_session_script("true")
    assert calls[0][0] == ["native-wslc.exe", "system", "session", "run", "sh", "-c", "true"]
    assert calls[0][1]["input"] == ""


def test_start_includes_stopped_containers_and_refreshes_extra_hosts(monkeypatch):
    service = Service(name="web", image="fixture", extra_hosts=["db:10.0.0.3"])
    project = Project(name="demo", directory=".", services={"web": service})
    states, starts, hosts = [], [], []
    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(cli, "_project_containers", lambda _, all_states=False: states.append(all_states) or [
        {"name": "demo-web-1", "service": "web", "running": False}])
    monkeypatch.setattr(engine, "run_retried", lambda args, **_: starts.append(args))
    monkeypatch.setattr(cli, "_apply_extra_hosts", lambda *args: hosts.append(args))
    assert cli._lifecycle(cli.build_parser().parse_args(["start"]), "start") == 0
    assert states == [True]
    assert starts == [["start", "demo-web-1"]]
    assert hosts == [("demo-web-1", ["db:10.0.0.3"], False)]


@pytest.mark.parametrize("code, text", [
    (71, "different owner"),
    (72, "DOCKER-USER"),
    (1, "iptables, iptables-restore and flock"),
])
def test_silent_session_failure_gets_an_actionable_message(monkeypatch, code, text):
    import subprocess
    monkeypatch.setattr(engine, "find_wslc", lambda: "wslc")
    monkeypatch.setattr(engine.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, code, "", ""))
    with pytest.raises(WslcError, match=text) as info:
        engine.run_session_script("true")
    assert f"exit code {code}" in str(info.value)
