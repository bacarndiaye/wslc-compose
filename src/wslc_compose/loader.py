"""Load, interpolate and normalize a compose file into a Project."""

from __future__ import annotations

import os
import re
import shlex
from typing import Dict, List, Optional, Tuple

import yaml

from wslc_compose.interpolation import interpolate_tree
from wslc_compose.model import (
    BuildConfig,
    Network,
    PortMapping,
    Project,
    Service,
    Volume,
    VolumeMount,
)

COMPOSE_FILENAMES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yml",
    "docker-compose.yaml",
)

# compose keys we accept but wslc cannot enforce (yet)
UNSUPPORTED_KEYS = {
    "healthcheck": "healthchecks are not supported by wslc; depends_on conditions fall back to 'started'",
    "cap_add": "capabilities are not configurable with wslc",
    "cap_drop": "capabilities are not configurable with wslc",
    "privileged": "privileged mode is not supported by wslc",
    "devices": "device mapping is not supported by wslc (see 'gpus' for GPUs)",
    "extra_hosts": "extra_hosts is not supported by wslc",
    "sysctls": "sysctls are not supported by wslc",
    "secrets": "secrets are not supported; use environment/env_file instead",
    "configs": "configs are not supported; use bind mounts instead",
    "init": "init is not supported by wslc",
    "pid": "pid mode is not supported by wslc",
    "ipc": "ipc mode is not supported by wslc",
    "read_only": "read_only rootfs is not supported by wslc",
    "security_opt": "security_opt is not supported by wslc",
    "logging": "logging drivers are not configurable with wslc",
    "healthcheck_disable": "",
}

_WIN_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


class ComposeError(ValueError):
    pass


def find_compose_file(directory: str) -> Optional[str]:
    for name in COMPOSE_FILENAMES:
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            return path
    return None


def load_dotenv(path: str) -> Dict[str, str]:
    env: Dict[str, str] = {}
    if not os.path.isfile(path):
        return env
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            env[key] = value
    return env


def normalize_project_name(raw: str) -> str:
    name = raw.strip().lower()
    name = re.sub(r"[^a-z0-9_-]", "", name)
    name = name.lstrip("_-")
    if not name:
        raise ComposeError(f"cannot derive a valid project name from {raw!r}")
    return name


def _as_list(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _as_command(value) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, str):
        return shlex.split(value)
    return [str(v) for v in value]


def _as_mapping(value, what: str) -> Dict[str, str]:
    """Accept both list ("K=V") and mapping syntax for labels/build args."""
    result: Dict[str, str] = {}
    if value is None:
        return result
    if isinstance(value, dict):
        for k, v in value.items():
            result[str(k)] = "" if v is None else str(v)
        return result
    if isinstance(value, list):
        for item in value:
            key, sep, val = str(item).partition("=")
            result[key] = val if sep else ""
        return result
    raise ComposeError(f"{what}: expected list or mapping, got {type(value).__name__}")


def _as_environment(value) -> Dict[str, Optional[str]]:
    result: Dict[str, Optional[str]] = {}
    if value is None:
        return result
    if isinstance(value, dict):
        for k, v in value.items():
            result[str(k)] = None if v is None else str(v)
        return result
    if isinstance(value, list):
        for item in value:
            key, sep, val = str(item).partition("=")
            result[key] = val if sep else None
        return result
    raise ComposeError(f"environment: expected list or mapping, got {type(value).__name__}")


def parse_port(spec) -> List[PortMapping]:
    if isinstance(spec, dict):  # long syntax
        target = int(spec["target"])
        published = spec.get("published")
        host_ip = spec.get("host_ip")
        pub = str(published) if published is not None else None
        if host_ip and pub:
            pub = f"{host_ip}:{pub}"
        return [PortMapping(target, pub, str(spec.get("protocol", "tcp")))]

    text = str(spec)
    protocol = "tcp"
    if "/" in text:
        text, protocol = text.rsplit("/", 1)

    parts = text.split(":")
    if len(parts) == 1:
        host, target = None, parts[0]
    elif len(parts) == 2:
        host, target = parts[0], parts[1]
    elif len(parts) == 3:
        host, target = f"{parts[0]}:{parts[1]}", parts[2]
    else:
        raise ComposeError(f"invalid port mapping: {spec!r}")

    def expand(rng: str) -> List[int]:
        if "-" in rng:
            lo, hi = rng.split("-", 1)
            return list(range(int(lo), int(hi) + 1))
        return [int(rng)]

    targets = expand(target)
    if host is None:
        return [PortMapping(t, None, protocol) for t in targets]
    ip_prefix = ""
    host_ports = host
    if host.count(":") == 1:  # ip:port form
        ip_prefix, host_ports = host.split(":", 1)
        ip_prefix += ":"
    hosts = expand(host_ports)
    if len(hosts) != len(targets):
        raise ComposeError(f"port range mismatch in {spec!r}")
    return [PortMapping(t, f"{ip_prefix}{h}", protocol) for h, t in zip(hosts, targets)]


