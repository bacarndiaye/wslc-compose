"""Wait for `depends_on` conditions (service_healthy, service_completed_successfully)."""

from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional

from wslc_compose import LABEL_PROJECT, LABEL_SERVICE, engine
from wslc_compose.engine import WslcError
from wslc_compose.model import Project, Service

POLL_INTERVAL = 1.0


def _labels(data: dict) -> dict:
    return (data.get("Config") or {}).get("Labels") or data.get("Labels") or {}


def _dependency_state(project: Project, dep: str) -> List[dict]:
    """Inspect data of every container (any state) that backs service `dep`."""
    states = []
    for entry in engine.list_project_containers(project.name, all_states=True):
        identifier = entry.get("ID") or entry.get("Id") or entry.get("Names") or entry.get("Name")
        data = engine.inspect(identifier) if identifier else None
        if data and _labels(data).get(LABEL_PROJECT) == project.name \
                and _labels(data).get(LABEL_SERVICE) == dep:
            states.append(data)
    return states


def _name(data: dict) -> str:
    return str(data.get("Name") or data.get("Id") or "?").lstrip("/")


def _evaluate(condition: str, dep: str, data: dict) -> Optional[str]:
    """Return None when satisfied, "wait" while pending; raise when it can never be met."""
    state = data.get("State") or {}
    running = bool(state.get("Running"))
    name = _name(data)
    if condition == "service_healthy":
        if not running:
            raise WslcError(f"dependency {dep!r} failed to start: container {name} is not running "
                            f"(exit code {state.get('ExitCode')})")
        health = state.get("Health")
        if not health:
            raise WslcError(f"dependency {dep!r} has no healthcheck (container {name}); define "
                            "one in the compose file or the image to use condition: service_healthy")
        status = health.get("Status")
        if status == "healthy":
            return None
        if status == "unhealthy":
            raise WslcError(f"dependency {dep!r} is unhealthy (container {name})")
        return "wait"
    # service_completed_successfully
    if running or state.get("Status") in ("created", "starting"):
        return "wait"
    if state.get("ExitCode") == 0:
        return None
    raise WslcError(f"dependency {dep!r} did not complete successfully: container {name} "
                    f"exited with code {state.get('ExitCode')}")


def wait_for_dependencies(
    project: Project,
    service: Service,
    selected: List[Service],
    timeout: float = 0,
    log: Callable[[str], None] = lambda _: None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    replicas: Optional[Dict[str, int]] = None,
) -> None:
    """Block until every non-default dependency condition of `service` is met.

    Dependencies outside `selected` (other profiles, not requested) and scaled to
    zero replicas are skipped. `timeout` seconds (0 = unlimited) bounds each wait.
    """
    names = {svc.name for svc in selected}
    for dep, condition in service.depends_on_conditions.items():
        if dep not in names or (replicas or {}).get(dep, 1) == 0:
            continue
        log(f"Waiting for {dep} to be healthy ..." if condition == "service_healthy"
            else f"Waiting for {dep} to complete ...")
        try:
            _wait_one(project, dep, condition, timeout, sleep, clock)
        except WslcError as exc:
            if dep not in service.depends_on_optional:
                raise
            log(f"warning: optional dependency {dep!r} (required: false): {exc}; continuing")


def _wait_one(project: Project, dep: str, condition: str, timeout: float,
              sleep: Callable[[float], None], clock: Callable[[], float]) -> None:
    deadline = clock() + timeout if timeout else None
    while True:
        containers = _dependency_state(project, dep)
        if not containers:
            raise WslcError(f"dependency {dep!r} has no container to wait for")
        if all(_evaluate(condition, dep, data) is None for data in containers):
            return
        if deadline is not None and clock() >= deadline:
            raise WslcError(f"timed out after {timeout:g}s waiting for {dep!r} ({condition})")
        sleep(POLL_INTERVAL)
