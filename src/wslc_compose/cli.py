"""wslc-compose command line interface."""

from __future__ import annotations

import argparse
import dataclasses
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional

import yaml

from wslc_compose import (
    LABEL_CONFIG_HASH,
    LABEL_HOST_GATEWAY,
    LABEL_INDEX,
    LABEL_LOCAL_CONFIG,
    LABEL_PROJECT,
    LABEL_SERVICE,
    __version__,
    dependencies,
    engine,
    flags,
    host_gateway,
    networking,
)
from wslc_compose.engine import WslcError
from wslc_compose.loader import ComposeError, find_compose_file, load_project
from wslc_compose.metadata import resource_labels
from wslc_compose.model import Project, Service, local_config_digest

PROTOCOLS = {6: "tcp", 17: "udp"}


def _err(message: str) -> None:
    print(f"wslc-compose: {message}", file=sys.stderr)


def _info(message: str) -> None:
    print(message)


# --- project loading -------------------------------------------------------


def _split_files(values: List[str]) -> List[str]:
    """Flatten `-f` values; each may hold several paths separated like COMPOSE_FILE."""
    sep = os.environ.get("COMPOSE_PATH_SEPARATOR") or os.pathsep
    files = []
    for value in values:
        if os.path.isfile(value):  # a real path wins over splitting (e.g. 'C:\\x.yml')
            files.append(value)
        else:
            files.extend(part for part in value.split(sep) if part)
    return files


def _locate_compose_files(explicit: Optional[List[str]]) -> List[str]:
    if not explicit and os.environ.get("COMPOSE_FILE"):
        explicit = [os.environ["COMPOSE_FILE"]]
    if explicit:
        files = _split_files(explicit)
        for path in files:
            if not os.path.isfile(path):
                raise ComposeError(f"compose file not found: {path}")
        return files
    directory = os.getcwd()
    while True:
        found = find_compose_file(directory)
        if found:
            return [found]
        parent = os.path.dirname(directory)
        if parent == directory:
            raise ComposeError(
                "no compose file found (looked for compose.yaml / compose.yml / "
                "docker-compose.yml / docker-compose.yaml in this directory and parents)"
            )
        directory = parent


def _load(ns: argparse.Namespace) -> Project:
    compose_files = _locate_compose_files(ns.file)
    project = load_project(compose_files, project_name=ns.project_name, env_file=ns.env_file)
    for warning in project.warnings:
        _err(f"warning: {warning}")
    return project


def _runtime_options_warnings(services: List[Service]) -> List[str]:
    """wslc < 3.0.5 has no --restart / --cap-*: say so instead of silently ignoring them."""
    supported = engine.run_options()
    messages = []
    for svc in services:
        if svc.restart == "always" and "--restart" not in supported:
            messages.append(f"{svc.name}: 'restart: always' needs wslc >= 3.0.5; ignoring it")
        if (svc.cap_add or svc.cap_drop) and "--cap-add" not in supported:
            messages.append(f"{svc.name}: cap_add/cap_drop need wslc >= 3.0.5; ignoring them")
    return messages


def _runtime_matches(svc: Service, current: dict) -> bool:
    """Restart policy and capabilities are reconciled against inspect (outside the shared hash).

    Only options the installed wslc supports are compared, so an older wslc never
    causes a recreate loop.
    """
    supported = engine.run_options()
    if "--restart" in supported:
        actual = current.get("restart_policy")
        if ("always" if svc.restart == "always" else "no") != (actual if actual == "always" else "no"):
            return False
    if "--cap-add" in supported:
        if sorted(svc.cap_add) != sorted(_normalized_caps(current.get("cap_add"))):
            return False
        if sorted(svc.cap_drop) != sorted(_normalized_caps(current.get("cap_drop"))):
            return False
    return True


def _local_config_matches(svc: Service, current: dict) -> bool:
    """Settings outside the shared hash (env_file contents, mount options...) vs the
    container's label. Containers created before 0.6.0 have no label: assumed to match,
    so upgrading never recreates anything by itself."""
    recorded = current.get("local_config")
    return recorded is None or recorded == local_config_digest(svc)


