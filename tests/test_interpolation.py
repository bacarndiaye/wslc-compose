import pytest

from wslc_compose.interpolation import InterpolationError, interpolate, interpolate_tree

ENV = {"NAME": "world", "EMPTY": ""}


def test_simple():
    assert interpolate("hello $NAME", ENV) == "hello world"
    assert interpolate("hello ${NAME}", ENV) == "hello world"


def test_missing_defaults_to_empty():
    assert interpolate("x${MISSING}y", ENV) == "xy"


def test_default_operator():
    assert interpolate("${MISSING:-def}", ENV) == "def"
    assert interpolate("${EMPTY:-def}", ENV) == "def"
    assert interpolate("${EMPTY-def}", ENV) == ""
    assert interpolate("${NAME:-def}", ENV) == "world"


def test_alt_operator():
    assert interpolate("${NAME:+set}", ENV) == "set"
    assert interpolate("${EMPTY:+set}", ENV) == ""
    assert interpolate("${EMPTY+set}", ENV) == "set"


def test_required_operator():
    assert interpolate("${NAME:?boom}", ENV) == "world"
    with pytest.raises(InterpolationError, match="boom"):
        interpolate("${MISSING:?boom}", ENV)
    with pytest.raises(InterpolationError):
        interpolate("${EMPTY:?}", ENV)


def test_escape():
    assert interpolate("cost: $$5 and $$(cmd)", ENV) == "cost: $5 and $(cmd)"


def test_shell_command_not_interpolated():
    assert interpolate("$(date)", ENV) == "$(date)"


def test_tree():
    tree = {"a": ["$NAME", 1, None], "b": {"c": "${MISSING:-x}"}}
    assert interpolate_tree(tree, ENV) == {"a": ["world", 1, None], "b": {"c": "x"}}
