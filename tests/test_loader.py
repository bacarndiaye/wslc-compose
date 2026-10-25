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


def test_static_ipv4_resolves_network_name_and_preserves_aliases(tmp_path):
    path = tmp_path / "compose.yaml"
    path.write_text(COMPOSE.replace("aliases: [www]", "aliases: [www]\n        ipv4_address: ' 10.0.0.51 '"))
    service = load_project(str(path)).services["web"]
    assert service.network_ipv4_addresses == {"demo_front": "10.0.0.51"}
    assert service.network_aliases == {"demo_front": ["www"]}


@pytest.mark.parametrize("address", ["not-an-ip", "::1", "10.0.0.2/24", "999.0.0.1", "", "null"])
def test_invalid_static_ipv4_is_rejected(tmp_path, address):
    path = tmp_path / "compose.yaml"
    path.write_text(COMPOSE.replace("aliases: [www]", f"aliases: [www]\n        ipv4_address: '{address}'"))
    with pytest.raises(ComposeError, match="invalid ipv4_address"):
        load_project(str(path))


def test_static_ipv4_on_unattached_secondary_network_is_rejected(tmp_path):
    path = tmp_path / "compose.yaml"
    path.write_text("""
services:
  web:
    image: fixture
    networks:
      front: {}
      back:
        ipv4_address: 10.0.0.51
networks:
  front: {}
  back: {}
""")
    with pytest.raises(ComposeError, match="only on the first network"):
        load_project(str(path))


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


@pytest.mark.parametrize(
    ("healthcheck", "test", "disabled"),
    [
        ("test: curl -f http://localhost", ["CMD-SHELL", "curl -f http://localhost"], False),
        ("test: [CMD, curl, -f, http://localhost]", ["CMD", "curl", "-f", "http://localhost"], False),
        ("test: [NONE]", ["NONE"], True),
        ("disable: true", [], True),
    ],
)
def test_healthcheck_forms_are_normalized(tmp_path, healthcheck, test, disabled):
    (tmp_path / "compose.yaml").write_text(f"""
services:
  a:
    image: x
    healthcheck:
      {healthcheck}
""")

    health = load_project(str(tmp_path / "compose.yaml")).services["a"].healthcheck

    assert health is not None
    assert health.test == test
    assert health.disabled is disabled


def test_network_mode_does_not_create_default_network(tmp_path):
    (tmp_path / "compose.yaml").write_text("""
services:
  a:
    image: x
    network_mode: none
""")

    project = load_project(str(tmp_path / "compose.yaml"))

    assert project.services["a"].networks == []
    assert project.networks == {}


def test_network_mapping_preserves_internal_flag(tmp_path):
    (tmp_path / "compose.yaml").write_text("""
services:
  a:
    image: x
    networks: [private]
networks:
  private:
    internal: true
""")

    project = load_project(str(tmp_path / "compose.yaml"))

    assert project.networks["private"].internal is True


def test_volume_mapping_preserves_driver_options_and_labels(tmp_path):
    (tmp_path / "compose.yaml").write_text("""
services:
  a:
    image: x
    volumes: [data:/data]
volumes:
  data:
    driver: guest
    driver_opts:
      size: 1G
    labels:
      purpose: cache
""")

    volume = load_project(str(tmp_path / "compose.yaml")).volumes["data"]

    assert volume.driver == "guest"
    assert volume.driver_opts == {"size": "1G"}
    assert volume.labels == {"purpose": "cache"}


def test_file_backed_secrets_and_configs_are_normalized(tmp_path):
    (tmp_path / "secret.txt").write_text("secret")
    (tmp_path / "config.txt").write_text("config")
    (tmp_path / "compose.yaml").write_text("""
services:
  a:
    image: x
    secrets: [token]
    configs:
      - source: app-config
        target: app.conf
secrets:
  token:
    file: ./secret.txt
configs:
  app-config:
    file: ./config.txt
""")

    service = load_project(str(tmp_path / "compose.yaml")).services["a"]

    assert service.secrets[0].target == "/run/secrets/token"
    assert service.configs[0].target == "/app.conf"




def test_long_form_mount_preserves_type_options_and_readonly(tmp_path):
    (tmp_path / "compose.yaml").write_text("""
services:
  a:
    image: x
    volumes:
      - type: bind
        source: ./data
        target: /data
        read_only: true
        bind:
          propagation: rshared
      - type: tmpfs
        target: /cache
        tmpfs:
          size: 65536
""")

    mounts = load_project(str(tmp_path / "compose.yaml")).services["a"].volumes

    assert mounts[0].structured and mounts[0].options == ["propagation=rshared"]
    assert mounts[0].read_only and mounts[1].options == ["size=65536"]
