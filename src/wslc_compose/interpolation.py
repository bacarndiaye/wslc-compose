"""Compose-file variable interpolation: ${VAR}, ${VAR:-def}, ${VAR:?err}, $$ ..."""

from __future__ import annotations

import re
from typing import Mapping


class InterpolationError(ValueError):
    pass


_VAR_RE = re.compile(
    r"""
    \$(?:
        (?P<escaped>\$)
      | (?P<named>[A-Za-z_][A-Za-z0-9_]*)
      | \{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)
            (?:(?P<sep>:?[-?+])(?P<operand>[^}]*))?
        \}
    )
    """,
    re.VERBOSE,
)


def interpolate(value: str, env: Mapping[str, str]) -> str:
    def repl(m: re.Match) -> str:
        if m.group("escaped"):
            return "$"
        name = m.group("named") or m.group("braced")
        sep = m.group("sep")
        operand = m.group("operand")
        raw = env.get(name)
        if sep is None:
            return raw or ""
        # ":" variants treat empty string like unset
        missing = raw is None if not sep.startswith(":") else not raw
        op = sep[-1]
        if op == "-":
            return raw if not missing else operand
        if op == "+":
            return operand if not missing else ""
        if op == "?":
            if missing:
                msg = operand or f"required variable {name} is missing"
                raise InterpolationError(f"${{{name}}}: {msg}")
            return raw or ""
        return raw or ""

    return _VAR_RE.sub(repl, value)


def interpolate_tree(node, env: Mapping[str, str]):
    """Recursively interpolate every string in a parsed YAML tree."""
    if isinstance(node, str):
        return interpolate(node, env)
    if isinstance(node, list):
        return [interpolate_tree(v, env) for v in node]
    if isinstance(node, dict):
        return {k: interpolate_tree(v, env) for k, v in node.items()}
    return node
