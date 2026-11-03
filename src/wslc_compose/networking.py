"""Network resource verification and create/connect/start orchestration."""

from __future__ import annotations

import uuid
from ipaddress import IPv4Address, IPv4Network

from wslc_compose import (
    LABEL_CREATE_OPERATION,
    LABEL_INDEX,
    LABEL_PROJECT,
    LABEL_SERVICE,
    engine,
    flags,
)
from wslc_compose.engine import WslcError
from wslc_compose.model import Network, Project, Service


def inspect_network(name: str) -> dict:
    records = engine.capture_json(["network", "inspect", name])
    if len(records) != 1 or not isinstance(records[0], dict) or records[0].get("Name") != name:
        raise WslcError(f"network {name!r}: inspect did not return a usable network identity")
    return records[0]


def verify_network(network: Network) -> dict:
    """Existing resources are never rewritten to satisfy changed creation settings."""
    actual = inspect_network(network.name)
    conflicts = []
    if network.driver and actual.get("Driver") != network.driver:
        conflicts.append("driver")
    if (not network.external or network.internal) and actual.get("Internal") != network.internal:
        conflicts.append("internal")
    for key, value in network.driver_opts.items():
        if (actual.get("Options") or {}).get(key) != value:
            conflicts.append(f"driver_opts.{key}")
    for key, value in network.labels.items():
        if (actual.get("Labels") or {}).get(key) != value:
            conflicts.append(f"labels.{key}")
    if network.subnet:
        records = (actual.get("IPAM") or {}).get("Config") or []
        allocation = next((record for record in records if record.get("Subnet") == network.subnet), None)
        if allocation is None:
            conflicts.append("ipam.subnet")
        else:
            if network.gateway and allocation.get("Gateway") != network.gateway:
                conflicts.append("ipam.gateway")
            if network.ip_range and allocation.get("IPRange") != network.ip_range:
                conflicts.append("ipam.ip_range")
    if conflicts:
        raise WslcError(f"network {network.name!r} conflicts with the requested settings: "
                        + ", ".join(conflicts) + "; use another name or remove the unused network explicitly")
    return actual


def validate_static_addresses(service: Service, networks: dict[str, dict]) -> None:
    for name, value in service.network_ipv4_addresses.items():
        if name not in networks:  # A dry run may describe a network that does not exist yet.
            continue
        address = IPv4Address(value)
        records = (networks[name].get("IPAM") or {}).get("Config") or []
        usable = False
        for record in records:
            try:
                subnet = IPv4Network(record["Subnet"])
            except (KeyError, ValueError):
                continue
            if (address in subnet and address not in (subnet.network_address, subnet.broadcast_address)
                    and value != record.get("Gateway")):
                usable = True
        if not usable:
            raise WslcError(f"service {service.name!r}: static IPv4 {value} is not usable in network {name!r}")


def configuration_matches(service: Service, current: dict) -> bool:
    if service.network_mode or not service.networks:
        return True
    networks = current.get("networks")
    if not isinstance(networks, dict):
        if len(service.networks) > 1 or service.network_ipv4_addresses:
            raise WslcError(f"cannot verify network settings for {current['name']!r}")
        return True
    primary = current.get("network_mode")
    if primary and primary != service.networks[0]:
        return False
    for name in service.networks:
        endpoint = networks.get(name)
        if not isinstance(endpoint, dict):
            return False
        actual_aliases = endpoint.get("Aliases") or []
        ipam = endpoint.get("IPAMConfig") or {}
        if not isinstance(actual_aliases, list) or not isinstance(ipam, dict):
            raise WslcError(f"cannot verify endpoint settings for network {name!r}")
        aliases = set(service.network_aliases.get(name, []) + [service.name])
        if not aliases.issubset(actual_aliases):
            return False
        desired = service.network_ipv4_addresses.get(name)
        requested = ipam.get("IPv4Address") or None
        if desired != requested:
            return False
        if desired and current["running"] and endpoint.get("IPAddress") != desired:
            return False
    return True


def _current(data: dict, name: str) -> dict:
    return {"name": name, "running": bool((data.get("State") or {}).get("Running")),
            "network_mode": (data.get("HostConfig") or {}).get("NetworkMode"),
            "networks": (data.get("NetworkSettings") or {}).get("Networks")}


def verify_container(service: Service, name: str, dry_run: bool = False) -> None:
    if dry_run or (len(service.networks) < 2 and not service.network_ipv4_addresses):
        return
    data = engine.inspect(name)
    if not data or not configuration_matches(service, _current(data, name)):
        raise WslcError(f"container {name!r} did not apply the requested network aliases/static IPv4 settings")


def _owned(data: dict, project: Project, service: Service, index: int, operation: str) -> bool:
    labels = (data.get("Config") or {}).get("Labels") or data.get("Labels") or {}
    return bool(data.get("Id")) and all(labels.get(key) == value for key, value in (
        (LABEL_PROJECT, project.name), (LABEL_SERVICE, service.name),
        (LABEL_INDEX, str(index)), (LABEL_CREATE_OPERATION, operation)))


def create_and_start(project: Project, service: Service, index: int, dry_run: bool) -> None:
    """All endpoints exist before the entrypoint runs. Cleanup only this operation's container."""
    name = project.container_name(service, index)
    operation = uuid.uuid4().hex
    identifier = None
    try:
        engine.run(flags.run_args(project, service, index, path_mapper=engine.to_host_path,
                                  create=True, operation=operation), capture=True, dry_run=dry_run)
        if not dry_run:
            created = engine.inspect(name)
            if not created or not _owned(created, project, service, index, operation):
                raise WslcError(f"created container {name!r} ownership could not be verified")
            identifier = created["Id"]
        for network in service.networks[1:]:
            engine.run(flags.network_connect_args(service, network, identifier or name),
                       capture=True, dry_run=dry_run)
        verify_container(service, identifier or name, dry_run)
        engine.run_retried(["start", identifier or name], dry_run=dry_run)
        verify_container(service, identifier or name, dry_run)
    except BaseException as failure:
        if not dry_run:
            data = engine.inspect(name)
            if data and _owned(data, project, service, index, operation):
                if identifier is not None and data["Id"] != identifier:
                    raise WslcError(f"partial container {name!r} changed identity; cleanup was not applied") from failure
                try:
                    engine.run(["remove", "-f", "-v", data["Id"]], capture=True)
                except WslcError as cleanup:
                    raise WslcError(f"{failure}; cleanup of partial container {name!r} failed: {cleanup}") from failure
        raise
