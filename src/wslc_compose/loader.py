"""Load, interpolate and normalize a compose file into a Project."""

from __future__ import annotations

import copy
import os
import re
import shlex
from ipaddress import IPv4Address, IPv4Network
from typing import Dict, List, Optional, Tuple, Union

import yaml

from wslc_compose.interpolation import InterpolationError, interpolate, interpolate_tree
from wslc_compose.model import (
    BuildConfig,
    FileMount,
    FileResource,
    HealthCheck,
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
    "privileged": "privileged mode is not supported by wslc",
    "devices": "device mapping is not supported by wslc (see 'gpus' for GPUs)",
    "sysctls": "sysctls are not supported by wslc",
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


class _ComposeYamlLoader(yaml.SafeLoader):
    """SafeLoader with YAML 1.2 scalars, as Docker Compose reads compose files.

    PyYAML follows YAML 1.1: an unquoted `22:22` is the base-60 integer 1342, and
    `yes`/`no`/`on`/`off` are booleans (so `OFF: 1` becomes the key `False`). Here only
    `true`/`false` are booleans and `22:22` stays a string; boolean settings still
    accept `yes`/`no`/`on`/`off` through `_as_bool`.
    """


def _drop_sexagesimal_resolvers() -> None:
    resolvers = {}
    for first, entries in yaml.SafeLoader.yaml_implicit_resolvers.items():
        kept = [
            (tag, regexp) for tag, regexp in entries
            if tag not in ("tag:yaml.org,2002:int", "tag:yaml.org,2002:float",
                           "tag:yaml.org,2002:bool")
        ]
        resolvers[first] = kept
    # Re-add bool (YAML 1.2) and int/float without their base-60 alternatives.
    int_re = re.compile(r"""^(?:[-+]?0b[0-1_]+
                    |[-+]?0[0-7_]+
                    |[-+]?(?:0|[1-9][0-9_]*)
                    |[-+]?0x[0-9a-fA-F_]+)$""", re.X)
    float_re = re.compile(r"""^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?
                    |\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?
                    |[-+]?\.(?:inf|Inf|INF)
                    |\.(?:nan|NaN|NAN))$""", re.X)
    _ComposeYamlLoader.yaml_implicit_resolvers = resolvers
    _ComposeYamlLoader.add_implicit_resolver(
        "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))
    _ComposeYamlLoader.add_implicit_resolver("tag:yaml.org,2002:int", int_re, list("-+0123456789"))
    _ComposeYamlLoader.add_implicit_resolver("tag:yaml.org,2002:float", float_re, list("-+0123456789."))


_drop_sexagesimal_resolvers()


class _Reset:
    """`!reset`: drop the value inherited from earlier files (Compose merge tag)."""


class _Override:
    """`!override`: replace the inherited value instead of merging into it."""

    def __init__(self, value):
        self.value = value


def _construct_tagged(loader, node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)


_ComposeYamlLoader.add_constructor("!reset", lambda loader, node: _Reset())
_ComposeYamlLoader.add_constructor(
    "!override", lambda loader, node: _Override(_construct_tagged(loader, node)))


def _interpolated(node, env):
    """`interpolate_tree` that also reaches inside `!override` values."""
    try:
        if isinstance(node, _Override):
            return _Override(_interpolated(node.value, env))
        if isinstance(node, dict):
            return {k: _interpolated(v, env) for k, v in node.items()}
        if isinstance(node, list):
            return [_interpolated(v, env) for v in node]
        return interpolate_tree(node, env)
    except InterpolationError as exc:
        raise ComposeError(str(exc)) from None


def _strip_merge_tags(node):
    """Apply the `!reset` / `!override` tags left after merging (or in a single file)."""
    if isinstance(node, _Override):
        return _strip_merge_tags(node.value)
    if isinstance(node, dict):
        return {k: _strip_merge_tags(v) for k, v in node.items() if not isinstance(v, _Reset)}
    if isinstance(node, list):
        return [_strip_merge_tags(v) for v in node if not isinstance(v, _Reset)]
    return node


def _yaml_load(fh, path: str):
    try:
        return yaml.load(fh, Loader=_ComposeYamlLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise ComposeError(f"{path}: invalid YAML: {exc}") from exc


_TRUE = ("true", "yes", "on", "1")
_FALSE = ("false", "no", "off", "0", "")


def _as_bool(value, what: str) -> bool:
    """Compose boolean: a YAML bool, or a string such as "false" produced by interpolation."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, (dict, list)):
        return bool(value)  # legacy `external: {name: ...}`
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    raise ComposeError(f"{what}: expected a boolean, got {value!r}")


def _scalar(value) -> str:
    """String form of a YAML scalar as Compose writes it (true, not Python's True)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def find_compose_file(directory: str) -> Optional[str]:
    for name in COMPOSE_FILENAMES:
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            return path
    return None


def load_dotenv(path: str, environ: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Read a .env file. `${VAR}` references in unquoted and double-quoted values are
    expanded from the variables defined above them and the process environment."""
    if not os.path.isfile(path):
        return {}
    return {key: value for key, value in read_dotenv_lines(path, environ) if value is not None}


def read_dotenv_lines(path: str, environ: Optional[Dict[str, str]] = None):
    """(key, value) for every variable line of a .env-style file, in order.

    `value` is None for a bare `KEY` line (no `=`), which env files use to pass a
    variable through from the environment.
    """
    environ = dict(os.environ) if environ is None else environ
    env: Dict[str, str] = {}
    entries = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                entries.append((line, None))
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            raw_value = value.strip()
            parsed = _dotenv_value(raw_value)
            if not raw_value.startswith("'"):
                parsed = interpolate(parsed, {**env, **environ}, dotenv=True)
            env[key] = parsed
            entries.append((key, parsed))
    return entries


def _dotenv_value(value: str) -> str:
    """Strip quotes and inline comments like Docker Compose's .env reader.

    A quoted value ends at its closing quote (anything after it, e.g. `# comment`, is
    dropped); an unquoted value ends before ` #`. Escapes and `${VAR}` references are
    kept literally (Windows paths such as `C:\\new` must not change meaning).
    """
    if value[:1] in ("\"", "'"):
        if len(value) >= 2 and value[-1] == value[0]:
            return value[1:-1]  # fully quoted (unchanged behaviour)
        end = value.find(value[0], 1)
        if end > 0 and value[end + 1:].lstrip().startswith("#"):
            return value[1:end]  # "value" # comment
        return value
    comment = re.search(r"\s#", value)
    return value[:comment.start()].rstrip() if comment else value


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


def _parse_healthcheck(value) -> Optional[HealthCheck]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ComposeError("healthcheck: expected mapping")
    disabled = _as_bool(value.get("disable", False), "healthcheck.disable")
    test = value.get("test", [])
    if isinstance(test, str):
        test = ["CMD-SHELL", test]
    else:
        test = [str(part) for part in test]
    if test and test[0] not in ("CMD", "CMD-SHELL", "NONE"):
        raise ComposeError("healthcheck.test: expected CMD, CMD-SHELL, or NONE")
    return HealthCheck(test=test, disabled=disabled or (test and test[0] == "NONE"),
                       interval=value.get("interval"), timeout=value.get("timeout"),
                       start_period=value.get("start_period"), retries=value.get("retries"))


def _duration_seconds(value) -> int:
    """Normalize Compose durations such as 30s and 1m30s to whole seconds."""
    raw = str(value).strip().lower()
    if raw.isdigit():
        return int(raw)
    matches = list(re.finditer(r"(\d+(?:\.\d+)?)(ms|h|m|s)", raw))
    if not matches or "".join(match.group(0) for match in matches) != raw:
        raise ComposeError(f"stop_grace_period: invalid duration {value!r}")
    seconds = sum(float(match.group(1)) * {"h": 3600, "m": 60, "s": 1, "ms": 0.001}[match.group(2)] for match in matches)
    return max(1, round(seconds))


def _as_mapping(value, what: str) -> Dict[str, str]:
    """Accept both list ("K=V") and mapping syntax for labels/build args."""
    result: Dict[str, str] = {}
    if value is None:
        return result
    if isinstance(value, dict):
        for k, v in value.items():
            result[str(k)] = "" if v is None else _scalar(v)
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
            result[str(k)] = None if v is None else _scalar(v)
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
        pub = str(published) if published not in (None, "") else None
        if host_ip:
            host_ip = str(host_ip)
            if ":" in host_ip and not host_ip.startswith("["):
                host_ip = f"[{host_ip}]"  # IPv6
            pub = f"{host_ip}:{pub or ''}"  # "ip:" = that address, random host port
        return [PortMapping(target, pub, str(spec.get("protocol", "tcp")))]

    text = str(spec)
    protocol = "tcp"
    if "/" in text:
        text, protocol = text.rsplit("/", 1)

    if text.startswith("["):  # IPv6 host address: [::1]:8080:80, [::1]::80
        close = text.find("]")
        rest = text[close + 1:]
        if close == -1 or not rest.startswith(":") or rest.count(":") != 2:
            raise ComposeError(f"invalid port mapping: {spec!r}")
        ip6 = text[:close + 1]
        host_ports, target = rest[1:].split(":")
        mapped = parse_port(f"{host_ports or '0'}:{target}/{protocol}")
        return [PortMapping(m.target, f"{ip6}:{m.published if host_ports else ''}", protocol)
                for m in mapped]

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
        try:
            if "-" in rng:
                lo, hi = rng.split("-", 1)
                return list(range(int(lo), int(hi) + 1))
            return [int(rng)]
        except ValueError:
            raise ComposeError(f"invalid port mapping: {spec!r}") from None

    targets = expand(target)
    if host is None:
        return [PortMapping(t, None, protocol) for t in targets]
    ip_prefix = ""
    host_ports = host
    if host.count(":") == 1:  # ip:port form
        ip_prefix, host_ports = host.split(":", 1)
        ip_prefix += ":"
        if not host_ports:  # "ip::target": that address, random host port
            return [PortMapping(t, ip_prefix, protocol) for t in targets]
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
        source.startswith(("/", "./", "../", "~", "\\\\", ".\\", "..\\"))
        or source in (".", "..")
        or bool(_WIN_PATH_RE.match(source))
    )


# Compose long-syntax mount options -> `wslc run --mount` keys (verified on wslc 3.0.5).
# None: accepted by Compose but not supported by wslc (dropped with a warning).
_MOUNT_OPTION_KEYS = {
    ("tmpfs", "size"): "tmpfs-size",
    ("tmpfs", "mode"): "tmpfs-mode",
    ("bind", "recursive"): "bind-recursive",
    ("bind", "propagation"): None,
    ("bind", "selinux"): None,
    ("volume", "nocopy"): None,
    ("volume", "subpath"): None,
}


def _mount_options(spec: dict, warnings: List[str]) -> List[str]:
    options = []
    for section in ("bind", "volume", "tmpfs"):
        for key, value in (spec.get(section) or {}).items():
            if (section, key) == ("bind", "create_host_path"):
                continue  # Compose-side behaviour, not a mount option
            flag = _MOUNT_OPTION_KEYS.get((section, key))
            if flag is None:
                warnings.append(f"mount {spec.get('target')}: {section}.{key} is not "
                                "supported by wslc (ignoring it)")
                continue
            if isinstance(value, str) and value.strip().lower() in ("yes", "on", "no", "off"):
                value = value.strip().lower() in ("yes", "on")  # YAML 1.1 spellings
            if isinstance(value, bool):
                value = str(value).lower()
            options.append(f"{flag}={value}")
    return options


def parse_volume(spec, project_dir: str, warnings: Optional[List[str]] = None) -> VolumeMount:
    if isinstance(spec, dict):  # long syntax
        vtype = spec.get("type", "volume")
        source = spec.get("source")
        target = spec.get("target")
        if not target:
            raise ComposeError(f"volume entry missing target: {spec!r}")
        read_only = _as_bool(spec.get("read_only", False), "volumes.read_only")
        if vtype == "bind" and source:
            source = _resolve_bind_source(source, project_dir)
        options = _mount_options(spec, [] if warnings is None else warnings)
        return VolumeMount(vtype, source, target, read_only, options, True)

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
    if source.startswith((".\\", "..\\")) or source in (".", ".."):
        source = source.replace("\\", "/")  # Windows-style relative path
    source = os.path.expanduser(source)
    if not os.path.isabs(source):
        source = os.path.normpath(os.path.join(project_dir, source))
    return source


DEPENDS_ON_CONDITIONS = ("service_started", "service_healthy", "service_completed_successfully")


def _parse_required(name: str, value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "yes", "no"):
        return value.strip().lower() in ("true", "yes")
    raise ComposeError(f"depends_on.{name}.required: expected true or false, got {value!r}")


def _parse_depends_on(value) -> Tuple[List[str], Dict[str, str], List[str]]:
    """Return (dependency names, {name: non-default condition}, optional names)."""
    if value is None:
        return [], {}, []
    if isinstance(value, list):
        return [str(v) for v in value], {}, []
    if isinstance(value, dict):
        deps: List[str] = []
        conditions: Dict[str, str] = {}
        optional: List[str] = []
        for name, cfg in value.items():
            name = str(name)
            deps.append(name)
            if cfg is not None and not isinstance(cfg, dict):
                raise ComposeError(f"depends_on.{name}: expected a mapping")
            cfg = cfg or {}
            condition = cfg.get("condition", "service_started")
            if condition not in DEPENDS_ON_CONDITIONS:
                raise ComposeError(
                    f"depends_on.{name}: unsupported condition {condition!r} "
                    f"(expected one of: {', '.join(DEPENDS_ON_CONDITIONS)})"
                )
            if condition != "service_started":
                conditions[name] = condition
            if not _parse_required(name, cfg.get("required", True)):
                optional.append(name)
        return deps, conditions, optional
    raise ComposeError("depends_on: expected list or mapping")


def _capabilities(service: str, key: str, value) -> List[str]:
    """Normalized Linux capability names: upper case, no CAP_ prefix, de-duplicated."""
    result: List[str] = []
    for item in _as_list(value):
        name = str(item).strip().upper()
        name = name[4:] if name.startswith("CAP_") else name
        if not name:
            raise ComposeError(f"service {service!r}: {key} contains an empty capability")
        if name not in result:
            result.append(name)
    return result


def _parse_env_files(value, project_dir: str, service: str) -> List[str]:
    """`env_file`: a path, or a list of paths / `{path, required}` mappings."""
    entries = value if isinstance(value, list) else ([] if value is None else [value])
    paths: List[str] = []
    for entry in entries:
        required = True
        if isinstance(entry, dict):
            if not entry.get("path"):
                raise ComposeError(f"service {service!r}: env_file entries need a 'path'")
            required = _as_bool(entry.get("required", True), f"service {service!r}: env_file.required")
            entry = entry["path"]
        path = _resolve_bind_source(str(entry), project_dir)
        foreign_windows_path = os.name != "nt" and (
            bool(_WIN_PATH_RE.match(path)) or path.startswith("\\\\"))
        if not required and not foreign_windows_path and not os.path.isfile(path):
            continue
        paths.append(path)
    return paths


def _parse_build(value, project_dir: str) -> BuildConfig:
    if isinstance(value, str):
        return BuildConfig(context=_resolve_bind_source(value, project_dir))
    if "secrets" in value:
        raise ComposeError(
            "build.secrets is not supported: 'wslc build' has no --secret option "
            "(do not pass credentials as build args)"
        )
    context = _resolve_bind_source(value.get("context", "."), project_dir)
    return BuildConfig(
        context=context,
        dockerfile=value.get("dockerfile"),
        args=_as_mapping(value.get("args"), "build.args"),
        target=value.get("target"),
        pull=_as_bool(value.get("pull", False), "build.pull"),
        no_cache=_as_bool(value.get("no_cache", False), "build.no_cache"),
    )

_MERGED_SECTIONS = ("services", "networks", "volumes", "secrets", "configs")


def _rebase(path: str, base_dir: str) -> str:
    """Make a relative host path absolute against `base_dir`.

    Absolute, `~`, Windows (`C:\\`, UNC) and remote (`https://`, `git@`) paths are kept.
    """
    if (os.path.isabs(path) or path.startswith(("~", "\\\\", "git@")) or "://" in path
            or _WIN_PATH_RE.match(path)):
        return path
    if path.startswith((".\\", "..\\")):
        path = path.replace("\\", "/")
    return os.path.normpath(os.path.join(base_dir, path))


def _rebase_service_paths(services: dict, base_dir: str) -> None:
    """Anchor relative paths of an included file's services to that file's directory."""
    for cfg in services.values():
        if not isinstance(cfg, dict):
            continue
        extends = cfg.get("extends")
        if isinstance(extends, dict) and isinstance(extends.get("file"), str):
            cfg["extends"] = {**extends, "file": _rebase(extends["file"], base_dir)}
        build = cfg.get("build")
        if isinstance(build, str):
            cfg["build"] = _rebase(build, base_dir)
        elif isinstance(build, dict) and isinstance(build.get("context"), str):
            build["context"] = _rebase(build["context"], base_dir)
        env_file = cfg.get("env_file")
        if isinstance(env_file, str):
            cfg["env_file"] = _rebase(env_file, base_dir)
        elif isinstance(env_file, list):
            cfg["env_file"] = [
                _rebase(f, base_dir) if isinstance(f, str)
                else {**f, "path": _rebase(f["path"], base_dir)}
                if isinstance(f, dict) and isinstance(f.get("path"), str) else f
                for f in env_file
            ]
        volumes = cfg.get("volumes")
        if isinstance(volumes, list):
            rebased = []
            for vol in volumes:
                if isinstance(vol, str) and vol.startswith("."):
                    source, sep, rest = vol.partition(":")
                    vol = _rebase(source, base_dir) + sep + rest
                elif (isinstance(vol, dict) and isinstance(vol.get("source"), str)
                      and (vol.get("type") == "bind" or vol["source"].startswith("."))):
                    vol = {**vol, "source": _rebase(vol["source"], base_dir)}
                rebased.append(vol)
            cfg["volumes"] = rebased


def _resolve_extends(services: dict, main_file: str, env: Dict[str, str]) -> None:
    """Expand `extends:` in place: the base service's settings, overlaid by the local ones.

    `extends: name`, `{service: name}` or `{service: name, file: other.yaml}` (relative
    paths of a service taken from another directory are anchored to that file).
    """
    base_dir = os.path.dirname(main_file)
    cache: Dict[Tuple[str, str], dict] = {}

    def load(path: str) -> dict:
        raw = _strip_merge_tags(_interpolated(_read_compose(path), env))
        found = raw.get("services") or {}
        if os.path.dirname(path) != base_dir:
            _rebase_service_paths(found, os.path.dirname(path))
        return found

    def resolve(name: str, pool: dict, where: str, stack: Tuple[Tuple[str, str], ...]) -> dict:
        key = (where, name)
        if key in cache:
            return cache[key]
        if key in stack:
            chain = " -> ".join(n for _, n in stack + (key,))
            raise ComposeError(f"circular extends: {chain}")
        cfg = pool.get(name)
        if not isinstance(cfg, dict):
            raise ComposeError(f"extends: service {name!r} not found in {where}")
        extends = cfg.get("extends")
        if extends is None:
            cache[key] = cfg
            return cfg
        if isinstance(extends, str):
            extends = {"service": extends}
        if not isinstance(extends, dict) or not extends.get("service"):
            raise ComposeError(f"service {name!r}: extends needs a 'service'")
        base_pool, base_where = pool, where
        if extends.get("file"):
            path = str(extends["file"])
            path = path if os.path.isabs(path) else os.path.join(os.path.dirname(where), path)
            path = os.path.abspath(path)
            if not os.path.isfile(path):
                raise ComposeError(f"service {name!r}: extends file not found: {extends['file']}")
            base_pool, base_where = load(path), path
        base = resolve(str(extends["service"]), base_pool, base_where, stack + (key,))
        local = {k: v for k, v in cfg.items() if k != "extends"}
        merged = _merge_dicts(copy.deepcopy({k: v for k, v in base.items() if k != "extends"}), local)
        cache[key] = merged
        return merged

    for name in list(services):
        if isinstance(services[name], dict) and "extends" in services[name]:
            services[name] = resolve(name, services, main_file, ())


def _include_paths(entry, compose_file: str) -> List[str]:
    """Normalize one `include:` entry (string, or mapping with `path`) to a path list."""
    if isinstance(entry, dict):
        entry = entry.get("path")
    paths = entry if isinstance(entry, list) else [entry]
    if not paths or not all(isinstance(p, str) and p for p in paths):
        raise ComposeError(f"{compose_file}: malformed 'include' entry")
    return paths


def _read_compose(path: str, stack: Tuple[str, ...] = ()) -> dict:
    """Read a compose file and recursively merge the files listed in its `include:`.

    Relative paths inside included files are resolved against the included file's own
    directory (as with Docker Compose). The same service/network/volume name defined in two
    files is an error.
    """
    path = os.path.abspath(path)
    if path in stack:
        chain = " -> ".join(stack + (path,))
        raise ComposeError(f"circular include: {chain}")
    with open(path, encoding="utf-8") as fh:
        raw = _yaml_load(fh, path)
    if not isinstance(raw, dict):
        raise ComposeError(f"{path}: not a valid compose file")
    includes = raw.pop("include", None) or []
    if not isinstance(includes, list):
        raise ComposeError(f"{path}: 'include' must be a list")
    base_dir = os.path.dirname(path)
    for entry in includes:
        for rel in _include_paths(entry, path):
            inc_path = os.path.abspath(rel if os.path.isabs(rel) else os.path.join(base_dir, rel))
            if not os.path.isfile(inc_path):
                raise ComposeError(f"{path}: included file not found: {rel}")
            included = _read_compose(inc_path, stack + (path,))
            if os.path.dirname(inc_path) != base_dir:
                inc_dir = os.path.dirname(inc_path)
                _rebase_service_paths(included.get("services") or {}, inc_dir)
                for section in ("secrets", "configs"):
                    for cfg in (included.get(section) or {}).values():
                        if isinstance(cfg, dict) and isinstance(cfg.get("file"), str):
                            cfg["file"] = _rebase(cfg["file"], inc_dir)
            for section in _MERGED_SECTIONS:
                items = included.get(section) or {}
                target = raw.setdefault(section, {})
                if target is None:
                    target = raw[section] = {}
                for key, value in items.items():
                    if key in target:
                        raise ComposeError(
                            f"{path}: {section[:-1]} {key!r} from {rel} conflicts with an "
                            "existing definition"
                        )
                    target[key] = value
    return raw


# Service keys whose list values are concatenated (not replaced) when files are overlaid.
_APPEND_KEYS = frozenset(
    {"ports", "expose", "dns", "dns_search", "dns_opt", "tmpfs", "env_file", "profiles",
     "secrets", "configs", "extra_hosts", "cap_add", "cap_drop", "devices", "security_opt"}
)
# Keys that may be written as a list ("K=V") or a mapping; merged as mappings.
_KEYVALUE_KEYS = frozenset({"environment", "labels", "sysctls"})
# Keys written as a list of names or a mapping name -> options; merged as mappings.
_NAMED_KEYS = frozenset({"networks", "depends_on"})


def _to_mapping(value, keyvalue: bool):
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        result = {}
        for item in value:
            if keyvalue:
                key, sep, val = str(item).partition("=")
                result[key] = val if sep else None
            else:
                result[str(item)] = None
        return result
    return value


def _volume_target(entry):
    try:
        return parse_volume(entry, ".").target
    except ComposeError:
        return str(entry)


def _merge_value(key, base, over):
    if key in _KEYVALUE_KEYS or key in _NAMED_KEYS:
        b, o = _to_mapping(base, key in _KEYVALUE_KEYS), _to_mapping(over, key in _KEYVALUE_KEYS)
        if isinstance(b, dict) and isinstance(o, dict):
            return _merge_dicts(b, o)
        return over
    if key == "volumes" and isinstance(base, list) and isinstance(over, list):
        overridden = {_volume_target(v) for v in over}
        return [v for v in base if _volume_target(v) not in overridden] + list(over)
    if key in _APPEND_KEYS and isinstance(base, list) and isinstance(over, list):
        return base + [v for v in over if v not in base]
    if isinstance(base, dict) and isinstance(over, dict):
        return _merge_dicts(base, over)
    return over


def _merge_dicts(base: dict, over: dict) -> dict:
    result = dict(base)
    for key, value in over.items():
        if isinstance(value, _Reset):
            result.pop(key, None)
        elif isinstance(value, _Override):
            result[key] = value.value
        else:
            result[key] = _merge_value(key, result[key], value) if key in result else value
    return result


def merge_compose(base: dict, override: dict) -> dict:
    """Overlay `override` on `base` like `docker compose -f a.yml -f b.yml`."""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, _Reset):
            result.pop(key, None)
        elif isinstance(value, _Override):
            result[key] = value.value
        elif key in ("services", "networks", "volumes", "secrets", "configs") and key in result:
            merged = dict(result[key] or {})
            for name, cfg in (value or {}).items():
                if isinstance(cfg, _Reset):
                    merged.pop(name, None)
                elif isinstance(cfg, _Override):
                    merged[name] = cfg.value
                elif name in merged and isinstance(merged[name], dict) and isinstance(cfg, dict):
                    merged[name] = _merge_dicts(merged[name], cfg)
                else:
                    merged[name] = cfg
            result[key] = merged
        else:
            result[key] = value
    return result


def _parse_file_mounts(value, kind, definitions, default_target, service_name):
    """Parse a service's `secrets:` / `configs:` list into mounts.

    Returns (mounts, warnings). Every reference must exist in the top-level `kind` section.
    """
    mounts: List[FileMount] = []
    warnings: List[str] = []
    targets = set()
    if value is None:
        return mounts, warnings
    if not isinstance(value, list):
        raise ComposeError(f"service {service_name!r}: {kind}: expected a list")
    label = kind[:-1]
    for item in value:
        if isinstance(item, str):
            source, target, ignored = item, item, []
        elif isinstance(item, dict) and item.get("source"):
            source = str(item["source"])
            target = str(item.get("target") or source)
            ignored = [k for k in ("uid", "gid", "mode") if k in item]
        else:
            raise ComposeError(f"service {service_name!r}: {label} entries require a source")
        if source not in definitions:
            raise ComposeError(f"service {service_name!r} references undefined {label} {source!r}")
        if target.startswith("/"):
            container_target = target
        else:
            if target in ("", ".", "..") or "/" in target or "\\" in target:
                raise ComposeError(
                    f"service {service_name!r}: {label} {source!r} has invalid target "
                    f"{target!r}; use a filename or an absolute container path"
                )
            container_target = default_target + target
        if container_target in targets:
            raise ComposeError(
                f"service {service_name!r}: multiple {kind} target {container_target!r}"
            )
        targets.add(container_target)
        mounts.append(FileMount(source, container_target))
        if ignored:
            warnings.append(
                f"{label} {source!r}: {', '.join(ignored)} cannot be enforced by wslc file "
                "mounts and will be ignored"
            )
    return mounts, warnings


def _parse_file_resources(kind, value, project_dir: str):
    """Parse top-level `secrets:` / `configs:` (file-backed only)."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ComposeError(f"{kind}: expected a mapping")
    label = kind[:-1]
    result = {}
    for key, cfg in value.items():
        if not isinstance(cfg, dict):
            raise ComposeError(f"{label} {key!r}: expected a mapping with a 'file' source")
        if _as_bool(cfg.get("external"), f"{label} {key!r}: external"):
            raise ComposeError(
                f"{label} {key!r}: external {kind} are not supported by wslc; use a file"
            )
        if "environment" in cfg or "content" in cfg:
            raise ComposeError(
                f"{label} {key!r}: only file-backed {kind} are supported by wslc; write the "
                "value to a protected host file and use 'file'"
            )
        if not cfg.get("file"):
            raise ComposeError(f"{label} {key!r}: only file-backed {kind} are supported by wslc")
        source = _resolve_bind_source(str(cfg["file"]), project_dir)
        foreign_windows_path = os.name != "nt" and (
            bool(_WIN_PATH_RE.match(source)) or source.startswith("\\\\")
        )
        if not foreign_windows_path and not os.path.isfile(source):
            raise ComposeError(f"{label} {key!r}: file not found: {source}")
        result[str(key)] = FileResource(str(key), source)
    return result


def _parse_network(key: str, cfg, project_name: str) -> Network:
    cfg = {} if cfg is None else cfg
    if not isinstance(cfg, dict):
        raise ComposeError(f"networks.{key}: expected mapping")
    external = _as_bool(cfg.get("external", False), f"networks.{key}.external")
    name = cfg.get("name") or (key if external else f"{project_name}_{key}")
    network = Network(key=key, name=str(name), external=external,
                      internal=_as_bool(cfg.get("internal", False), f"networks.{key}.internal"),
                      driver=cfg.get("driver"),
                      driver_opts=_as_mapping(cfg.get("driver_opts"), "network driver_opts"),
                      labels=_as_mapping(cfg.get("labels"), "network labels"))
    for option in cfg:
        if option not in {"name", "external", "internal", "driver", "driver_opts", "labels",
                          "ipam", "enable_ipv4", "enable_ipv6", "attachable"} and not str(option).startswith("x-"):
            raise ComposeError(f"networks.{key}: unsupported setting {option!r}")
    ipv4_disabled = cfg.get("enable_ipv4") is not None and not _as_bool(
        cfg["enable_ipv4"], f"networks.{key}.enable_ipv4")
    if (_as_bool(cfg.get("enable_ipv6"), f"networks.{key}.enable_ipv6") or ipv4_disabled
            or _as_bool(cfg.get("attachable"), f"networks.{key}.attachable")):
        raise ComposeError(f"networks.{key}: WSLC supports IPv4 networks without attachable mode")
    ipam = {} if cfg.get("ipam") is None else cfg["ipam"]
    if not isinstance(ipam, dict):
        raise ComposeError(f"networks.{key}.ipam: expected mapping")
    if ipam.get("driver") not in (None, "default") or ipam.get("options"):
        raise ComposeError(f"networks.{key}.ipam: custom IPAM drivers/options are not supported by WSLC")
    if any(option not in {"driver", "options", "config"} for option in ipam):
        raise ComposeError(f"networks.{key}.ipam: unsupported setting")
    configs = [] if ipam.get("config") is None else ipam["config"]
    if not isinstance(configs, list) or len(configs) > 1:
        raise ComposeError(f"networks.{key}.ipam.config: WSLC supports one IPv4 subnet per network")
    if not configs:
        return network
    config = configs[0]
    if not isinstance(config, dict):
        raise ComposeError(f"networks.{key}.ipam.config: expected mapping")
    if any(option not in {"subnet", "gateway", "ip_range", "aux_addresses"} for option in config) or config.get("aux_addresses"):
        raise ComposeError(f"networks.{key}.ipam.config: unsupported setting (including aux_addresses)")
    try:
        subnet = IPv4Network(str(config["subnet"]).strip())
        if subnet.num_addresses < 4:
            raise ValueError("subnet needs network, gateway and broadcast addresses")
        network.subnet = str(subnet)
        if config.get("gateway") is not None:
            gateway = IPv4Address(str(config["gateway"]).strip())
            if gateway not in subnet or gateway in (subnet.network_address, subnet.broadcast_address):
                raise ValueError("gateway must be a usable address inside the subnet")
            network.gateway = str(gateway)
        if config.get("ip_range") is not None:
            ip_range = IPv4Network(str(config["ip_range"]).strip())
            if not ip_range.subnet_of(subnet):
                raise ValueError("ip_range must be inside the subnet")
            network.ip_range = str(ip_range)
    except (KeyError, ValueError) as exc:
        raise ComposeError(f"networks.{key}.ipam.config: invalid IPv4 subnet/gateway/ip_range: {exc}") from exc
    return network


def load_project(
    compose_file: Union[str, List[str]],
    project_name: Optional[str] = None,
    env_file: Optional[str] = None,
) -> Project:
    """Load one compose file, or several that are overlaid in order (later files win).

    Relative paths resolve against the directory of the *first* file, as in Docker Compose.
    """
    compose_files = [os.path.abspath(f) for f in ([compose_file] if isinstance(compose_file, str) else compose_file)]
    if not compose_files:
        raise ComposeError("no compose file given")
    compose_file = compose_files[0]
    project_dir = os.path.dirname(compose_file)

    dotenv_path = env_file or os.path.join(project_dir, ".env")
    try:
        env = dict(load_dotenv(dotenv_path))
    except InterpolationError as exc:
        raise ComposeError(f"{dotenv_path}: {exc}") from None
    env.update(os.environ)  # process env wins

    # Each file is interpolated before the overlay, as Docker Compose does, so merge
    # keys (a volume's container path...) are compared on their resolved values.
    raw = _interpolated(_read_compose(compose_files[0]), env)
    for extra in compose_files[1:]:
        raw = merge_compose(raw, _interpolated(_read_compose(extra), env))
    raw = _strip_merge_tags(raw)
    if not raw.get("services"):
        where = compose_file if len(compose_files) == 1 else ", ".join(compose_files)
        raise ComposeError(f"{where}: no 'services' section found")
    _resolve_extends(raw["services"], compose_file, env)

    name = normalize_project_name(
        project_name
        or env.get("COMPOSE_PROJECT_NAME")
        or (str(raw["name"]) if raw.get("name") else "")
        or os.path.basename(project_dir)
    )
    project = Project(
        name=name,
        directory=project_dir,
        compose_files=compose_files,
        env_file=os.path.abspath(env_file) if env_file else None,
    )

    # --- top level networks & volumes -------------------------------------
    raw_networks = raw.get("networks") or {}
    if not isinstance(raw_networks, dict):
        raise ComposeError("networks: expected mapping")
    for key, cfg in raw_networks.items():
        project.networks[key] = _parse_network(str(key), cfg, name)

    raw_volumes = raw.get("volumes") or {}
    for key, cfg in raw_volumes.items():
        cfg = cfg or {}
        external = _as_bool(cfg.get("external", False), f"volumes.{key}.external")
        vol_name = cfg.get("name") or (key if external else f"{name}_{key}")
        project.volumes[key] = Volume(key=key, name=vol_name, external=external,
                                      driver=cfg.get("driver"),
                                      driver_opts=_as_mapping(cfg.get("driver_opts"), "driver_opts"),
                                      labels=_as_mapping(cfg.get("labels"), "labels"))

    project.secrets = _parse_file_resources("secrets", raw.get("secrets"), project_dir)
    project.configs = _parse_file_resources("configs", raw.get("configs"), project_dir)

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
        svc.environment = {
            # `FOO` without a value takes it from the shell / .env like Docker Compose;
            # wslc.exe launched from WSL cannot see the Linux environment itself.
            key: env.get(key) if value is None else value
            for key, value in _as_environment(cfg.get("environment")).items()
        }
        svc.env_files = _parse_env_files(cfg.get("env_file"), project_dir, svc_name)
        for spec in cfg.get("ports") or []:
            svc.ports.extend(parse_port(spec))
        for spec in cfg.get("volumes") or []:
            mount_warnings: List[str] = []
            svc.volumes.append(parse_volume(spec, project_dir, mount_warnings))
            project.warnings.extend(f"{svc_name}: {w}" for w in mount_warnings)
        svc.tmpfs = _as_list(cfg.get("tmpfs"))

        # network_mode replaces ordinary Compose network endpoints.
        svc.network_mode = cfg.get("network_mode")
        # networks: list or mapping (with aliases); default network otherwise
        raw_svc_networks = cfg.get("networks")
        if svc.network_mode:
            keys = []
            aliases = {}
        elif raw_svc_networks is None:
            keys = ["default"]
            aliases: Dict[str, List[str]] = {}
        elif isinstance(raw_svc_networks, list):
            keys = [str(k) for k in raw_svc_networks]
            aliases = {}
        else:
            if not isinstance(raw_svc_networks, dict) or any(
                value is not None and not isinstance(value, dict) for value in raw_svc_networks.values()
            ):
                raise ComposeError(f"service {svc_name!r}: networks must be a list or endpoint mapping")
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
            endpoint = raw_svc_networks.get(key) if isinstance(raw_svc_networks, dict) else None
            if isinstance(endpoint, dict) and any(
                option not in {"aliases", "ipv4_address"} and not str(option).startswith("x-")
                for option in endpoint
            ):
                raise ComposeError(f"service {svc_name!r}, network {key!r}: unsupported endpoint setting")
            if isinstance(endpoint, dict) and "ipv4_address" in endpoint:
                try:
                    address = str(IPv4Address(str(endpoint["ipv4_address"]).strip()))
                except ValueError as exc:
                    raise ComposeError(
                        f"service {svc_name!r}, network {key!r}: invalid ipv4_address"
                    ) from exc
                if net.subnet:
                    subnet = IPv4Network(net.subnet)
                    if (IPv4Address(address) not in subnet or address == net.gateway or
                            IPv4Address(address) in (subnet.network_address, subnet.broadcast_address)):
                        raise ComposeError(f"service {svc_name!r}, network {key!r}: ipv4_address is not usable in its subnet")
                svc.network_ipv4_addresses[net.name] = address
        if len(svc.networks) != len(set(svc.networks)):
            raise ComposeError(f"service {svc_name!r}: network declarations resolve to the same runtime name")

        # named volumes must be declared
        for mount in svc.volumes:
            if mount.type == "volume":
                if mount.source not in project.volumes:
                    raise ComposeError(
                        f"service {svc_name!r} references undefined volume {mount.source!r}"
                    )
                mount.source = project.volumes[mount.source].name

        try:
            svc.depends_on, svc.depends_on_conditions, svc.depends_on_optional = _parse_depends_on(
                cfg.get("depends_on"))
        except ComposeError as exc:
            raise ComposeError(f"service {svc_name!r}: {exc}") from exc

        svc.hostname = cfg.get("hostname")
        svc.domainname = cfg.get("domainname")
        svc.dns = _as_list(cfg.get("dns"))
        svc.dns_search = _as_list(cfg.get("dns_search"))
        svc.dns_opt = _as_list(cfg.get("dns_opt"))
        svc.user = str(cfg["user"]) if cfg.get("user") is not None else None
        svc.working_dir = cfg.get("working_dir")
        svc.labels = _as_mapping(cfg.get("labels"), "labels")
        svc.stop_signal = cfg.get("stop_signal")
        if cfg.get("stop_grace_period") is not None:
            svc.stop_timeout_seconds = _duration_seconds(cfg["stop_grace_period"])
        svc.shm_size = str(cfg["shm_size"]) if cfg.get("shm_size") is not None else None
        svc.stdin_open = _as_bool(cfg.get("stdin_open", False), f"service {svc_name!r}: stdin_open")
        svc.tty = _as_bool(cfg.get("tty", False), f"service {svc_name!r}: tty")
        svc.secrets, secret_warnings = _parse_file_mounts(
            cfg.get("secrets"), "secrets", project.secrets, "/run/secrets/", svc_name
        )
        svc.configs, config_warnings = _parse_file_mounts(
            cfg.get("configs"), "configs", project.configs, "/", svc_name
        )
        project.warnings.extend(f"{svc_name}: {w}" for w in secret_warnings + config_warnings)
        volume_targets = {mount.target for mount in svc.volumes}
        for mount in svc.secrets + svc.configs:
            if mount.target in volume_targets:
                raise ComposeError(
                    f"service {svc_name!r}: {mount.target!r} is used by both a volume and a "
                    "secret/config"
                )
        svc.healthcheck = _parse_healthcheck(cfg.get("healthcheck"))
        hosts = cfg.get("extra_hosts") or []
        svc.extra_hosts = ([f"{host}:{address}" for host, address in hosts.items()]
                           if isinstance(hosts, dict) else [str(host) for host in hosts])
        if svc.extra_hosts:
            project.warnings.append(f"{svc_name}: extra_hosts is applied after start; the entrypoint may not observe it")
        svc.profiles = _as_list(cfg.get("profiles"))
        restart = cfg.get("restart")
        svc.restart = "no" if restart is False else (str(restart) if restart is not None else None)
        if svc.restart not in (None, "no", "always"):
            project.warnings.append(
                f"{svc_name}: restart policy {svc.restart!r} is not supported by wslc "
                "(only 'always' and 'no'); ignoring it"
            )
        svc.cap_add = _capabilities(svc_name, "cap_add", cfg.get("cap_add"))
        svc.cap_drop = _capabilities(svc_name, "cap_drop", cfg.get("cap_drop"))

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
        if deploy.get("replicas") is not None:
            try:
                svc.replicas = int(deploy["replicas"])
            except (TypeError, ValueError):
                raise ComposeError(
                    f"service {svc_name!r}: deploy.replicas must be a number") from None
            if svc.replicas < 0:
                raise ComposeError(f"service {svc_name!r}: deploy.replicas cannot be negative")
        reservations = (deploy.get("resources") or {}).get("reservations") or {}
        for device in reservations.get("devices") or []:
            if "gpu" in (device.get("capabilities") or []):
                svc.gpus = "all" if device.get("count", "all") == "all" else str(device["count"])

        if cfg.get("gpus"):  # shorthand
            svc.gpus = str(cfg["gpus"])

        project.services[svc.name] = svc

    for svc in project.services.values():
        for dep, condition in svc.depends_on_conditions.items():
            target = project.services.get(dep)
            if target is None:
                raise ComposeError(f"service {svc.name!r} depends on undefined service {dep!r}")
            if condition == "service_healthy" and target.healthcheck and target.healthcheck.disabled:
                raise ComposeError(
                    f"service {svc.name!r}: depends_on {dep!r} is service_healthy but "
                    f"{dep!r} disables its healthcheck"
                )

    return project
