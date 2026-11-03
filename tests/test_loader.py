import pytest

from wslc_compose.loader import ComposeError, load_project, parse_port

COMPOSE = """
name: demo
services:
  web:
    image: nginx:alpine
    ports:
      - "8088:80"
      - "127.0.0.1:9000:9000/udp"
      - target: 443
        published: 8443
    volumes:
      - ./html:/usr/share/nginx/html:ro
      - data:/data
    environment:
      - FOO=bar
      - NOVAL
    depends_on:
      - app
    networks:
      front:
        aliases: [www]
  app:
    image: alpine
    command: sh -c 'sleep 999'
    deploy:
      replicas: 2
      resources:
        limits:
          cpus: "0.5"
          memory: 256M
    networks: [front]
networks:
  front:
volumes:
  data:
"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "compose.yaml").write_text(COMPOSE)
    (tmp_path / "html").mkdir()
    return load_project(str(tmp_path / "compose.yaml"))


def test_project_name_from_compose(project):
    assert project.name == "demo"


def test_networks_and_volumes_prefixed(project):
    assert project.networks["front"].name == "demo_front"
    assert project.volumes["data"].name == "demo_data"


def test_ports(project):
    web = project.services["web"]
    assert [p.to_flag() for p in web.ports] == ["8088:80", "127.0.0.1:9000:9000/udp", "8443:443"]


def test_volumes(project, tmp_path):
    web = project.services["web"]
    bind, named = web.volumes
    assert bind.type == "bind"
    assert bind.source == str(tmp_path / "html")
    assert bind.read_only
    assert named.type == "volume"
    assert named.source == "demo_data"  # resolved to project-scoped name


def test_environment_list_form(project):
    assert project.services["web"].environment == {"FOO": "bar", "NOVAL": None}


def test_command_string_is_split(project):
    assert project.services["app"].command == ["sh", "-c", "sleep 999"]


def test_deploy_limits(project):
    app = project.services["app"]
    assert app.replicas == 2
    assert app.cpus == "0.5"
    assert app.mem_limit == "256M"


def test_network_aliases(project):
    web = project.services["web"]
    assert web.networks == ["demo_front"]
    assert web.network_aliases["demo_front"] == ["www"]


def test_dependency_order(project):
    order = [s.name for s in project.sorted_services()]
    assert order.index("app") < order.index("web")


def test_container_names(project):
    web = project.services["web"]
    assert project.container_name(web, 1) == "demo-web-1"


def test_circular_dependency(tmp_path):
    (tmp_path / "compose.yaml").write_text(
        """
services:
  a:
    image: x
    depends_on: [b]
  b:
    image: x
    depends_on: [a]
"""
    )
    project = load_project(str(tmp_path / "compose.yaml"))
    with pytest.raises(ValueError, match="circular"):
        project.sorted_services()


def test_undefined_network_rejected(tmp_path):
    (tmp_path / "compose.yaml").write_text(
        """
services:
  a:
    image: x
    networks: [nope]
"""
    )
    with pytest.raises(ComposeError, match="undefined network"):
        load_project(str(tmp_path / "compose.yaml"))


def test_windows_path_volume(tmp_path):
    (tmp_path / "compose.yaml").write_text(
        r"""
services:
  a:
    image: x
    volumes:
      - E:\data\www:/srv:ro
"""
    )
    project = load_project(str(tmp_path / "compose.yaml"))
    mount = project.services["a"].volumes[0]
    assert mount.type == "bind"
    assert mount.source == r"E:\data\www"
    assert mount.target == "/srv"
    assert mount.read_only


def test_port_range():
    flags = [p.to_flag() for p in parse_port("8000-8002:9000-9002")]
    assert flags == ["8000:9000", "8001:9001", "8002:9002"]


def test_unsupported_key_warns(tmp_path):
    (tmp_path / "compose.yaml").write_text(
        """
services:
  a:
    image: x
    privileged: true
    restart: always
"""
    )
    project = load_project(str(tmp_path / "compose.yaml"))
    text = "\n".join(project.warnings)
    assert "privileged" in text
    assert "restart" in text


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_include_merges_services_and_rebases_relative_paths(tmp_path):
    _write(
        tmp_path / "compose.yaml",
        "include:\n  - svc/web.yaml\nservices:\n  db:\n    image: postgres\n",
    )
    _write(
        tmp_path / "svc" / "web.yaml",
        "services:\n  web:\n    image: nginx\n    volumes:\n      - ./html:/usr/share/nginx/html:ro\n"
        "    env_file: ./web.env\n",
    )
    project = load_project(str(tmp_path / "compose.yaml"))
    assert set(project.services) == {"db", "web"}
    web = project.services["web"]
    assert web.volumes[0].source == str(tmp_path / "svc" / "html")
    assert web.env_files == [str(tmp_path / "svc" / "web.env")]


def test_include_accepts_mapping_and_nested_includes(tmp_path):
    _write(tmp_path / "compose.yaml", "include:\n  - path: a.yaml\nservices:\n  main:\n    image: x\n")
    _write(tmp_path / "a.yaml", "include:\n  - b.yaml\nservices:\n  a:\n    image: x\n")
    _write(tmp_path / "b.yaml", "services:\n  b:\n    image: x\n")
    project = load_project(str(tmp_path / "compose.yaml"))
    assert set(project.services) == {"main", "a", "b"}


def test_include_only_file_without_own_services(tmp_path):
    _write(tmp_path / "compose.yaml", "include:\n  - a.yaml\n")
    _write(tmp_path / "a.yaml", "services:\n  a:\n    image: x\n")
    assert set(load_project(str(tmp_path / "compose.yaml")).services) == {"a"}


def test_include_conflict_missing_and_cycle(tmp_path):
    _write(tmp_path / "compose.yaml", "include:\n  - a.yaml\nservices:\n  a:\n    image: y\n")
    _write(tmp_path / "a.yaml", "services:\n  a:\n    image: x\n")
    with pytest.raises(ComposeError, match="conflicts"):
        load_project(str(tmp_path / "compose.yaml"))

    _write(tmp_path / "missing.yaml", "include:\n  - nope.yaml\nservices:\n  m:\n    image: x\n")
    with pytest.raises(ComposeError, match="not found"):
        load_project(str(tmp_path / "missing.yaml"))

    _write(tmp_path / "c1.yaml", "include:\n  - c2.yaml\nservices:\n  c1:\n    image: x\n")
    _write(tmp_path / "c2.yaml", "include:\n  - c1.yaml\nservices:\n  c2:\n    image: x\n")
    with pytest.raises(ComposeError, match="circular"):
        load_project(str(tmp_path / "c1.yaml"))
