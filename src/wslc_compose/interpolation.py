"""Compose-file variable interpolation: ${VAR}, ${VAR:-def}, ${VAR:?err}, $$ ..."""

from __future__ import annotations

import re
from typing import Mapping


class InterpolationError(ValueError):
    pass


_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BRACED_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?+])(.*))?\Z", re.DOTALL)


def _closing_brace(value: str, start: int) -> int:
    """Index of the `}` closing the `${` that opens just before `start`, or -1.

    Nested `${...}` in a default (`${A:-${B}}`) are skipped over.
    """
    depth = 1
    i = start
    while i < len(value):
        if value.startswith("$$", i):
            i += 2
            continue
        if value.startswith("${", i):
            depth += 1
            i += 2
            continue
        if value[i] == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _resolve(name: str, sep, operand, env: Mapping[str, str]) -> str:
    raw = env.get(name)
    if sep is None:
        return raw or ""
    # ":" variants treat empty string like unset
    missing = raw is None if not sep.startswith(":") else not raw
    op = sep[-1]
    if op == "-":
        return raw if not missing else interpolate(operand, env)
    if op == "+":
        return interpolate(operand, env) if not missing else ""
    if missing:  # "?"
        msg = interpolate(operand, env) or f"required variable {name} is missing"
        raise InterpolationError(f"${{{name}}}: {msg}")
    return raw or ""


def interpolate(value: str, env: Mapping[str, str], dotenv: bool = False) -> str:
    """Expand `$VAR`, `${VAR}` and the `${VAR:-default}` family.

    With `dotenv=True` (values read from a .env file) a bare `$VAR` that is not defined
    and `$$` are kept literally, so existing values such as passwords containing `$`
    keep their meaning; `${...}` references are always expanded.
    """
    out = []
    i = 0
    while i < len(value):
        char = value[i]
        if char != "$" or i + 1 == len(value):
            out.append(char)
            i += 1
            continue
        nxt = value[i + 1]
        if nxt == "$":
            out.append("$$" if dotenv else "$")
            i += 2
            continue
        if nxt == "{":
            end = _closing_brace(value, i + 2)
            match = _BRACED_RE.match(value, i + 2, end) if end != -1 else None
            if match:
                out.append(_resolve(match.group(1), match.group(2), match.group(3), env))
                i = end + 1
                continue
        else:
            match = _NAME_RE.match(value, i + 1)
            if match:
                name = match.group(0)
                if dotenv and name not in env:
                    out.append("$" + name)
                else:
                    out.append(env.get(name) or "")
                i = match.end()
                continue
        out.append(char)  # not a reference (e.g. "$(cmd)", "$1"): kept literally
        i += 1
    return "".join(out)


def interpolate_tree(node, env: Mapping[str, str]):
    """Recursively interpolate every string in a parsed YAML tree."""
    if isinstance(node, str):
        return interpolate(node, env)
    if isinstance(node, list):
        return [interpolate_tree(v, env) for v in node]
    if isinstance(node, dict):
        return {k: interpolate_tree(v, env) for k, v in node.items()}
    return node
