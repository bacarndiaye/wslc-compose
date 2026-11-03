"""Compose host-gateway access to published ports inside the WSLC session VM."""

from __future__ import annotations

import hashlib
import ipaddress

from wslc_compose import LABEL_PROJECT, LABEL_SERVICE, engine
from wslc_compose.engine import WslcError


def requested(project) -> bool:
    return any(entry.partition(":")[2].strip().lower() == "host-gateway"
               for service in getattr(project, "services", {}).values() for entry in service.extra_hosts)


def _labels(data: dict) -> dict:
    return (data.get("Config") or {}).get("Labels") or data.get("Labels") or {}


def mappings(project, inspections: list[dict]) -> list[tuple]:
    owned = [data for data in inspections
             if _labels(data).get(LABEL_PROJECT) == project.name
             and (data.get("State") or {}).get("Running") is True]
    client_networks = {network for service in project.services.values()
                       if service.networks and not service.network_mode
                       and any(entry.partition(":")[2].strip().lower() == "host-gateway"
                               for entry in service.extra_hosts) for network in service.networks}
    gateways = set()
    for data in owned:
        for name, endpoint in ((data.get("NetworkSettings") or {}).get("Networks") or {}).items():
            if name in client_networks and endpoint.get("Gateway"):
                gateways.add(str(ipaddress.IPv4Address(endpoint["Gateway"])))
    routes = {}
    for data in owned:
        service = project.services.get(_labels(data).get(LABEL_SERVICE))
        if service is None:
            continue
        networks = (data.get("NetworkSettings") or {}).get("Networks") or {}
        primary = (data.get("HostConfig") or {}).get("NetworkMode")
        endpoint = networks.get(primary) or next(iter(networks.values()), {})
        if not endpoint.get("IPAddress"):
            continue
        address = str(ipaddress.IPv4Address(endpoint["IPAddress"]))
        for port in service.ports:
            published = port.published
            if published and ":" in published:
                binding, published = published.rsplit(":", 1)
                if binding not in ("", "0.0.0.0"):
                    continue  # Explicit loopback/interface bindings are not widened.
            protocol = port.protocol.lower()
            if protocol not in ("tcp", "udp"):
                raise WslcError("host-gateway supports TCP/UDP IPv4 publications only")
            for binding in (data.get("Ports") or {}).get(f"{port.target}/{protocol}", []):
                host_port = int(binding["HostPort"])
                if published and host_port != int(published):
                    continue  # The desired port edit has not been applied to this instance yet.
                for gateway in gateways:
                    key = (gateway, protocol, host_port)
                    value = (address, port.target)
                    if key in routes and routes[key] != value:
                        raise WslcError("host-gateway publication is ambiguous")
                    routes[key] = value
    return sorted((*key, *value) for key, value in routes.items())


def script(project_name: str, routes: list[tuple], cleanup: bool = False) -> str:
    digest = hashlib.sha256(project_name.encode("utf-8")).hexdigest()
    incoming, outgoing, forward = "WCGP_" + digest[:12], "WCGS_" + digest[:12], "WCGF_" + digest[:12]
    owner = "wslc-compose-host-gateway-" + digest
    lines = ["set -eu", "command -v iptables >/dev/null", "command -v iptables-restore >/dev/null"]
    # A kernel lock serializes compatible frontends reconciling the same project.
    lines += [f"exec 9>/run/{owner}.lock", "flock -w 10 9"]
    for chain, table, parent in ((incoming, "nat", "PREROUTING"), (outgoing, "nat", "POSTROUTING"),
                                 (forward, "filter", "FORWARD")):
        lines += [f"if iptables -w -t {table} -S {chain} >/dev/null 2>&1; then",
                  f"  iptables -w -t {table} -S {chain} | grep -F -- {owner} >/dev/null || exit 71"]
        if cleanup:
            lines += [f"  while iptables -w -t {table} -C {parent} -j {chain} 2>/dev/null; do",
                      f"    iptables -w -t {table} -D {parent} -j {chain}",
                      "  done", f"  iptables -w -t {table} -F {chain}", f"  iptables -w -t {table} -X {chain}", "fi"]
        else:
            lines += ["else", f"  iptables -w -t {table} -N {chain}",
                      f"  iptables -w -t {table} -A {chain} -m comment --comment {owner} -j RETURN", "fi"]
    if cleanup:
        return ("\n".join(lines) + "\n").replace("iptables -w -t", "iptables -w 10 -t")
    rules = ["*nat", f"-F {incoming}", f"-F {outgoing}"]
    filtering = ["*filter", f"-F {forward}"]
    for gateway, protocol, host_port, address, port in routes:
        gateway, address = str(ipaddress.IPv4Address(gateway)), str(ipaddress.IPv4Address(address))
        if protocol not in ("tcp", "udp") or not 1 <= host_port <= 65535 or not 1 <= port <= 65535:
            raise WslcError("invalid host-gateway mapping")
        rules += [f"-A {incoming} -d {gateway}/32 -p {protocol} --dport {host_port} -j DNAT --to-destination {address}:{port}",
                  f"-A {outgoing} -d {address}/32 -p {protocol} --dport {port} -m conntrack --ctorigdst {gateway} --ctorigdstport {host_port} -j MASQUERADE"]
        filtering += [f"-A {forward} -d {address}/32 -p {protocol} --dport {port} -m conntrack --ctdir ORIGINAL --ctorigdst {gateway} --ctorigdstport {host_port} -j ACCEPT",
                      f"-A {forward} -s {address}/32 -p {protocol} --sport {port} -m conntrack --ctstate ESTABLISHED --ctdir REPLY --ctorigdst {gateway} --ctorigdstport {host_port} -j ACCEPT"]
    rules += [f"-A {incoming} -m comment --comment {owner} -j RETURN",
              f"-A {outgoing} -m comment --comment {owner} -j RETURN", "COMMIT"]
    rules += filtering + [f"-A {forward} -m comment --comment {owner} -j RETURN", "COMMIT"]
    lines += ["iptables-restore --wait 10 --noflush <<'WCG_RULES'", *rules, "WCG_RULES"]
    for table, chain in (("PREROUTING", incoming), ("POSTROUTING", outgoing)):
        lines += [f"iptables -w -t nat -C {table} -j {chain} 2>/dev/null || iptables -w -t nat -I {table} 1 -j {chain}"]
    lines += [f"if ! iptables -w -t filter -C FORWARD -j {forward} 2>/dev/null; then",
              "  position=$(iptables -w -t filter -S FORWARD | awk '$1 == \"-A\" {i++; if ($0 == \"-A FORWARD -j DOCKER-USER\") {print i+1; exit}}')",
              "  [ -n \"$position\" ] || exit 72",
              f"  iptables -w -t filter -I FORWARD \"$position\" -j {forward}", "fi"]
    return ("\n".join(lines) + "\n").replace("iptables -w -t", "iptables -w 10 -t")


def reconcile(project, dry_run: bool = False, cleanup: bool = False, previously_enabled: bool = False) -> None:
    enabled = requested(project)
    if not enabled and not previously_enabled:
        return
    cleanup = cleanup or not enabled
    records = []
    if not cleanup:
        for entry in engine.list_project_containers(project.name, all_states=True):
            identifier = entry.get("ID") or entry.get("Id") or entry.get("Names") or entry.get("Name")
            data = engine.inspect(identifier)
            if data is None:
                raise WslcError("cannot verify host-gateway publication inventory")
            records.append(data)
    commands = script(project.name, mappings(project, records), cleanup)
    engine.run_session_script(commands, dry_run=dry_run)
