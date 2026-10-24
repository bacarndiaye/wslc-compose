"""Normalized in-memory model of a compose project."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class BuildConfig:
    context: str
    dockerfile: Optional[str] = None
    args: Dict[str, str] = field(default_factory=dict)
    target: Optional[str] = None
    pull: bool = False


@dataclass
class VolumeMount:
    """A -v entry: either a bind mount or a named volume."""

    type: str  # "bind" | "volume" | "tmpfs"
    source: Optional[str]  # host path or volume name (None for tmpfs)
    target: str
    read_only: bool = False


@dataclass
class PortMapping:
    target: int
    published: Optional[str] = None  # may be None (random), a port, or "ip:port"
    protocol: str = "tcp"

    def to_flag(self) -> str:
        spec = str(self.target)
        if self.published:
            spec = f"{self.published}:{spec}"
        if self.protocol and self.protocol != "tcp":
            spec = f"{spec}/{self.protocol}"
        return spec


@dataclass
class Service:
    name: str
    image: Optional[str] = None
    build: Optional[BuildConfig] = None
    command: Optional[List[str]] = None
    entrypoint: Optional[List[str]] = None
    container_name: Optional[str] = None
    environment: Dict[str, Optional[str]] = field(default_factory=dict)
    env_files: List[str] = field(default_factory=list)
    ports: List[PortMapping] = field(default_factory=list)
    volumes: List[VolumeMount] = field(default_factory=list)
    tmpfs: List[str] = field(default_factory=list)
    networks: List[str] = field(default_factory=list)  # project-resolved network names
    network_aliases: Dict[str, List[str]] = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)
    hostname: Optional[str] = None
    domainname: Optional[str] = None
    dns: List[str] = field(default_factory=list)
    dns_search: List[str] = field(default_factory=list)
    dns_opt: List[str] = field(default_factory=list)
    user: Optional[str] = None
    working_dir: Optional[str] = None
    labels: Dict[str, str] = field(default_factory=dict)
    mem_limit: Optional[str] = None
    cpus: Optional[str] = None
    shm_size: Optional[str] = None
    ulimits: List[str] = field(default_factory=list)
    stop_signal: Optional[str] = None
    gpus: Optional[str] = None
    stdin_open: bool = False
    tty: bool = False
    replicas: int = 1
    profiles: List[str] = field(default_factory=list)
    restart: Optional[str] = None  # accepted but not enforceable by wslc yet

    def config_hash(self) -> str:
        """Versioned hash of resolved runtime inputs for compatible Compose frontends."""
        return self._config_hash("v1", include_v2_fields=False)

    def config_hash_v2(self) -> str:
        """Experimental shared hash; do not publish until the v2 contract is complete."""
        return self._config_hash("v2", include_v2_fields=True)

    def _config_hash(self, version: str, include_v2_fields: bool) -> str:
        payload = {
            "service": self.name,
            "image": self.image or "",
            # Build context changes require an explicit build. The shared runtime
            # fingerprint only records that the service's image is build-backed.
            "build": self.build is not None,
            "command": self.command or [],
            "entrypoint": self.entrypoint or [],
            "environment": dict(sorted(self.environment.items())),
            "ports": sorted({port.to_flag() for port in self.ports}),
            "mounts": sorted({
                f"{mount.source or ''}:{mount.target}" + (":ro" if mount.read_only else "")
                for mount in self.volumes
            }),
            "networks": sorted(
                (
                    {
                        "name": network,
                        "aliases": sorted(self.network_aliases.get(network, [])),
                    }
                    for network in self.networks
                ),
                key=lambda network: network["name"],
            ),
            "labels": dict(sorted(self.labels.items())),
            "hostname": self.hostname or "",
            "domainname": self.domainname or "",
            "user": self.user or "",
            "working_dir": self.working_dir or "",
            "dns": sorted(set(self.dns)),
            "dns_search": sorted(set(self.dns_search)),
            "dns_opt": sorted(set(self.dns_opt)),
            "tmpfs": sorted(set(self.tmpfs)),
            "ulimits": sorted(set(self.ulimits)),
            "mem_limit": self.mem_limit or "",
            "cpus": self.cpus or "",
            "shm_size": self.shm_size or "",
            "stop_signal": self.stop_signal or "",
        }
        if include_v2_fields:
            payload["stdin_open"] = self.stdin_open
            payload["gpus_all"] = self.gpus == "all"
            payload["tty"] = self.tty
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return version + ":" + hashlib.sha256(blob.encode()).hexdigest()


@dataclass
class Network:
    key: str  # name used in the compose file
    name: str  # actual wslc network name
    external: bool = False


@dataclass
class Volume:
    key: str
    name: str
    external: bool = False


@dataclass
class Project:
    name: str
    directory: str
    compose_files: List[str] = field(default_factory=list)
    env_file: Optional[str] = None
    services: Dict[str, Service] = field(default_factory=dict)
    networks: Dict[str, Network] = field(default_factory=dict)
    volumes: Dict[str, Volume] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def container_name(self, service: Service, index: int = 1) -> str:
        if service.container_name:
            return service.container_name
        return f"{self.name}-{service.name}-{index}"

    def sorted_services(self, names: Optional[List[str]] = None) -> List[Service]:
        """Services in dependency order (depends_on first)."""
        selected = set(names or self.services)
        if names:
            # pull in transitive dependencies
            stack = list(names)
            while stack:
                svc = self.services.get(stack.pop())
                if not svc:
                    continue
                for dep in svc.depends_on:
                    if dep not in selected:
                        selected.add(dep)
                        stack.append(dep)
        order: List[Service] = []
        seen: Dict[str, int] = {}  # 0 = visiting, 1 = done

        def visit(name: str, chain: List[str]) -> None:
            if name not in self.services or name not in selected:
                return
            state = seen.get(name)
            if state == 1:
                return
            if state == 0:
                raise ValueError(
                    "circular depends_on: " + " -> ".join(chain + [name])
                )
            seen[name] = 0
            for dep in self.services[name].depends_on:
                visit(dep, chain + [name])
            seen[name] = 1
            order.append(self.services[name])

        for name in sorted(selected):
            visit(name, [])
        return order
