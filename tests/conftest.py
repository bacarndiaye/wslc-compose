import pytest

from wslc_compose import engine


@pytest.fixture(autouse=True)
def no_wslc_probe(monkeypatch):
    """Unit tests never query a real wslc; tests that need options override this."""
    engine.run_options.cache_clear()
    monkeypatch.setattr(engine, "run_options", lambda: frozenset())
