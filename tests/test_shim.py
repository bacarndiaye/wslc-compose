from wslc_compose.shim import split_argv


def test_compose_dispatch():
    compose, passthrough = split_argv(["compose", "up", "-d"])
    assert compose == ["up", "-d"]
    assert passthrough == []


def test_bare_compose():
    compose, passthrough = split_argv(["compose"])
    assert compose == []


def test_passthrough():
    compose, passthrough = split_argv(["list", "-a"])
    assert compose is None
    assert passthrough == ["list", "-a"]


def test_empty():
    compose, passthrough = split_argv([])
    assert compose is None
    assert passthrough == []