def _split_volume_spec(spec: str) -> Tuple[str, ...]:
    """Split 'src:dst[:opts]' respecting Windows drive letters (E:\\x:/data)."""
    parts = spec.split(":")
    merged: List[str] = []
    i = 0
    while i < len(parts):
        part = parts[i]
        if (
            len(part) == 1
            and part.isalpha()
            and i + 1 < len(parts)
            and parts[i + 1][:1] in ("\\", "/")
        ):
            merged.append(part + ":" + parts[i + 1])
            i += 2
        else:
            merged.append(part)
            i += 1
    return tuple(merged)


def _is_host_path(source: str) -> bool:
    return (
        source.startswith(("/", "./", "../", "~", "\\\\"))
        or source in (".", "..")
        or bool(_WIN_PATH_RE.match(source))
    )


def parse_volume(spec, project_dir: str) -> VolumeMount:
    if isinstance(spec, dict):  # long syntax
        vtype = spec.get("type", "volume")
        source = spec.get("source")
        target = spec.get("target")
        if not target:
            raise ComposeError(f"volume entry missing target: {spec!r}")
        read_only = bool(spec.get("read_only", False))
        if vtype == "bind" and source:
            source = _resolve_bind_source(source, project_dir)
        return VolumeMount(vtype, source, target, read_only)

    parts = _split_volume_spec(str(spec))
    if len(parts) == 1:
        raise ComposeError(
            f"anonymous volumes are not supported by wslc: {spec!r}; name the volume or use a bind mount"
        )
    if len(parts) == 2:
        source, target, opts = parts[0], parts[1], ""
    elif len(parts) == 3:
        source, target, opts = parts
    else:
        raise ComposeError(f"invalid volume spec: {spec!r}")
    read_only = "ro" in opts.split(",") if opts else False
    if _is_host_path(source):
        return VolumeMount("bind", _resolve_bind_source(source, project_dir), target, read_only)
    return VolumeMount("volume", source, target, read_only)


def _resolve_bind_source(source: str, project_dir: str) -> str:
    if _WIN_PATH_RE.match(source) or source.startswith("\\\\"):
        return source  # already a Windows path, hand to wslc as-is
    source = os.path.expanduser(source)
    if not os.path.isabs(source):
        source = os.path.normpath(os.path.join(project_dir, source))
    return source


def _parse_depends_on(value) -> Tuple[List[str], List[str]]:
    warnings: List[str] = []
    if value is None:
        return [], warnings
    if isinstance(value, list):
        return [str(v) for v in value], warnings
    if isinstance(value, dict):
        deps = []
        for name, cfg in value.items():
            deps.append(str(name))
            condition = (cfg or {}).get("condition", "service_started")
            if condition not in ("service_started",):
                warnings.append(
                    f"depends_on condition {condition!r} on {name!r} is treated as service_started"
                )
        return deps, warnings
    raise ComposeError("depends_on: expected list or mapping")


def _parse_build(value, project_dir: str) -> BuildConfig:
    if isinstance(value, str):
        return BuildConfig(context=_resolve_bind_source(value, project_dir))
    context = _resolve_bind_source(value.get("context", "."), project_dir)
    return BuildConfig(
        context=context,
        dockerfile=value.get("dockerfile"),
        args=_as_mapping(value.get("args"), "build.args"),
        target=value.get("target"),
        pull=bool(value.get("pull", False)),
    )


