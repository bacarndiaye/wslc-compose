"""Build wslc CLI argument vectors from the normalized model (pure functions)."""

from __future__ import annotations

import os
from typing import Callable, List, Optional

from wslc_compose import LABEL_CONFIG_HASH, LABEL_INDEX, LABEL_PROJECT, LABEL_SERVICE
from wslc_compose.model import Project, Service


def run_args(
    project: Project,
    service: Service,
    index: int = 1,
    detach: bool = True,
    path_mapper: Optional[Callable[[str], str]] = None,
) -> List[str]:
    """Arguments for `wslc run` creating one container of a service."""
    mapper = path_mapper or (lambda p: p)
    args: List[str] = ["run"]
    if detach:
        args.append("-d")

    args += ["--name", project.container_name(service, index)]
    args += ["-l", f"{LABEL_PROJECT}={project.name}"]
    args += ["-l", f"{LABEL_SERVICE}={service.name}"]
    args += ["-l", f"{LABEL_INDEX}={index}"]
    args += ["-l", f"{LABEL_CONFIG_HASH}={service.config_hash()}"]
    for key, value in service.labels.items():
        args += ["-l", f"{key}={value}"]

    for env_file in service.env_files:
        args += ["--env-file", mapper(env_file)]
    for key, value in service.environment.items():
        args += ["-e", key if value is None else f"{key}={value}"]

    for port in service.ports:
        args += ["-p", port.to_flag()]

    for mount in service.volumes:
        if mount.type == "tmpfs":
            args += ["--tmpfs", mount.target]
            continue
        source = mount.source or ""
        if mount.type == "bind":
            source = mapper(source)
        spec = f"{source}:{mount.target}"
        if mount.read_only:
            spec += ":ro"
        args += ["-v", spec]
    for target in service.tmpfs:
        args += ["--tmpfs", target]

    if service.networks:
        args += ["--network", service.networks[0]]
        aliases = set(service.network_aliases.get(service.networks[0], []))
        aliases.add(service.name)
        for alias in sorted(aliases):
            args += ["--network-alias", alias]

    if service.hostname:
        args += ["-h", service.hostname]
    if service.domainname:
        args += ["--domainname", service.domainname]
    for dns in service.dns:
        args += ["--dns", dns]
    for domain in service.dns_search:
        args += ["--dns-search", domain]
    for opt in service.dns_opt:
        args += ["--dns-option", opt]

    if service.user:
        args += ["-u", service.user]
    if service.working_dir:
        args += ["-w", service.working_dir]
    if service.mem_limit:
        args += ["-m", str(service.mem_limit)]
    if service.cpus:
        args += ["--cpus", service.cpus]
    if service.shm_size:
        args += ["--shm-size", service.shm_size]
    for ulimit in service.ulimits:
        args += ["--ulimit", ulimit]
    if service.stop_signal:
        args += ["--stop-signal", service.stop_signal]
    if service.gpus:
        args += ["--gpus", service.gpus]
    if service.stdin_open:
        args.append("-i")
    if service.tty:
        args.append("-t")

    entrypoint = service.entrypoint or []
    if entrypoint:
        args += ["--entrypoint", entrypoint[0]]

    args.append(image_name(project, service))
    args += entrypoint[1:]
    args += service.command or []
    return args


def build_args(
    project: Project,
    service: Service,
    no_cache: bool = False,
    path_mapper: Optional[Callable[[str], str]] = None,
) -> List[str]:
    """Arguments for `wslc build` for a service with a build section."""
    assert service.build is not None
    mapper = path_mapper or (lambda p: p)
    build = service.build
    args: List[str] = ["build", "-t", image_name(project, service)]
    if build.dockerfile:
        # wslc resolves -f against its own cwd, not the build context: pass the
        # dockerfile as an absolute path run through the same mapper as the context
        dockerfile = build.dockerfile
        if not os.path.isabs(dockerfile):
            dockerfile = os.path.join(build.context, dockerfile)
        args += ["-f", mapper(dockerfile)]
    for key, value in build.args.items():
        args += ["--build-arg", f"{key}={value}"]
    if build.target:
        args += ["--target", build.target]
    if build.pull:
        args.append("--pull")
    if no_cache:
        args.append("--no-cache")
    args.append(mapper(build.context))
    return args


def image_name(project: Project, service: Service) -> str:
    if service.image:
        return service.image
    return f"{project.name}-{service.name}"
