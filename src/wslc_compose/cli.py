"""wslc-compose command line interface."""

from __future__ import annotations

import argparse
import dataclasses
import os
import signal
import subprocess
import sys
import threading
from typing import Dict, List, Optional

import yaml

from wslc_compose import (
    LABEL_CONFIG_HASH,
    LABEL_INDEX,
    LABEL_SERVICE,
    __version__,
)
from wslc_compose import engine, flags
from wslc_compose.engine import WslcError
from wslc_compose.loader import ComposeError, find_compose_file, load_project
from wslc_compose.model import Project, Service

PROTOCOLS = {6: "tcp", 17: "udp"}


def _err(message: str) -> None:
    print(f"wslc-compose: {message}", file=sys.stderr)


def _info(message: str) -> None:
    print(message)


# --- project loading -------------------------------------------------------


def _locate_compose_file(explicit: Optional[str]) -> str:
    if explicit:
        if not os.path.isfile(explicit):
            raise ComposeError(f"compose file not found: {explicit}")
        return explicit
    directory = os.getcwd()
    while True:
        found = find_compose_file(directory)
        if found:
            return found
        parent = os.path.dirname(directory)
        if parent == directory:
            raise ComposeError(
                "no compose file found (looked for compose.yaml / compose.yml / "
                "docker-compose.yml / docker-compose.yaml in this directory and parents)"
            )
        directory = parent


def _load(ns: argparse.Namespace) -> Project:
    compose_file = _locate_compose_file(ns.file)
    project = load_project(compose_file, project_name=ns.project_name, env_file=ns.env_file)
    for warning in project.warnings:
        _err(f"warning: {warning}")
    return project


def _select_services(
    project: Project, names: List[str], profiles: List[str]
) -> List[Service]:
    active = set(profiles or [])
    for name in names:
        if name not in project.services:
            raise ComposeError(f"no such service: {name}")
    services = project.sorted_services(names or None)
    result = []
    for svc in services:
        if svc.profiles and not (set(svc.profiles) & active) and svc.name not in names:
            continue
        result.append(svc)
    return result


# --- container queries ------------------------------------------------------


def _project_containers(project: Project) -> List[dict]:
    """list entries enriched with inspect data (labels, status)."""
    entries = engine.list_project_containers(project.name)
    enriched = []
    for entry in entries:
        data = engine.inspect(entry.get("Id") or entry.get("Name")) or {}
        labels = data.get("Labels") or {}
        state = data.get("State") or {}
        enriched.append(
            {
                "id": entry.get("Id"),
                "name": entry.get("Name") or data.get("Name"),
                "image": entry.get("Image") or data.get("Image"),
                "service": labels.get(LABEL_SERVICE, ""),
                "index": int(labels.get(LABEL_INDEX, "1") or 1),
                "hash": labels.get(LABEL_CONFIG_HASH, ""),
                "running": bool(state.get("Running")),
                "status": state.get("Status", "unknown"),
                "ports": entry.get("Ports") or [],
            }
        )
    return enriched


def _format_ports(ports: List[dict]) -> str:
    parts = []
    for port in ports:
        proto = PROTOCOLS.get(port.get("Protocol"), str(port.get("Protocol", "")))
        addr = port.get("BindingAddress") or "0.0.0.0"
        parts.append(f"{addr}:{port.get('HostPort')}->{port.get('ContainerPort')}/{proto}")
    return ", ".join(parts)