def load_project(
    compose_file: str,
    project_name: Optional[str] = None,
    env_file: Optional[str] = None,
) -> Project:
    compose_file = os.path.abspath(compose_file)
    project_dir = os.path.dirname(compose_file)

    dotenv_path = env_file or os.path.join(project_dir, ".env")
    env = dict(load_dotenv(dotenv_path))
    env.update(os.environ)  # process env wins

    with open(compose_file, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict) or "services" not in raw:
        raise ComposeError(f"{compose_file}: no 'services' section found")
    raw = interpolate_tree(raw, env)

    name = normalize_project_name(
        project_name
        or env.get("COMPOSE_PROJECT_NAME")
        or (str(raw["name"]) if raw.get("name") else "")
        or os.path.basename(project_dir)
    )
    project = Project(name=name, directory=project_dir)

    # --- top level networks & volumes -------------------------------------
    raw_networks = raw.get("networks") or {}
    for key, cfg in raw_networks.items():
        cfg = cfg or {}
        external = bool(cfg.get("external", False))
        net_name = cfg.get("name") or (key if external else f"{name}_{key}")
        project.networks[key] = Network(key=key, name=net_name, external=external)

    raw_volumes = raw.get("volumes") or {}
    for key, cfg in raw_volumes.items():
        cfg = cfg or {}
        external = bool(cfg.get("external", False))
        vol_name = cfg.get("name") or (key if external else f"{name}_{key}")
        project.volumes[key] = Volume(key=key, name=vol_name, external=external)

    # --- services ----------------------------------------------------------
    for svc_name, cfg in (raw.get("services") or {}).items():
        if cfg is None:
            raise ComposeError(f"service {svc_name!r} is empty")
        svc = Service(name=str(svc_name))

        for key, message in UNSUPPORTED_KEYS.items():
            if key in cfg and message:
                project.warnings.append(f"{svc_name}: {message} (ignoring '{key}')")

        svc.image = cfg.get("image")
        if "build" in cfg:
            svc.build = _parse_build(cfg["build"], project_dir)
        if not svc.image and not svc.build:
            raise ComposeError(f"service {svc_name!r} needs 'image' or 'build'")

        svc.command = _as_command(cfg.get("command"))
        svc.entrypoint = _as_command(cfg.get("entrypoint"))
        svc.container_name = cfg.get("container_name")
        svc.environment = _as_environment(cfg.get("environment"))
        svc.env_files = [
            f if os.path.isabs(f) else os.path.join(project_dir, f)
            for f in _as_list(cfg.get("env_file"))
        ]
        for spec in cfg.get("ports") or []:
            svc.ports.extend(parse_port(spec))
        for spec in cfg.get("volumes") or []:
            svc.volumes.append(parse_volume(spec, project_dir))
        svc.tmpfs = _as_list(cfg.get("tmpfs"))

        # networks: list or mapping (with aliases); default network otherwise
        raw_svc_networks = cfg.get("networks")
        if raw_svc_networks is None:
            keys = ["default"]
            aliases: Dict[str, List[str]] = {}
        elif isinstance(raw_svc_networks, list):
            keys = [str(k) for k in raw_svc_networks]
            aliases = {}
        else:
            keys = [str(k) for k in raw_svc_networks]
            aliases = {
                str(k): _as_list((v or {}).get("aliases"))
                for k, v in raw_svc_networks.items()
            }
        for key in keys:
            if key not in project.networks:
                if key == "default":
                    project.networks["default"] = Network(
                        key="default", name=f"{name}_default", external=False
                    )
                else:
                    raise ComposeError(
                        f"service {svc_name!r} references undefined network {key!r}"
                    )
            net = project.networks[key]
            svc.networks.append(net.name)
            svc.network_aliases[net.name] = aliases.get(key, [])

        # named volumes must be declared
        for mount in svc.volumes:
            if mount.type == "volume":
                if mount.source not in project.volumes:
                    raise ComposeError(
                        f"service {svc_name!r} references undefined volume {mount.source!r}"
                    )
                mount.source = project.volumes[mount.source].name

        svc.depends_on, dep_warnings = _parse_depends_on(cfg.get("depends_on"))
        project.warnings.extend(f"{svc_name}: {w}" for w in dep_warnings)

        svc.hostname = cfg.get("hostname")
        svc.domainname = cfg.get("domainname")
        svc.dns = _as_list(cfg.get("dns"))
        svc.dns_search = _as_list(cfg.get("dns_search"))
        svc.dns_opt = _as_list(cfg.get("dns_opt"))
        svc.user = str(cfg["user"]) if cfg.get("user") is not None else None
        svc.working_dir = cfg.get("working_dir")
        svc.labels = _as_mapping(cfg.get("labels"), "labels")
        svc.stop_signal = cfg.get("stop_signal")
        svc.shm_size = str(cfg["shm_size"]) if cfg.get("shm_size") is not None else None
        svc.stdin_open = bool(cfg.get("stdin_open", False))
        svc.tty = bool(cfg.get("tty", False))
        svc.profiles = _as_list(cfg.get("profiles"))
        svc.restart = cfg.get("restart")
        if svc.restart and svc.restart not in ("no", '"no"'):
            project.warnings.append(
                f"{svc_name}: restart policies are not supported by wslc yet (ignoring 'restart: {svc.restart}')"
            )

        raw_ulimits = cfg.get("ulimits")
        if isinstance(raw_ulimits, dict):
            for item, limit in raw_ulimits.items():
                if isinstance(limit, dict):
                    svc.ulimits.append(f"{item}={limit.get('soft', -1)}:{limit.get('hard', -1)}")
                else:
                    svc.ulimits.append(f"{item}={limit}")

        # resource limits: v2 style and deploy.resources.limits
        svc.mem_limit = cfg.get("mem_limit")
        svc.cpus = str(cfg["cpus"]) if cfg.get("cpus") is not None else None
        deploy = cfg.get("deploy") or {}
        limits = ((deploy.get("resources") or {}).get("limits")) or {}
        svc.mem_limit = limits.get("memory", svc.mem_limit)
        if limits.get("cpus") is not None:
            svc.cpus = str(limits["cpus"])
        if deploy.get("replicas"):
            svc.replicas = int(deploy["replicas"])
        reservations = (deploy.get("resources") or {}).get("reservations") or {}
        for device in reservations.get("devices") or []:
            if "gpu" in (device.get("capabilities") or []):
                svc.gpus = "all" if device.get("count", "all") == "all" else str(device["count"])

        if cfg.get("gpus"):  # shorthand
            svc.gpus = str(cfg["gpus"])

        project.services[svc.name] = svc

    return project
