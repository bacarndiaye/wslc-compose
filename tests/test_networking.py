import copy
import subprocess

import pytest

from wslc_compose import LABEL_CREATE_OPERATION, LABEL_PROJECT, cli, engine, networking
from wslc_compose.engine import WslcError
from wslc_compose.model import Network, Project, Service


def desired_service():
    return Service(name="web", image="fixture", networks=["front", "back"],
                   network_aliases={"front": ["public"], "back": ["private"]},
                   network_ipv4_addresses={"front": "10.1.0.10", "back": "10.2.0.20"})


def endpoint(service, network, running=False):
    address = service.network_ipv4_addresses.get(network)
    return {"Aliases": [service.name, *service.network_aliases.get(network, [])],
            "IPAMConfig": {"IPv4Address": address} if address else None,
            "IPAddress": address if running else ""}


class Runtime:
    """A stateful CLI double: start observes whether both endpoints were attached beforehand."""

    def __init__(self, monkeypatch, failure=None):
        self.calls = []
        self.data = None
        self.failure = failure
        self.at_start = None
        monkeypatch.setattr(engine, "run", self.run)
        monkeypatch.setattr(engine, "run_retried", self.run)
        monkeypatch.setattr(engine, "inspect", lambda _: copy.deepcopy(self.data))

    def run(self, args, **kwargs):
        self.calls.append(args)
        if kwargs.get("dry_run"):
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "create":
            labels = dict(args[i + 1].split("=", 1) for i, value in enumerate(args) if value == "-l")
            self.data = {"Id": "owned-id", "Config": {"Labels": labels},
                         "HostConfig": {"NetworkMode": "front"}, "State": {"Running": False},
                         "NetworkSettings": {"Networks": {"front": endpoint(desired_service(), "front")}}}
            if self.failure == "create":
                raise WslcError("create failed after committing")
            if self.failure == "foreign":
                self.data["Config"]["Labels"][LABEL_CREATE_OPERATION] = "another-operation"
        elif args[:2] == ["network", "connect"]:
            if self.failure != "ignored-connect":
                self.data["NetworkSettings"]["Networks"]["back"] = endpoint(desired_service(), "back")
            if self.failure == "connect":
                raise WslcError("connect failed after committing")
            if self.failure == "interrupt":
                raise KeyboardInterrupt()
        elif args[0] == "start":
            self.at_start = copy.deepcopy(self.data["NetworkSettings"]["Networks"])
            self.data["State"]["Running"] = True
            self.data["NetworkSettings"]["Networks"] = {
                name: endpoint(desired_service(), name, running=True) for name in self.at_start}
            if self.failure == "wrong-live-ip":
                self.data["NetworkSettings"]["Networks"]["back"]["IPAddress"] = "10.2.0.99"
            if self.failure in ("start", "cleanup"):
                raise WslcError("start failed")
        elif args[0] == "remove":
            if self.failure == "cleanup":
                raise WslcError("remove failed")
            assert args[-1] == self.data["Id"]
            self.data = None
        return subprocess.CompletedProcess(args, 0, "", "")


def test_every_endpoint_is_ready_before_entrypoint_and_scoped_flags_do_not_leak(monkeypatch):
    runtime = Runtime(monkeypatch)
    service = desired_service()
    networking.create_and_start(Project(name="demo", directory="."), service, 1, False)

    assert set(runtime.at_start) == {"front", "back"}
    assert runtime.at_start["back"]["IPAMConfig"]["IPv4Address"] == "10.2.0.20"
    create, connect, start = runtime.calls
    assert create[0] == "create" and "-d" not in create
    assert "public" in create and "private" not in create
    assert "10.1.0.10" in create and "10.2.0.20" not in create
    assert "private" in connect and "public" not in connect
    assert "10.2.0.20" in connect and "10.1.0.10" not in connect
    assert start == ["start", "owned-id"]


@pytest.mark.parametrize("failure", ["create", "connect", "ignored-connect", "start", "wrong-live-ip"])
def test_partial_creation_is_removed_after_verified_ownership(monkeypatch, failure):
    runtime = Runtime(monkeypatch, failure)
    with pytest.raises(WslcError):
        networking.create_and_start(Project(name="demo", directory="."), desired_service(), 1, False)
    assert runtime.data is None
    assert runtime.calls[-1] == ["remove", "-f", "-v", "owned-id"]
    if failure in ("create", "connect", "ignored-connect"):
        assert runtime.at_start is None