def _print_table(rows: List[List[str]], headers: List[str]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    for row in rows:
        print(fmt.format(*row))


# --- commands ----------------------------------------------------------------


def cmd_up(ns: argparse.Namespace) -> int:
    project = _load(ns)
    services = _select_services(project, ns.services, ns.profile)
    if not services:
        _err("no services to start")
        return 1

    scale: Dict[str, int] = {}
    for spec in ns.scale or []:
        name, _, count = spec.partition("=")
        if not count.isdigit():
            raise ComposeError(f"invalid --scale value: {spec!r} (expected service=N)")
        scale[name] = int(count)

    needed_networks = {n for svc in services for n in svc.networks}
    for net in project.networks.values():
        if net.name not in needed_networks:
            continue
        if net.external:
            if net.name not in engine.network_names():
                raise ComposeError(f"external network {net.name!r} not found")
        elif engine.ensure_network(net.name, dry_run=ns.dry_run):
            _info(f"Network {net.name} created")

    needed_volumes = {
        m.source for svc in services for m in svc.volumes if m.type == "volume"
    }
    for vol in project.volumes.values():
        if vol.name not in needed_volumes:
            continue
        if vol.external:
            if vol.name not in engine.volume_names():
                raise ComposeError(f"external volume {vol.name!r} not found")
        elif engine.ensure_volume(vol.name, dry_run=ns.dry_run):
            _info(f"Volume {vol.name} created")

    for svc in services:
        # build when explicitly asked, or when the target image is absent
        if svc.build and (ns.build or not engine.image_exists(flags.image_name(project, svc))):
            _info(f"Building {svc.name} ...")
            engine.run(
                flags.build_args(project, svc, path_mapper=engine.to_host_path),
                dry_run=ns.dry_run,
            )

    existing = {c["name"]: c for c in _project_containers(project)} if not ns.dry_run else {}
    started: List[str] = []

    for svc in services:
        replicas = scale.get(svc.name, svc.replicas)
        if svc.container_name and replicas > 1:
            raise ComposeError(
                f"{svc.name}: cannot scale a service with container_name set"
            )
        desired_hash = svc.config_hash()
        for index in range(1, replicas + 1):
            cname = project.container_name(svc, index)
            current = existing.pop(cname, None)
            if current is not None:
                fresh = current["hash"] == desired_hash and not ns.force_recreate
                if fresh and current["running"]:
                    _info(f"Container {cname} is up-to-date")
                    continue
                if fresh and not current["running"]:
                    _info(f"Starting {cname} ...")
                    engine.run_retried(["start", cname], dry_run=ns.dry_run)
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
            engine.run(
                flags.run_args(
                    project, svc, index, detach=True, path_mapper=engine.to_host_path
                ),
                dry_run=ns.dry_run,
            )
            started.append(cname)

        # drop replicas beyond the requested scale
        for name, leftover in list(existing.items()):
            if leftover["service"] == svc.name and leftover["index"] > replicas:
                _info(f"Removing surplus {name} ...")
                engine.run(["remove", "-f", name], capture=True, dry_run=ns.dry_run)
                existing.pop(name)

    if ns.detach or ns.dry_run or not started:
        return 0
    _info("Attaching to logs (Ctrl+C to detach; containers keep running)")
    return _follow_logs(project, service_names=[s.name for s in services], follow=True)


def cmd_down(ns: argparse.Namespace) -> int:
    project = _load(ns)
    containers = _project_containers(project)
    for entry in containers:
        if entry["running"]:
            _info(f"Stopping {entry['name']} ...")
            engine.run(
                ["stop", "-t", str(ns.timeout), entry["name"]], capture=True, dry_run=ns.dry_run
            )
        _info(f"Removing {entry['name']} ...")
        engine.run(["remove", "-f", entry["name"]], capture=True, dry_run=ns.dry_run)

    existing_networks = engine.network_names()
    for net in project.networks.values():
        if not net.external and net.name in existing_networks:
            _info(f"Removing network {net.name}")
            try:
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
    containers = _project_containers(project)
    if ns.services:
        containers = [c for c in containers if c["service"] in ns.services]
    if ns.quiet:
        for entry in containers:
            print(entry["id"])
        return 0
    rows = [
        [
            entry["name"] or "",
            entry["service"] or "",
            entry["image"] or "",
            entry["status"] or "",
            _format_ports(entry["ports"]),
        ]
        for entry in sorted(containers, key=lambda c: (c["service"], c["index"]))
    ]
    _print_table(rows, ["NAME", "SERVICE", "IMAGE", "STATUS", "PORTS"])
    return 0


def _follow_logs(
    project: Project,
    service_names: List[str],
    follow: bool,
    tail: Optional[int] = None,
    timestamps: bool = False,
) -> int:
    containers = [
        c
        for c in _project_containers(project)
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
    except KeyboardInterrupt:
        for proc in procs:
            proc.send_signal(signal.SIGTERM)
        return 0
    return 0


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
    args = ["exec"]
    if not ns.no_tty and sys.stdin.isatty():
        args += ["-i", "-t"]
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


def _lifecycle(ns: argparse.Namespace, action: str) -> int:
    project = _load(ns)
    containers = _project_containers(project)
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
    parser.add_argument("-f", "--file", help="compose file (default: auto-detect)")
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
    p.set_defaults(func=cmd_up)

    p = sub.add_parser("down", help="stop and remove project containers and networks")
    p.add_argument("-v", "--volumes", action="store_true", help="also remove named volumes")
    p.add_argument("-t", "--timeout", type=int, default=10)
    p.set_defaults(func=cmd_down)

    p = sub.add_parser("ps", help="list project containers")
    p.add_argument("services", nargs="*")
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