def _normalized_caps(values) -> List[str]:
    names = []
    for value in values or []:
        name = str(value).upper()
        name = name[4:] if name.startswith("CAP_") else name
        if name not in names:
            names.append(name)
    return names


def _select_services(
    project: Project, names: List[str], profiles: List[str]
) -> List[Service]:
    active = set(profiles or [])
    for name in names:
        if name not in project.services:
            raise ComposeError(f"no such service: {name}")
    try:
        services = project.sorted_services(names or None)
    except ValueError as exc:  # circular depends_on
        raise ComposeError(str(exc)) from None
    result = []
    for svc in services:
        if svc.profiles and not (set(svc.profiles) & active) and svc.name not in names:
            continue
        result.append(svc)
    return result


# --- container queries ------------------------------------------------------


def _project_containers(project: Project, all_states: bool = False) -> List[dict]:
    """list entries enriched with inspect data (labels, status)."""
    entries = engine.list_project_containers(project.name, all_states)
    enriched = []
    for entry in entries:
        container_id = entry.get("Id") or entry.get("ID")
        container_name = entry.get("Name") or entry.get("Names")
        identifier = container_id or container_name
        if not identifier:
            continue
        data = engine.inspect(identifier) or {}
        labels = data.get("Labels") or {}
        state = data.get("State") or {}
        config = data.get("Config") or {}
        command = entry.get("Command")
        if not command:
            command = " ".join(
                (config.get("Entrypoint") or []) + (config.get("Cmd") or [])
            )
        enriched.append(
            {
                "id": container_id,
                "name": container_name or data.get("Name"),
                "image": entry.get("Image") or data.get("Image"),
                "command": command or "",
                "service": labels.get(LABEL_SERVICE, ""),
                "index": int(labels.get(LABEL_INDEX, "1") or 1),
                "hash": labels.get(LABEL_CONFIG_HASH, ""),
                "running": bool(state.get("Running")),
                "created": _format_created(entry.get("CreatedAt") or data.get("Created") or ""),
                "status": entry.get("Status") or state.get("Status", "unknown"),
                "ports": entry.get("Ports") or [],
                "networks": (data.get("NetworkSettings") or {}).get("Networks"),
                "network_mode": (data.get("HostConfig") or {}).get("NetworkMode"),
                "host_gateway": labels.get(LABEL_HOST_GATEWAY) == "v1",
                "local_config": labels.get(LABEL_LOCAL_CONFIG),
                "restart_policy": ((data.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name"),
                "cap_add": (data.get("HostConfig") or {}).get("CapAdd"),
                "cap_drop": (data.get("HostConfig") or {}).get("CapDrop"),
            }
        )
    return enriched


def _format_ports(ports: List[dict]) -> str:
    if isinstance(ports, str):
        return ports
    parts = []
    for port in ports:
        proto = PROTOCOLS.get(port.get("Protocol"), str(port.get("Protocol", "")))
        addr = port.get("BindingAddress") or "0.0.0.0"
        parts.append(f"{addr}:{port.get('HostPort')}->{port.get('ContainerPort')}/{proto}")
    return ", ".join(parts)


def _format_created(value: str, now: Optional[datetime] = None) -> str:
    """Format WSLc's absolute creation timestamp like Docker Compose."""
    try:
        if len(value) > 10 and value[10] == "T":
            created = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            # WSLc appends a time-zone abbreviation after the numeric offset.
            created = datetime.strptime(value.rsplit(" ", 1)[0], "%Y-%m-%d %H:%M:%S %z")
        seconds = max(0, int(((now or datetime.now(timezone.utc)) - created).total_seconds()))
    except (TypeError, ValueError):
        return value

    if seconds < 60:
        unit = "second" if seconds == 1 else "seconds"
        return f"{seconds} {unit} ago"
    if seconds < 90:
        return "About a minute ago"
    if seconds < 45 * 60:
        return f"{seconds // 60} minutes ago"
    if seconds < 90 * 60:
        return "About an hour ago"
    if seconds < 24 * 60 * 60:
        return f"{seconds // 3600} hours ago"
    if seconds < 42 * 60 * 60:
        return "About a day ago"
    return f"{seconds // 86400} days ago"


def _print_table(rows: List[List[str]], headers: List[str]) -> None:
    # Docker Compose starts each column at least ten characters after the previous one.
    # With the two-character separator below, cells need a minimum width of eight.
    widths = [max(8, len(h)) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers).rstrip())
    for row in rows:
        print(fmt.format(*row).rstrip())


# --- commands ----------------------------------------------------------------


def cmd_up(ns: argparse.Namespace) -> int:
    project = _load(ns)
    services = _select_services(project, ns.services, ns.profile)
    if not services:
        _err("no services to start")
        return 1

    for message in _runtime_options_warnings(services):
        _err(f"warning: {message}")

    scale: Dict[str, int] = {}
    for spec in ns.scale or []:
        name, _, count = spec.partition("=")
        if not count.isdigit():
            raise ComposeError(f"invalid --scale value: {spec!r} (expected service=N)")
        if name not in project.services:
            raise ComposeError(f"--scale: no such service: {name}")
        scale[name] = int(count)
    # Validate before creating anything, so a bad combination never leaves a half-started project.
    for svc in services:
        if svc.container_name and scale.get(svc.name, svc.replicas) > 1:
            raise ComposeError(f"{svc.name}: cannot scale a service with container_name set")

    static_addresses = set()
    for svc in services:
        if svc.network_ipv4_addresses and scale.get(svc.name, svc.replicas) > 1:
            raise ComposeError(f"{svc.name}: multiple replicas cannot share a static IPv4 address")
        if scale.get(svc.name, svc.replicas) > 0:
            for endpoint in svc.network_ipv4_addresses.items():
                if endpoint in static_addresses:
                    raise ComposeError(f"{svc.name}: another selected service requests the same static IPv4 address")
                static_addresses.add(endpoint)

    needed_networks = {n for svc in services for n in svc.networks}
    network_states = {}
    for net in project.networks.values():
        if net.name not in needed_networks:
            continue
        if net.external:
            if net.name not in engine.network_names():
                raise ComposeError(f"external network {net.name!r} not found")
            network_states[net.name] = networking.verify_network(net)
        else:
            created = engine.ensure_network(
                net.name,
                labels={**net.labels, **resource_labels(project, "network", net.key, net.name)},
                internal=net.internal,
                dry_run=ns.dry_run,
                driver=net.driver, driver_opts=net.driver_opts, subnet=net.subnet,
                gateway=net.gateway, ip_range=net.ip_range,
            )
            if created:
                _info(f"Network {net.name} created")
            if not (created and ns.dry_run):
                network_states[net.name] = networking.verify_network(net)
    for svc in services:
        networking.validate_static_addresses(svc, network_states)

    needed_volumes = {
        m.source for svc in services for m in svc.volumes if m.type == "volume"
    }
    for vol in project.volumes.values():
        if vol.name not in needed_volumes:
            continue
        if vol.external:
            if vol.name not in engine.volume_names():
                raise ComposeError(f"external volume {vol.name!r} not found")
        elif engine.ensure_volume(
            vol.name,
            labels={**vol.labels, **resource_labels(project, "volume", vol.key, vol.name)},
            driver=vol.driver,
            driver_opts=vol.driver_opts,
            dry_run=ns.dry_run,
        ):
            _info(f"Volume {vol.name} created")

    for svc in services:
        # build when explicitly asked, or when the target image is absent
        if svc.build and (ns.build or not engine.image_exists(flags.image_name(project, svc))):
            _info(f"Building {svc.name} ...")
            engine.run(
                flags.build_args(project, svc, path_mapper=engine.to_host_path),
                dry_run=ns.dry_run,
            )

    # Include exited containers so `up` can start an unchanged service instead
    # of colliding with its existing Compose name.
    existing = {c["name"]: c for c in _project_containers(project, all_states=True)} if not ns.dry_run else {}
    gateway_known = any(current.get("host_gateway") for current in existing.values())
    started: List[str] = []
    # Like docker compose and podman-compose, one service failing to start
    # (a host port already taken, an unhealthy dependency...) does not stop
    # the others: its dependents are skipped, every other service still
    # starts, and `up` exits non-zero with a summary at the end.
    failed: Dict[str, str] = {}

    for svc in services:
        replicas = scale.get(svc.name, svc.replicas)
        blocked_by = [dep for dep in svc.depends_on
                      if dep in failed and dep not in svc.depends_on_optional]
        if blocked_by and replicas > 0:
            failed[svc.name] = f"dependency {blocked_by[0]!r} failed to start"
            _err(f"Skipping {svc.name}: {failed[svc.name]}")
            continue
        desired_hash = svc.config_hash(project)
        if svc.depends_on_conditions and replicas > 0:
            if ns.dry_run:
                for dep, condition in svc.depends_on_conditions.items():
                    _info(f"(dry run) would wait for {dep}: {condition}")
            else:
                for dep in svc.depends_on_conditions:
                    if dep in failed:  # optional (required: false): a required one skipped svc
                        _err(f"warning: {svc.name}: optional dependency {dep!r} failed to start; "
                             "not waiting for it")
                try:
                    dependencies.wait_for_dependencies(
                        project, svc, [s for s in services if s.name not in failed],
                        timeout=getattr(ns, "wait_timeout", 0) or 0,
                        log=_info,
                        replicas={s.name: scale.get(s.name, s.replicas) for s in services},
                    )
                except WslcError as exc:
                    failed[svc.name] = str(exc)
                    _err(f"Skipping {svc.name}: {exc}")
                    continue
        for index in range(1, replicas + 1):
            host_gateway.reconcile(project, ns.dry_run, previously_enabled=gateway_known)
            cname = project.container_name(svc, index)
            current = existing.pop(cname, None)
            try:
                if current is not None:
                    fresh = (current["hash"] == desired_hash and not ns.force_recreate
                             and not (current.get("host_gateway") and not any(
                                 entry.partition(":")[2].strip().lower() == "host-gateway"
                                 for entry in svc.extra_hosts))
                             and networking.configuration_matches(svc, current)
                             and _runtime_matches(svc, current)
                             and _local_config_matches(svc, current))
                    if fresh and current["running"]:
                        _info(f"Container {cname} is up-to-date")
                        continue
                    if fresh and not current["running"]:
                        _info(f"Starting {cname} ...")
                        engine.run_retried(["start", cname], dry_run=ns.dry_run)
                        networking.verify_container(svc, cname, ns.dry_run)
                        _apply_extra_hosts(cname, svc.extra_hosts, ns.dry_run)
                        started.append(cname)
                        continue
                    _info(f"Recreating {cname} ...")
                    if current["running"]:
                        engine.run(
                            ["stop", "-t", str(ns.timeout), cname], capture=True, dry_run=ns.dry_run
                        )
                    engine.run(["remove", "-f", cname], capture=True, dry_run=ns.dry_run)
                else:
                    _info(f"Creating {cname} ...")
                _pull_for_command_reset(project, svc, ns.dry_run)
                if len(svc.networks) > 1 and not svc.network_mode:
                    networking.create_and_start(project, svc, index, ns.dry_run)
                else:
                    engine.run(
                        flags.run_args(
                            project, svc, index, detach=True, path_mapper=engine.to_host_path
                        ),
                        dry_run=ns.dry_run,
                    )
                    networking.verify_container(svc, cname, ns.dry_run)
                _apply_extra_hosts(cname, svc.extra_hosts, ns.dry_run)
                started.append(cname)
            except WslcError as exc:
                failed.setdefault(svc.name, str(exc))
                _err(f"Container {cname} failed to start: {exc}")

        # drop replicas beyond the requested scale
        for name, leftover in list(existing.items()):
            if leftover["service"] == svc.name and leftover["index"] > replicas:
                _info(f"Removing surplus {name} ...")
                engine.run(["remove", "-f", name], capture=True, dry_run=ns.dry_run)
                existing.pop(name)

    host_gateway.reconcile(project, ns.dry_run, previously_enabled=gateway_known)
    if failed:
        noun = "service" if len(failed) == 1 else "services"
        _err(f"{len(failed)} {noun} failed to start: "
             + "; ".join(f"{name} ({reason})" for name, reason in failed.items()))
        return 1
    if ns.detach or ns.dry_run or not started:
        return 0
    _info("Attaching to logs (Ctrl+C to detach; containers keep running)")
    return _follow_logs(project, service_names=[s.name for s in services], follow=True)


def cmd_down(ns: argparse.Namespace) -> int:
    project = _load(ns)
    containers = _project_containers(project, all_states=True)
    for entry in containers:
        if entry["running"]:
            _info(f"Stopping {entry['name']} ...")
            engine.run(
                ["stop", "-t", str(ns.timeout), entry["name"]], capture=True, dry_run=ns.dry_run
            )
        _info(f"Removing {entry['name']} ...")
        engine.run(["remove", "-f", entry["name"]], capture=True, dry_run=ns.dry_run)

    existing_networks = engine.network_names()
    host_gateway.reconcile(project, ns.dry_run, cleanup=True,
                           previously_enabled=any(entry.get("host_gateway") for entry in containers))
    for net in project.networks.values():
        if not net.external and net.name in existing_networks:
            try:
                actual = networking.inspect_network(net.name)
                if (actual.get("Labels") or {}).get(LABEL_PROJECT) != project.name:
                    _err(f"warning: preserving network {net.name} (project ownership is unverified)")
                    continue
                _info(f"Removing network {net.name}")
                engine.run(["network", "remove", net.name], capture=True, dry_run=ns.dry_run)
            except WslcError:
                _err(f"warning: could not remove network {net.name} (still in use?)")

    if ns.volumes:
        existing_volumes = engine.volume_names()
        for vol in project.volumes.values():
            if not vol.external and vol.name in existing_volumes:
                _info(f"Removing volume {vol.name}")
                try:
                    engine.run(["volume", "remove", vol.name], capture=True, dry_run=ns.dry_run)
                except WslcError:
                    _err(f"warning: could not remove volume {vol.name}")
    return 0


def cmd_ps(ns: argparse.Namespace) -> int:
    project = _load(ns)
    containers = _project_containers(project, all_states=ns.all)
    if ns.services:
        containers = [c for c in containers if c["service"] in ns.services]
    if ns.quiet:
        for entry in containers:
            print(entry["id"])
        return 0
    rows = [
        [
            entry["name"] or "",
            entry["image"] or "",
            entry["command"],
            entry["service"] or "",
            entry["created"],
            entry["status"] or "",
            _format_ports(entry["ports"]),
        ]
        for entry in sorted(containers, key=lambda c: (c["service"], c["index"]))
    ]
    _print_table(
        rows,
        ["NAME", "IMAGE", "COMMAND", "SERVICE", "CREATED", "STATUS", "PORTS"],
    )
    return 0


def _follow_logs(
    project: Project,
    service_names: List[str],
    follow: bool,
    tail: Optional[int] = None,
    timestamps: bool = False,
) -> int:
    # Stopped and crashed containers too: that is when their logs matter most.
    containers = [
        c
        for c in _project_containers(project, all_states=True)
        if not service_names or c["service"] in service_names
    ]
    if not containers:
        _err("no containers found")
        return 1

    width = max(len(c["name"]) for c in containers)
    procs: List[subprocess.Popen] = []
    threads: List[threading.Thread] = []
    lock = threading.Lock()

    def pump(entry: dict, proc: subprocess.Popen) -> None:
        prefix = entry["name"].ljust(width)
        for line in proc.stdout:  # type: ignore[union-attr]
            with lock:
                sys.stdout.write(f"{prefix} | {line}")
                sys.stdout.flush()

    for entry in containers:
        args = ["logs"]
        if follow and entry["running"]:
            args.append("-f")
        if tail is not None:
            args += ["-n", str(tail)]
        if timestamps:
            args.append("-t")
        args.append(entry["name"])
        proc = engine.popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        procs.append(proc)
        thread = threading.Thread(target=pump, args=(entry, proc), daemon=True)
        thread.start()
        threads.append(thread)

    try:
        for thread in threads:
            thread.join()
        codes = [proc.wait() for proc in procs]
    except KeyboardInterrupt:
        for proc in procs:
            proc.send_signal(signal.SIGTERM)
        return 0
    return next((code for code in codes if code), 0)


def cmd_logs(ns: argparse.Namespace) -> int:
    project = _load(ns)
    return _follow_logs(
        project,
        service_names=ns.services,
        follow=ns.follow,
        tail=ns.tail,
        timestamps=ns.timestamps,
    )


def cmd_exec(ns: argparse.Namespace) -> int:
    project = _load(ns)
    if ns.service not in project.services:
        raise ComposeError(f"no such service: {ns.service}")
    cname = project.container_name(project.services[ns.service], ns.index)
    # stdin stays attached (piped input works with -T); -T only disables the TTY.
    args = ["exec", "-i"]
    if not ns.no_tty and sys.stdin.isatty():
        args.append("-t")
    if ns.user:
        args += ["-u", ns.user]
    if ns.workdir:
        args += ["-w", ns.workdir]
    for env in ns.env or []:
        args += ["-e", env]
    args.append(cname)
    args += ns.command
    proc = engine.run(args, check=False, dry_run=ns.dry_run)
    return proc.returncode


def _pull_for_command_reset(project: Project, svc: Service, dry_run: bool) -> None:
    """`command: []` restates the image's ENTRYPOINT, which is read from the local image."""
    if svc.command != [] or svc.entrypoint is not None or dry_run:
        return
    image = flags.image_name(project, svc)
    if engine.image_entrypoint(image) is None:
        _info(f"Pulling {image} ...")
        engine.run(["pull", image])


def _apply_extra_hosts(container: str, hosts: List[str], dry_run: bool) -> None:
    """Best-effort fallback while wslc has no native --add-host flag."""
    lines = []
    for entry in hosts:
        host, sep, address = entry.partition(":")
        if not sep or not host.strip() or not address.strip():
            continue
        addr = "$GW" if address.strip().lower() == "host-gateway" else address.strip()
        lines.append(f'echo "{addr} {host.strip()}" >> /etc/hosts')
    if lines:
        script = (
            "GW=$(awk '$2 == \"00000000\" { h=$3; for (i=7; i>=1; i-=2) { "
            "hi=index(\"0123456789ABCDEF\", substr(h,i,1))-1; "
            "lo=index(\"0123456789ABCDEF\", substr(h,i+1,1))-1; "
            "printf \"%s%d\", i==7 ? \"\" : \".\", hi*16+lo } print \"\"; exit }' "
            "/proc/net/route 2>/dev/null); "
            + "; ".join(lines)
        )
        engine.run(["exec", "--user", "0", container, "sh", "-c", script], check=False, dry_run=dry_run)




def _lifecycle(ns: argparse.Namespace, action: str) -> int:
    project = _load(ns)
    containers = _project_containers(project, all_states=action in ("start", "restart"))
    if ns.services:
        containers = [c for c in containers if c["service"] in ns.services]
    if not containers:
        _err("no containers found")
        return 1
    for entry in containers:
        if action in ("stop", "restart") and entry["running"]:
            _info(f"Stopping {entry['name']} ...")
            engine.run(
                ["stop", "-t", str(ns.timeout), entry["name"]], capture=True, dry_run=ns.dry_run
            )
        if action in ("start", "restart"):
            _info(f"Starting {entry['name']} ...")
            engine.run_retried(["start", entry["name"]], dry_run=ns.dry_run)
            service = project.services.get(entry["service"])
            if service is not None:  # a leftover of a service removed from the file
                _apply_extra_hosts(entry["name"], service.extra_hosts, ns.dry_run)
    host_gateway.reconcile(project, ns.dry_run,
                           previously_enabled=any(entry.get("host_gateway") for entry in containers))
    return 0


def cmd_pull(ns: argparse.Namespace) -> int:
    project = _load(ns)
    for svc in _select_services(project, ns.services, ns.profile):
        if svc.image:
            _info(f"Pulling {svc.image} ...")
            engine.run(["pull", svc.image], dry_run=ns.dry_run)
    return 0


def cmd_build(ns: argparse.Namespace) -> int:
    project = _load(ns)
    built = 0
    for svc in _select_services(project, ns.services, ns.profile):
        if not svc.build:
            continue
        _info(f"Building {svc.name} ...")
        engine.run(
            flags.build_args(
                project, svc, no_cache=ns.no_cache, path_mapper=engine.to_host_path
            ),
            dry_run=ns.dry_run,
        )
        built += 1
    if not built:
        _err("no services with a build section")
        return 1
    return 0


def cmd_config(ns: argparse.Namespace) -> int:
    project = _load(ns)
    print(yaml.safe_dump(dataclasses.asdict(project), sort_keys=False, default_flow_style=False))
    return 0


def cmd_version(ns: argparse.Namespace) -> int:
    print(f"wslc-compose {__version__}")
    try:
        engine.run(["version"], check=False)
    except WslcError as exc:
        _err(str(exc))
    return 0


# --- argument parsing ---------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wslc-compose",
        description="docker-compose style orchestration for WSL containers (wslc)",
    )
    parser.add_argument(
        "-f", "--file", action="append",
        help="compose file; repeat to overlay several (default: auto-detect, or $COMPOSE_FILE)",
    )
    parser.add_argument("-p", "--project-name", help="project name (default: directory name)")
    parser.add_argument("--env-file", help="alternate .env file")
    parser.add_argument("--dry-run", action="store_true", help="print wslc commands instead of running them")
    parser.add_argument("--profile", action="append", default=[], help="enable a compose profile")
    parser.add_argument("--version", action="version", version=f"wslc-compose {__version__}")

    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("up", help="create and start services")
    p.add_argument("services", nargs="*")
    p.add_argument("-d", "--detach", action="store_true")
    p.add_argument("--build", action="store_true", help="rebuild images before starting")
    p.add_argument("--force-recreate", action="store_true")
    p.add_argument("--scale", action="append", metavar="SERVICE=N")
    p.add_argument("-t", "--timeout", type=int, default=10)
    p.add_argument("--wait-timeout", type=int, default=0, metavar="SECONDS",
                   help="max seconds to wait for each depends_on condition "
                        "(service_healthy / service_completed_successfully); 0 = no limit")
    p.set_defaults(func=cmd_up)

    p = sub.add_parser("down", help="stop and remove project containers and networks")
    p.add_argument("-v", "--volumes", action="store_true", help="also remove named volumes")
    p.add_argument("-t", "--timeout", type=int, default=10)
    p.set_defaults(func=cmd_down)

    p = sub.add_parser("ps", help="list project containers")
    p.add_argument("services", nargs="*")
    p.add_argument(
        "-a", "--all", action="store_true", help="show all containers, including stopped ones"
    )
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(func=cmd_ps)

    p = sub.add_parser("logs", help="show container logs")
    p.add_argument("services", nargs="*")
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("-n", "--tail", type=int)
    p.add_argument("-t", "--timestamps", action="store_true")
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("exec", help="run a command in a service container")
    p.add_argument("service")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.add_argument("--index", type=int, default=1)
    p.add_argument("-u", "--user")
    p.add_argument("-w", "--workdir")
    p.add_argument("-e", "--env", action="append")
    p.add_argument("-T", "--no-tty", action="store_true")
    p.set_defaults(func=cmd_exec)

    for action in ("start", "stop", "restart"):
        p = sub.add_parser(action, help=f"{action} project containers")
        p.add_argument("services", nargs="*")
        p.add_argument("-t", "--timeout", type=int, default=10)
        p.set_defaults(func=lambda ns, a=action: _lifecycle(ns, a))

    p = sub.add_parser("pull", help="pull service images")
    p.add_argument("services", nargs="*")
    p.set_defaults(func=cmd_pull)

    p = sub.add_parser("build", help="build service images")
    p.add_argument("services", nargs="*")
    p.add_argument("--no-cache", action="store_true")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("config", help="print the resolved configuration")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("version", help="show version information")
    p.set_defaults(func=cmd_version)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)
    try:
        return ns.func(ns)
    except (ComposeError, WslcError) as exc:
        _err(str(exc))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
