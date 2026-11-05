import pytest

from wslc_compose import dependencies
from wslc_compose.engine import WslcError
from wslc_compose.model import Project, Service


def _project(condition="service_healthy"):
    db = Service(name="db", image="x")
    web = Service(name="web", image="x", depends_on=["db"], depends_on_conditions={"db": condition})
    return Project(name="demo", directory=".", services={"db": db, "web": web}), db, web


def _container(health=None, running=True, status="running", exit_code=0, service="db"):
    state = {"Running": running, "Status": status, "ExitCode": exit_code,
             "Health": None if health is None else {"Status": health}}
    return {"Name": "/demo-db-1", "State": state,
            "Labels": {"com.wslc-compose.project": "demo", "com.wslc-compose.service": service}}


@pytest.fixture
def fake_engine(monkeypatch):
    """Feed successive inspect snapshots; the last one repeats."""
    def install(*snapshots):
        calls = {"n": 0}

        def listing(project, all_states=False):
            assert all_states
            return [{"ID": "db1"}]

        def inspect(identifier):
            data = snapshots[min(calls["n"], len(snapshots) - 1)]
            calls["n"] += 1
            return data

        monkeypatch.setattr(dependencies.engine, "list_project_containers", listing)
        monkeypatch.setattr(dependencies.engine, "inspect", inspect)
        return calls
    return install


def _wait(project, web, selected, **kwargs):
    sleeps = []
    dependencies.wait_for_dependencies(project, web, selected, sleep=sleeps.append, **kwargs)
    return sleeps


def test_waits_through_starting_until_healthy(fake_engine):
    project, db, web = _project()
    fake_engine(_container("starting"), _container("starting"), _container("healthy"))
    assert len(_wait(project, web, [db, web])) == 2


def test_already_healthy_does_not_sleep(fake_engine):
    project, db, web = _project()
    fake_engine(_container("healthy"))
    assert _wait(project, web, [db, web]) == []


def test_unhealthy_dependency_fails(fake_engine):
    project, db, web = _project()
    fake_engine(_container("starting"), _container("unhealthy"))
    with pytest.raises(WslcError, match="'db' is unhealthy"):
        _wait(project, web, [db, web])


def test_dependency_without_healthcheck_fails(fake_engine):
    project, db, web = _project()
    fake_engine(_container(health=None))
    with pytest.raises(WslcError, match="has no healthcheck"):
        _wait(project, web, [db, web])


def test_stopped_dependency_fails_instead_of_hanging(fake_engine):
    project, db, web = _project()
    fake_engine(_container("starting", running=False, status="exited", exit_code=3))
    with pytest.raises(WslcError, match="not running .*exit code 3"):
        _wait(project, web, [db, web])


def test_timeout_is_enforced(fake_engine):
    project, db, web = _project()
    fake_engine(_container("starting"))
    now = iter(range(0, 100, 5))
    with pytest.raises(WslcError, match="timed out after 12s"):
        _wait(project, web, [db, web], timeout=12, clock=lambda: next(now))


def test_missing_dependency_container_fails(monkeypatch):
    project, db, web = _project()
    monkeypatch.setattr(dependencies.engine, "list_project_containers", lambda *a, **k: [])
    with pytest.raises(WslcError, match="no container to wait for"):
        _wait(project, web, [db, web])


def test_unselected_or_scaled_to_zero_dependencies_are_skipped(monkeypatch):
    project, db, web = _project()
    monkeypatch.setattr(dependencies.engine, "list_project_containers",
                        lambda *a, **k: pytest.fail("must not query the engine"))
    assert _wait(project, web, [web]) == []
    assert _wait(project, web, [db, web], replicas={"db": 0}) == []


def test_other_projects_and_services_are_ignored(fake_engine):
    project, db, web = _project()
    foreign = _container("healthy")
    foreign["Labels"]["com.wslc-compose.project"] = "other"
    fake_engine(foreign)
    with pytest.raises(WslcError, match="no container to wait for"):
        _wait(project, web, [db, web])


def test_completed_successfully(fake_engine):
    project, db, web = _project("service_completed_successfully")
    fake_engine(_container(running=True), _container(running=False, status="exited", exit_code=0))
    assert len(_wait(project, web, [db, web])) == 1


def test_completed_with_failure(fake_engine):
    project, db, web = _project("service_completed_successfully")
    fake_engine(_container(running=False, status="exited", exit_code=2))
    with pytest.raises(WslcError, match="did not complete successfully.*code 2"):
        _wait(project, web, [db, web])


def test_optional_dependency_failure_only_warns(fake_engine):
    project, db, web = _project()
    web.depends_on_optional = ["db"]
    fake_engine(_container("unhealthy"))
    logs = []
    _wait(project, web, [db, web], log=logs.append)
    assert any("optional dependency 'db'" in line and "unhealthy" in line for line in logs)


def test_required_dependency_failure_still_raises(fake_engine):
    project, db, web = _project()
    fake_engine(_container("unhealthy"))
    with pytest.raises(WslcError, match="unhealthy"):
        _wait(project, web, [db, web])