def test_keyboard_interrupt_also_cleans_the_partial_container(monkeypatch):
    runtime = Runtime(monkeypatch, "interrupt")
    with pytest.raises(KeyboardInterrupt):
        networking.create_and_start(Project(name="demo", directory="."), desired_service(), 1, False)
    assert runtime.data is None


def test_foreign_same_named_container_is_never_removed(monkeypatch):
    runtime = Runtime(monkeypatch, "foreign")
    with pytest.raises(WslcError, match="ownership"):
        networking.create_and_start(Project(name="demo", directory="."), desired_service(), 1, False)
    assert runtime.data is not None
    assert not any(args[0] == "remove" for args in runtime.calls)


def test_cleanup_failure_is_reported_alongside_start_failure(monkeypatch):
    runtime = Runtime(monkeypatch, "cleanup")
    with pytest.raises(WslcError, match="start failed; cleanup.*remove failed"):
        networking.create_and_start(Project(name="demo", directory="."), desired_service(), 1, False)
    assert runtime.data is not None


def test_multi_network_dry_run_prints_complete_sequence_without_inspect(monkeypatch):
    runtime = Runtime(monkeypatch)
    monkeypatch.setattr(engine, "inspect", lambda _: pytest.fail("dry run inspected a nonexistent container"))
    networking.create_and_start(Project(name="demo", directory="."), desired_service(), 1, True)
    assert [args[:2] for args in runtime.calls] == [
        ["create", "--name"], ["network", "connect"], ["start", "demo-web-1"]]


@pytest.mark.parametrize("drift", [None, "missing", "alias", "requested-ip", "live-ip", "removed-ip", "primary"])
def test_all_observed_endpoints_are_reconciled_including_secondary_and_primary_order(drift):
    service = desired_service()
    current = {"name": "demo-web-1", "running": True, "network_mode": "front", "networks": {
        name: endpoint(service, name, running=True) for name in service.networks}}
    if drift == "missing":
        del current["networks"]["back"]
    elif drift == "alias":
        current["networks"]["back"]["Aliases"] = ["web"]
    elif drift == "requested-ip":
        current["networks"]["back"]["IPAMConfig"]["IPv4Address"] = "10.2.0.99"
    elif drift == "live-ip":
        current["networks"]["back"]["IPAddress"] = "10.2.0.99"
    elif drift == "removed-ip":
        service.network_ipv4_addresses.pop("back")
    elif drift == "primary":
        current["network_mode"] = "back"
    assert networking.configuration_matches(service, current) is (drift is None)


def test_existing_multi_network_up_is_non_disruptive(monkeypatch):
    service = desired_service()
    project = Project(name="demo", directory=".", services={"web": service})
    current = {"name": "demo-web-1", "service": "web", "index": 1,
               "running": True, "hash": service.config_hash(project), "network_mode": "front",
               "networks": {name: endpoint(service, name, True) for name in service.networks}}
    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(cli, "_project_containers", lambda *_, **__: [current])
    monkeypatch.setattr(engine, "run", lambda *_, **__: pytest.fail("up mutated a compatible container"))
    assert cli.cmd_up(cli.build_parser().parse_args(["up", "-d"])) == 0


def test_up_uses_native_create_connect_start_for_new_multi_network_service(monkeypatch):
    runtime = Runtime(monkeypatch)
    service = desired_service()
    project = Project(name="demo", directory=".", services={"web": service})
    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(cli, "_project_containers", lambda *_, **__: [])
    assert cli.cmd_up(cli.build_parser().parse_args(["up", "-d"])) == 0
    assert set(runtime.at_start) == {"front", "back"}


def test_duplicate_static_ips_fail_before_any_engine_access(monkeypatch):
    first, second = desired_service(), desired_service()
    second.name = "other"
    project = Project(name="demo", directory=".", services={"web": first, "other": second})
    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(engine, "run", lambda *_, **__: pytest.fail("conflict reached the engine"))
    from wslc_compose.loader import ComposeError
    with pytest.raises(ComposeError, match="same static"):
        cli.cmd_up(cli.build_parser().parse_args(["up", "-d"]))


