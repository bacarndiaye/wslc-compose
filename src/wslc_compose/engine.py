"""Thin subprocess layer over the wslc CLI."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from typing import Dict, List, Optional

from wslc_compose import LABEL_PROJECT

_WIN_PATH_RE = re.compile(r"^([A-Za-z]:[\\/]|\\\\)")

WSLC_FALLBACKS = (
    "/mnt/c/Program Files/WSL/wslc.exe",
    "C:\\Program Files\\WSL\\wslc.exe",
)


class WslcError(RuntimeError):
    pass


def _is_self(path: str) -> bool:
    """True when `path` is our own `wslc` wrapper script, not the real CLI."""
    try:
        return os.path.realpath(path) == os.path.realpath(sys.argv[0])
    except OSError:
        return False


@lru_cache(maxsize=1)
def find_wslc() -> str:
    override = os.environ.get("WSLC_COMPOSE_BIN")
    if override:
        return override
    for name in ("wslc.exe", "wslc"):
        path = shutil.which(name)
        if path and not _is_self(path):
            return path
    for path in WSLC_FALLBACKS:
        if os.path.isfile(path):
            return path
    raise WslcError(
        "wslc CLI not found. Install the WSL container preview "
        "(https://learn.microsoft.com/windows/wsl/wsl-container) or set WSLC_COMPOSE_BIN."
    )


def _running_in_wsl() -> bool:
    return sys.platform.startswith("linux") and (
        "WSL_DISTRO_NAME" in os.environ or "microsoft" in os.uname().release.lower()
    )


@lru_cache(maxsize=1)
def needs_path_translation() -> bool:
    """True when we call the Windows wslc.exe from inside a WSL distro."""
    return _running_in_wsl() and find_wslc().lower().endswith(".exe")


@lru_cache(maxsize=256)
def to_host_path(path: str) -> str:
    """Translate a Linux path to the Windows path wslc.exe expects."""
    if not needs_path_translation() or _WIN_PATH_RE.match(path):
        return path
    try:
        out = subprocess.run(
            ["wslpath", "-w", path], capture_output=True, text=True, check=True
        ).stdout.strip()
        if out:
            return out
    except (OSError, subprocess.CalledProcessError):
        pass
    # manual fallback for /mnt/<drive>/... paths
    m = re.match(r"^/mnt/([a-zA-Z])(/.*)?$", path)
    if m:
        rest = (m.group(2) or "/").replace("/", "\\")
        return f"{m.group(1).upper()}:{rest}"
    print(
        f"wslc-compose: warning: cannot translate path {path!r} for wslc.exe; passing as-is",
        file=sys.stderr,
    )
    return path


def run(
    args: List[str],
    capture: bool = False,
    check: bool = True,
    dry_run: bool = False,
) -> subprocess.CompletedProcess:
    argv = [find_wslc()] + args
    if dry_run:
        print("+ " + " ".join(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")
    proc = subprocess.run(argv, capture_output=capture, text=capture)
    if check and proc.returncode != 0:
        detail = (proc.stderr or "").strip() if capture else ""
        raise WslcError(
            f"wslc {' '.join(args[:2])} failed (exit {proc.returncode})"
            + (f": {detail}" if detail else "")
        )
    return proc


def popen(args: List[str], **kwargs) -> subprocess.Popen:
    return subprocess.Popen([find_wslc()] + args, **kwargs)


def capture_json(args: List[str]):
    proc = run(args, capture=True)
    text = proc.stdout.strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise WslcError(f"unexpected non-JSON output from wslc {' '.join(args)}: {exc}")


# --- queries ---------------------------------------------------------------


def list_project_containers(project: str, all_states: bool = True) -> List[dict]:
    args = ["list", "--format", "json", "-f", f"label={LABEL_PROJECT}={project}"]
    if all_states:
        args.insert(1, "-a")
    result = capture_json(args)
    return result if isinstance(result, list) else []


def inspect(object_id: str) -> Optional[dict]:
    try:
        result = capture_json(["inspect", object_id])
    except WslcError:
        return None
    if isinstance(result, list):
        return result[0] if result else None
    return result


def inspect_many(ids: List[str]) -> Dict[str, dict]:
    return {i: data for i in ids if (data := inspect(i)) is not None}


def _names_from_table(args: List[str], column: str = "NAME") -> List[str]:
    """Parse names out of a wslc table output (network/volume list)."""
    proc = run(args, capture=True)
    lines = proc.stdout.splitlines()
    if not lines:
        return []
    header = lines[0]
    idx = header.find(column)
    if idx < 0:
        return []
    names = []
    for line in lines[1:]:
        if len(line) > idx:
            names.append(line[idx:].split()[0])
    return names


def image_exists(name: str) -> bool:
    repo, _, tag = name.partition(":")
    try:
        images = capture_json(["images", "--format", "json"])
    except WslcError:
        return False
    for img in images if isinstance(images, list) else []:
        if img.get("Repository") == repo and (not tag or img.get("Tag") == tag):
            return True
    return False


def network_names() -> List[str]:
    return _names_from_table(["network", "list"])


def volume_names() -> List[str]:
    return _names_from_table(["volume", "list"], column="VOLUME NAME")


def ensure_network(name: str, dry_run: bool = False) -> bool:
    if name in network_names():
        return False
    run(["network", "create", name], dry_run=dry_run)
    return True


def ensure_volume(name: str, dry_run: bool = False) -> bool:
    if name in volume_names():
        return False
    run(["volume", "create", name], dry_run=dry_run)
    return True