def network_inspect():
    return {"Name": "front", "Id": "network-id", "Driver": "bridge", "Internal": False,
            "Labels": {"purpose": "test", LABEL_PROJECT: "demo"}, "Options": {"mtu": "1400"},
            "IPAM": {"Config": [{"Subnet": "10.1.0.0/24", "Gateway": "10.1.0.1", "IPRange": "10.1.0.128/25"}]}}


@pytest.mark.parametrize("drift", [None, "subnet", "gateway", "range", "driver", "options", "labels", "internal"])
def test_network_creation_settings_are_verified_read_only(monkeypatch, drift):
    network = Network(key="front", name="front", driver="bridge", driver_opts={"mtu": "1400"},
                      labels={"purpose": "test"}, subnet="10.1.0.0/24", gateway="10.1.0.1", ip_range="10.1.0.128/25")
    data = network_inspect()
    if drift in ("subnet", "gateway", "range"):
        data["IPAM"]["Config"][0][{"subnet": "Subnet", "gateway": "Gateway", "range": "IPRange"}[drift]] = "different"
    elif drift == "driver":
        data["Driver"] = "other"
    elif drift == "options":
        data["Options"]["mtu"] = "1500"
    elif drift == "labels":
        data["Labels"]["purpose"] = "other"
    elif drift == "internal":
        data["Internal"] = True
    monkeypatch.setattr(engine, "capture_json", lambda _: [data])
    if drift:
        with pytest.raises(WslcError, match="conflicts"):
            networking.verify_network(network)
    else:
        assert networking.verify_network(network) is data


def test_ipam_conflict_aborts_up_before_touching_existing_workloads(monkeypatch):
    project = Project(name="demo", directory=".", services={"web": desired_service()},
                      networks={"front": Network(key="front", name="front", subnet="10.9.0.0/24")})
    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(engine, "ensure_network", lambda *_, **__: False)
    monkeypatch.setattr(engine, "capture_json", lambda _: [network_inspect()])
    monkeypatch.setattr(engine, "run", lambda *_, **__: pytest.fail("IPAM conflict mutated a workload"))
    with pytest.raises(WslcError, match="ipam.subnet"):
        cli.cmd_up(cli.build_parser().parse_args(["up", "-d"]))


@pytest.mark.parametrize("address", ["10.9.0.10", "10.1.0.0", "10.1.0.1", "10.1.0.255"])
def test_external_network_allocation_is_checked_before_static_attachment(address):
    service = Service(name="web", networks=["front"], network_ipv4_addresses={"front": address})
    with pytest.raises(WslcError, match="not usable"):
        networking.validate_static_addresses(service, {"front": network_inspect()})


def test_static_address_outside_dynamic_pool_is_valid():
    service = Service(name="web", networks=["front"], network_ipv4_addresses={"front": "10.1.0.10"})
    networking.validate_static_addresses(service, {"front": network_inspect()})


def test_down_preserves_external_and_unverified_networks(monkeypatch):
    project = Project(name="demo", directory=".", networks={
        "external": Network(key="external", name="external", external=True),
        "front": Network(key="front", name="front")})
    calls = []
    monkeypatch.setattr(cli, "_load", lambda _: project)
    monkeypatch.setattr(cli, "_project_containers", lambda *_, **__: [])
    monkeypatch.setattr(engine, "network_names", lambda: ["front", "external"])
    data = network_inspect()
    data["Labels"] = {}
    monkeypatch.setattr(engine, "capture_json", lambda _: [data])
    monkeypatch.setattr(engine, "run", lambda args, **_: calls.append(args))
    assert cli.cmd_down(cli.build_parser().parse_args(["down"])) == 0
    assert calls == []


def test_unavailable_inspect_never_authorizes_partial_container_removal(monkeypatch):
    runtime = Runtime(monkeypatch)
    monkeypatch.setattr(engine, "inspect", lambda _: None)
    with pytest.raises(WslcError, match="ownership"):
        networking.create_and_start(Project(name="demo", directory="."), desired_service(), 1, False)
    assert runtime.data is not None
    assert not any(args[0] == "remove" for args in runtime.calls)
