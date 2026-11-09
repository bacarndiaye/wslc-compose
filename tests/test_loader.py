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


def test_static_ipv4_is_retained_on_secondary_network(tmp_path):
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
    service = load_project(str(path)).services["web"]
    assert service.network_ipv4_addresses == {service.networks[1]: "10.0.0.51"}


def test_network_ipam_and_creation_metadata_are_normalized(tmp_path):
    path = tmp_path / "compose.yaml"
    path.write_text("""
name: demo
services:
  web:
    image: fixture
    networks:
      front: {ipv4_address: 10.1.0.10, aliases: [www]}
      back: {ipv4_address: 10.2.0.20, aliases: [private]}
networks:
  front:
    name: custom-front
    driver: bridge
    driver_opts: {mtu: '1400'}
    labels: {purpose: web}
    ipam:
      driver: default
      config:
        - subnet: 10.1.0.0/24
          gateway: 10.1.0.1
          ip_range: 10.1.0.128/25
  back:
    internal: true
    ipam:
      config: [{subnet: 10.2.0.0/24}]
""")
    project = load_project(str(path))
    front = project.networks["front"]
    assert (front.name, front.driver, front.subnet, front.gateway, front.ip_range) == (
        "custom-front", "bridge", "10.1.0.0/24", "10.1.0.1", "10.1.0.128/25")
    assert front.driver_opts == {"mtu": "1400"} and front.labels == {"purpose": "web"}
    assert project.networks["back"].internal
    assert project.services["web"].network_ipv4_addresses == {
        "custom-front": "10.1.0.10", "demo_back": "10.2.0.20"}
    assert project.services["web"].network_aliases == {"custom-front": ["www"], "demo_back": ["private"]}


@pytest.mark.parametrize("config", [
    "{ipam: {config: [{subnet: 10.1.0.0/24}, {subnet: 10.2.0.0/24}]}}",
    "{ipam: {config: [{subnet: 'fd00::/64'}]}}",
    "{enable_ipv6: true}", "{enable_ipv4: false}", "{attachable: true}",
    "{ipam: {driver: custom}}", "{ipam: {options: {foo: bar}}}",
    "{ipam: {config: [{subnet: 10.1.0.0/24, aux_addresses: {reserved: 10.1.0.5}}]}}",
    "{ipam: {config: [{subnet: 10.1.0.5/24}]}}",
    "{ipam: {config: [{subnet: 10.1.0.0/24, gateway: 10.2.0.1}]}}",
    "{ipam: {config: [{subnet: 10.1.0.0/24, gateway: 10.1.0.255}]}}",
    "{ipam: {config: [{subnet: 10.1.0.0/24, ip_range: 10.2.0.0/25}]}}",
    "{ipam: {config: [{gateway: 10.1.0.1}]}}",
    "{ipam: []}", "{ipam: {config: {}}}", "{ipam: {config: [null]}}",
    "{unknown: true}",
])
def test_ipam_that_cannot_be_applied_is_rejected_instead_of_dropped(tmp_path, config):
    path = tmp_path / "compose.yaml"
    path.write_text(f"services:\n  web:\n    image: fixture\n    networks: [front]\nnetworks:\n  front: {config}\n")
    with pytest.raises(ComposeError, match="networks.front"):
        load_project(str(path))


@pytest.mark.parametrize("address", ["10.2.0.10", "10.1.0.0", "10.1.0.1", "10.1.0.255"])
def test_static_address_must_be_usable_in_declared_subnet(tmp_path, address):
    path = tmp_path / "compose.yaml"
    path.write_text(f"""
services:
  web:
    image: fixture
    networks:
      front: {{ipv4_address: {address}}}
networks:
  front:
    ipam:
      config: [{{subnet: 10.1.0.0/24, gateway: 10.1.0.1}}]
""")
    with pytest.raises(ComposeError, match="not usable"):
        load_project(str(path))


@pytest.mark.parametrize("setting", ["ipv6_address: 'fd00::2'", "priority: 100", "gw_priority: 1", "interface_name: eth1"])
def test_unsupported_endpoint_options_are_not_silently_discarded(tmp_path, setting):
    path = tmp_path / "compose.yaml"
    path.write_text(f"services:\n  web:\n    image: fixture\n    networks:\n      front: {{{setting}}}\nnetworks:\n  front: {{}}")
    with pytest.raises(ComposeError, match="unsupported endpoint"):
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
    restart: on-failure:3
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


def _secrets_project(tmp_path, body, files=("tok.txt", "cfg.txt")):
    for name in files:
        (tmp_path / name).write_text("top-secret-value")
    (tmp_path / "compose.yaml").write_text(body)
    return str(tmp_path / "compose.yaml")


def test_file_secrets_and_configs_short_and_long_syntax(tmp_path):
    path = _secrets_project(
        tmp_path,
        """
services:
  app:
    image: x
    secrets:
      - tok
      - source: tok
        target: /root/.tok
        mode: 0400
    configs:
      - cfg
secrets:
  tok:
    file: ./tok.txt
configs:
  cfg:
    file: ./cfg.txt
""",
    )
    project = load_project(path)
    svc = project.services["app"]
    assert [m.target for m in svc.secrets] == ["/run/secrets/tok", "/root/.tok"]
    assert [m.target for m in svc.configs] == ["/cfg"]
    assert "mode cannot be enforced" in "\n".join(project.warnings)
    assert "top-secret-value" not in repr(project)


def test_secret_and_config_with_same_key_use_their_own_files(tmp_path):
    from wslc_compose import flags

    path = _secrets_project(
        tmp_path,
        """
services:
  app:
    image: x
    secrets: [shared]
    configs: [shared]
secrets:
  shared:
    file: ./tok.txt
configs:
  shared:
    file: ./cfg.txt
""",
    )
    project = load_project(path)
    args = flags.run_args(project, project.services["app"], 1, lambda p: p)
    assert f"{tmp_path / 'tok.txt'}:/run/secrets/shared:ro" in args
    assert f"{tmp_path / 'cfg.txt'}:/shared:ro" in args


@pytest.mark.parametrize(
    ("definition", "message"),
    [
        ("external: true", "external secrets are not supported"),
        ("environment: FEED_TOKEN", "only file-backed secrets"),
        ("file: ./missing.txt", "file not found"),
        ("{}", "only file-backed secrets"),
    ],
)
def test_unsupported_or_broken_secret_definitions(tmp_path, definition, message):
    path = _secrets_project(
        tmp_path,
        f"services:\n  app:\n    image: x\n    secrets: [tok]\nsecrets:\n  tok:\n    {definition}\n",
    )
    with pytest.raises(ComposeError, match=message):
        load_project(path)


@pytest.mark.parametrize(
    ("service_body", "message"),
    [
        ("secrets: [nope]", "undefined secret 'nope'"),
        ("configs: [nope]", "undefined config 'nope'"),
        ("secrets: [tok, {source: tok, target: tok}]", "multiple secrets target"),
        ("secrets: [{source: tok, target: a/b}]", "invalid target"),
        ("secrets: [tok]\n    volumes: ['data:/run/secrets/tok']", "volume"),
        ("build: {context: ., secrets: [tok]}", "build.secrets is not supported"),
    ],
)
def test_service_secret_reference_errors(tmp_path, service_body, message):
    path = _secrets_project(
        tmp_path,
        f"services:\n  app:\n    image: x\n    {service_body}\n"
        "secrets:\n  tok:\n    file: ./tok.txt\nvolumes:\n  data:\n",
    )
    with pytest.raises(ComposeError, match=message):
        load_project(path)


def test_include_carries_secret_definitions_with_rebased_paths(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "tok.txt").write_text("v")
    (tmp_path / "compose.yaml").write_text("include:\n  - sub/inc.yaml\n")
    (tmp_path / "sub" / "inc.yaml").write_text(
        "services:\n  app:\n    image: x\n    secrets: [tok]\nsecrets:\n  tok:\n    file: ./tok.txt\n"
    )
    project = load_project(str(tmp_path / "compose.yaml"))
    assert project.secrets["tok"].file == str(tmp_path / "sub" / "tok.txt")


def _w(path, text):
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_multiple_files_overlay_like_docker_compose(tmp_path):
    base = _w(
        tmp_path / "a.yaml",
        """
services:
  web:
    image: nginx:1
    ports: ["8080:80"]
    environment: [A=1, B=2]
    volumes: ["./one:/data", "./html:/usr/share/nginx/html"]
    command: ["old"]
    networks: [front]
  db:
    image: postgres
networks:
  front:
""",
    )
    over = _w(
        tmp_path / "b.yaml",
        """
services:
  web:
    image: nginx:2
    ports: ["9090:90"]
    environment: {B: "3", C: "4"}
    volumes: ["./two:/data"]
    command: ["new"]
    networks: [back]
  cache:
    image: redis
networks:
  back:
""",
    )
    project = load_project([base, over])
    assert set(project.services) == {"web", "db", "cache"}
    web = project.services["web"]
    assert web.image == "nginx:2"
    assert [(p.published, p.target) for p in web.ports] == [("8080", 80), ("9090", 90)]
    assert web.environment == {"A": "1", "B": "3", "C": "4"}
    assert web.command == ["new"]
    assert sorted(v.target for v in web.volumes) == ["/data", "/usr/share/nginx/html"]
    assert next(v for v in web.volumes if v.target == "/data").source == str(tmp_path / "two")
    assert web.networks == [f"{project.name}_front", f"{project.name}_back"]
    assert project.compose_files == [base, over]


def test_first_file_without_services_is_allowed(tmp_path):
    # Issue #3: a base file that only declares volumes, services come from a later file.
    base = _w(tmp_path / "vol.yaml", "volumes:\n  data:\n")
    over = _w(tmp_path / "svc.yaml", "services:\n  app:\n    image: x\n    volumes: ['data:/d']\n")
    project = load_project([base, over])
    assert "app" in project.services and "data" in project.volumes


def test_no_services_anywhere_is_still_an_error(tmp_path):
    only = _w(tmp_path / "vol.yaml", "volumes:\n  data:\n")
    with pytest.raises(ComposeError, match="no 'services' section"):
        load_project([only])


def test_healthcheck_is_supported_without_warning(tmp_path):
    (tmp_path / "compose.yaml").write_text(
        "services:\n  web:\n    image: x\n    healthcheck:\n"
        "      test: [CMD-SHELL, 'true']\n      interval: 3s\n      retries: 2\n"
    )
    project = load_project(str(tmp_path / "compose.yaml"))
    assert project.services["web"].healthcheck.retries == 2
    assert not any("healthcheck" in w for w in project.warnings)


def _deps(tmp_path, body):
    (tmp_path / "compose.yaml").write_text(body)
    return load_project(str(tmp_path / "compose.yaml"))


def test_depends_on_conditions_are_parsed_without_warning(tmp_path):
    project = _deps(
        tmp_path,
        "services:\n  a:\n    image: x\n  m:\n    image: x\n  b:\n    image: x\n    depends_on:\n"
        "      a:\n        condition: service_healthy\n"
        "      m:\n        condition: service_completed_successfully\n",
    )
    web = project.services["b"]
    assert web.depends_on == ["a", "m"]
    assert web.depends_on_conditions == {
        "a": "service_healthy", "m": "service_completed_successfully"}
    assert not any("depends_on" in w for w in project.warnings)


def test_depends_on_default_condition_is_not_recorded(tmp_path):
    project = _deps(
        tmp_path,
        "services:\n  a:\n    image: x\n  b:\n    image: x\n    depends_on:\n"
        "      a:\n        condition: service_started\n",
    )
    assert project.services["b"].depends_on_conditions == {}


def test_depends_on_unknown_condition_is_rejected(tmp_path):
    with pytest.raises(ComposeError, match="unsupported condition 'service_ready'"):
        _deps(
            tmp_path,
            "services:\n  a:\n    image: x\n  b:\n    image: x\n    depends_on:\n"
            "      a:\n        condition: service_ready\n",
        )


def test_service_healthy_on_disabled_healthcheck_is_rejected(tmp_path):
    with pytest.raises(ComposeError, match="disables its healthcheck"):
        _deps(
            tmp_path,
            "services:\n  a:\n    image: x\n    healthcheck:\n      disable: true\n"
            "  b:\n    image: x\n    depends_on:\n      a:\n        condition: service_healthy\n",
        )


def test_conditional_dependency_on_undefined_service_is_rejected(tmp_path):
    with pytest.raises(ComposeError, match="undefined service 'ghost'"):
        _deps(
            tmp_path,
            "services:\n  b:\n    image: x\n    depends_on:\n      ghost:\n"
            "        condition: service_healthy\n",
        )


def test_depends_on_required_false_is_recorded(tmp_path):
    project = _deps(
        tmp_path,
        "services:\n  a:\n    image: x\n  c:\n    image: x\n  b:\n    image: x\n    depends_on:\n"
        "      a:\n        condition: service_healthy\n        required: false\n"
        "      c:\n        required: 'true'\n",
    )
    web = project.services["b"]
    assert web.depends_on_optional == ["a"]
    assert web.depends_on_conditions == {"a": "service_healthy"}


def test_depends_on_required_must_be_boolean(tmp_path):
    with pytest.raises(ComposeError, match="required: expected true or false"):
        _deps(
            tmp_path,
            "services:\n  a:\n    image: x\n  b:\n    image: x\n    depends_on:\n"
            "      a:\n        required: maybe\n",
        )


def test_optional_dependency_is_not_pulled_in_by_selection(tmp_path):
    project = _deps(
        tmp_path,
        "services:\n  cache:\n    image: x\n    profiles: [extra]\n  db:\n    image: x\n"
        "  web:\n    image: x\n    depends_on:\n      db: {}\n      cache:\n        required: false\n",
    )
    assert [s.name for s in project.sorted_services(["web"])] == ["db", "web"]
    assert [s.name for s in project.sorted_services()] == ["cache", "db", "web"]


def test_capabilities_are_normalized(tmp_path):
    project = _deps(
        tmp_path,
        "services:\n  a:\n    image: x\n    cap_add: [net_admin, CAP_SYS_TIME, NET_ADMIN]\n    cap_drop: [ALL]\n",
    )
    svc = project.services["a"]
    assert svc.cap_add == ["NET_ADMIN", "SYS_TIME"]
    assert svc.cap_drop == ["ALL"]
    assert not any("cap_" in w for w in project.warnings)


@pytest.mark.parametrize("value, expected, warns", [
    ("always", "always", False),
    ("'no'", "no", False),
    ("no", "no", False),  # YAML 1.1 turns a bare no into False
    ("unless-stopped", "unless-stopped", True),
    ("on-failure:3", "on-failure:3", True),
])
def test_restart_policy_parsing(tmp_path, value, expected, warns):
    project = _deps(tmp_path, f"services:\n  a:\n    image: x\n    restart: {value}\n")
    assert project.services["a"].restart == expected
    assert any("restart policy" in w for w in project.warnings) is warns


# --- audit fixes (#42 #43 #44 #46 #47 #48 #49 #54) ----------------------------


def _one(tmp_path, body, files=None, env=None, monkeypatch=None):
    (tmp_path / "compose.yaml").write_text(body)
    for name, text in (files or {}).items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    if monkeypatch is not None:
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)
    return load_project(str(tmp_path / "compose.yaml"))


def test_interpolated_booleans_are_parsed(tmp_path, monkeypatch):
    project = _one(tmp_path, """
name: t
services:
  a:
    image: alpine
    tty: ${TTY:-false}
    stdin_open: "${STDIN:-true}"
    networks: [n]
    volumes:
      - type: bind
        source: ./d
        target: /d
        read_only: ${RO:-false}
networks:
  n:
    external: ${EXT:-false}
    internal: "no"
""")
    a = project.services["a"]
    assert (a.tty, a.stdin_open, a.volumes[0].read_only) == (False, True, False)
    assert project.networks["n"].external is False
    assert project.networks["n"].name == "t_n"
    assert project.networks["n"].internal is False


def test_invalid_boolean_is_rejected(tmp_path):
    with pytest.raises(ComposeError, match="tty: expected a boolean"):
        _one(tmp_path, "services:\n  a:\n    image: alpine\n    tty: maybe\n")


def test_legacy_external_mapping_still_means_external(tmp_path):
    project = _one(tmp_path, """
services:
  a: {image: alpine, networks: [n]}
networks:
  n:
    external: {name: shared}
    name: shared
""")
    assert project.networks["n"].external is True


def test_yaml_booleans_are_written_like_compose(tmp_path):
    project = _one(tmp_path, """
services:
  a:
    image: alpine
    environment: {DEBUG: true, OFF: false, N: 3, FEATURE: yes, ON: on}
    labels: {x.enabled: true}
    restart: no
    tty: yes
    volumes:
      - type: bind
        source: ./d
        target: /d
        bind: {create_host_path: yes}
""")
    a = project.services["a"]
    # YAML 1.2 like Docker Compose: only true/false are booleans
    assert a.environment == {"DEBUG": "true", "OFF": "false", "N": "3", "FEATURE": "yes", "ON": "on"}
    assert a.labels == {"x.enabled": "true"}
    assert a.restart == "no"
    assert a.tty is True  # boolean settings still accept yes/no
    assert a.volumes[0].options == ["create_host_path=true"]


def test_unquoted_port_is_not_read_as_base60(tmp_path):
    project = _one(tmp_path, "services:\n  a:\n    image: alpine\n    ports:\n      - 22:22\n      - 8080\n")
    assert [p.to_flag() for p in project.services["a"].ports] == ["22:22", "8080"]


def test_yaml_numbers_still_parse(tmp_path):
    project = _one(tmp_path, """
services:
  a:
    image: alpine
    environment: {I: 10, F: 1.5, O: 0x1F, NEG: -3}
    deploy: {replicas: 2}
""")
    a = project.services["a"]
    assert a.environment == {"I": "10", "F": "1.5", "O": "31", "NEG": "-3"}
    assert a.replicas == 2


def test_invalid_yaml_is_a_compose_error(tmp_path):
    with pytest.raises(ComposeError, match="invalid YAML"):
        _one(tmp_path, "services:\n  a:\n    image: [alpine\n")
    with pytest.raises(ComposeError, match="invalid YAML"):
        _one(tmp_path, "services:\n  a:\n    image: !unknown alpine\n")


def test_reset_and_override_merge_tags(tmp_path):
    base = tmp_path / "compose.yaml"
    base.write_text("""
services:
  a:
    image: alpine
    ports: ["8080:80"]
    environment: {A: "1", B: "2"}
    labels: {x: "1"}
  gone: {image: alpine}
""")
    over = tmp_path / "override.yaml"
    over.write_text("""
services:
  a:
    ports: !reset []
    environment: !override {C: "3"}
    labels: !reset null
  gone: !reset
""")
    project = load_project([str(base), str(over)])
    a = project.services["a"]
    assert (a.ports, a.environment, a.labels) == ([], {"C": "3"}, {})
    assert "gone" not in project.services


def test_merge_tags_in_a_single_file(tmp_path):
    # without a file to merge into, the tags only unwrap or drop their value
    project = _one(tmp_path, "services:\n  a:\n    image: !override alpine\n    ports: !reset [\"1:1\"]\n"
                             "    dns: [!reset 1.1.1.1, 8.8.8.8]\n")
    a = project.services["a"]
    assert (a.image, a.ports, a.dns) == ("alpine", [], ["8.8.8.8"])


def test_include_rebases_relative_paths_without_dot(tmp_path):
    (tmp_path / "compose.yaml").write_text("include: [sub/compose.yaml]\nservices:\n  main: {image: alpine}\n")
    sub = tmp_path / "sub"
    (sub / "ctx").mkdir(parents=True)
    (sub / "tok.txt").write_text("x")
    (sub / "common.env").write_text("A=1\n")
    (sub / "compose.yaml").write_text("""
services:
  s:
    build: ctx
    env_file: common.env
    secrets: [tok]
    volumes:
      - type: bind
        source: data
        target: /data
      - named:/v
volumes:
  named:
secrets:
  tok: {file: tok.txt}
""")
    project = load_project(str(tmp_path / "compose.yaml"))
    s = project.services["s"]
    assert s.build.context == str(sub / "ctx")
    assert s.env_files == [str(sub / "common.env")]
    assert project.secrets["tok"].file == str(sub / "tok.txt")
    assert s.volumes[0].source == str(sub / "data")
    assert s.volumes[1].type == "volume"  # short syntax `named:` stays a named volume


def test_windows_style_relative_bind(tmp_path):
    project = _one(tmp_path, "services:\n  a:\n    image: alpine\n    volumes: ['.\\\\data:/data']\n")
    mount = project.services["a"].volumes[0]
    assert (mount.type, mount.source) == ("bind", str(tmp_path / "data"))


def test_deploy_replicas_zero(tmp_path):
    project = _one(tmp_path, "services:\n  a:\n    image: alpine\n    deploy: {replicas: 0}\n")
    assert project.services["a"].replicas == 0


def test_env_file_paths_and_long_syntax(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = _one(tmp_path, r"""
services:
  a:
    image: alpine
    env_file:
      - C:\cfg\app.env
      - ~/app.env
      - path: ./present.env
      - path: ./missing.env
        required: false
      - path: ./required-missing.env
""", files={"present.env": "A=1\n"})
    assert project.services["a"].env_files == [
        "C:\\cfg\\app.env",
        str(tmp_path / "home" / "app.env"),
        str(tmp_path / "present.env"),
        str(tmp_path / "required-missing.env"),  # still handed to wslc, which reports it
    ]


def test_dotenv_inline_comments(tmp_path):
    (tmp_path / ".env").write_text(
        'TAG=3.20 # pinned\nQ="hello" # c\nS=\'it\'\nHASH=a#b\nESC="a\\"b"\nWIN="C:\\new"\n')
    project = _one(tmp_path, """
services:
  a:
    image: alpine:${TAG}
    environment: {Q: "${Q}", S: "${S}", HASH: "${HASH}", ESC: "${ESC}", WIN: "${WIN}"}
""")
    a = project.services["a"]
    assert a.image == "alpine:3.20"
    assert a.environment == {"Q": "hello", "S": "it", "HASH": "a#b", "ESC": 'a\\"b', "WIN": "C:\\new"}


def test_port_ip_with_random_host_port():
    assert [p.to_flag() for p in parse_port("127.0.0.1::80")] == ["127.0.0.1::80"]
    assert [p.to_flag() for p in parse_port({"target": 80, "host_ip": "127.0.0.1"})] == ["127.0.0.1::80"]
    assert [p.to_flag() for p in parse_port({"target": 80, "host_ip": "127.0.0.1", "published": 8080})] \
        == ["127.0.0.1:8080:80"]
    with pytest.raises(ComposeError, match="invalid port mapping"):
        parse_port("abc:80")


def test_environment_without_value_comes_from_shell_or_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("FROM_DOTENV=d\n")
    monkeypatch.setenv("FROM_SHELL", "s")
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    project = _one(tmp_path, """
services:
  a:
    image: alpine
    environment: [FROM_SHELL, FROM_DOTENV, NOT_SET_ANYWHERE]
""")
    assert project.services["a"].environment == {
        "FROM_SHELL": "s", "FROM_DOTENV": "d", "NOT_SET_ANYWHERE": None}


def test_extends_same_file_and_other_file(tmp_path):
    (tmp_path / "common").mkdir()
    (tmp_path / "common" / "base.yaml").write_text("""
services:
  base:
    image: alpine
    env_file: base.env
    environment: {LEVEL: base, KEEP: "1"}
    ports: ["8080:80"]
""")
    project = _one(tmp_path, """
services:
  root:
    image: busybox
    environment: {ROOT: "1"}
  mid:
    extends: root
    command: ["echo", "mid"]
  leaf:
    extends: {service: mid}
    environment: {LEAF: "1"}
  remote:
    extends: {service: base, file: common/base.yaml}
    environment: {LEVEL: remote}
    ports: ["9090:90"]
""")
    leaf = project.services["leaf"]
    assert (leaf.image, leaf.command) == ("busybox", ["echo", "mid"])
    assert leaf.environment == {"ROOT": "1", "LEAF": "1"}
    remote = project.services["remote"]
    assert remote.environment == {"LEVEL": "remote", "KEEP": "1"}
    assert [p.to_flag() for p in remote.ports] == ["8080:80", "9090:90"]
    assert remote.env_files == [str(tmp_path / "common" / "base.env")]


@pytest.mark.parametrize("body, message", [
    ("services:\n  a: {image: x, extends: b}\n  b: {image: x, extends: a}\n", "circular extends"),
    ("services:\n  a: {extends: nope}\n", "service 'nope' not found"),
    ("services:\n  a: {image: x, extends: {file: x.yaml}}\n", "extends needs a 'service'"),
    ("services:\n  a: {image: x, extends: {service: b, file: missing.yaml}}\n", "extends file not found"),
])
def test_extends_errors(tmp_path, body, message):
    with pytest.raises(ComposeError, match=message):
        _one(tmp_path, body)


def test_extends_file_paths_in_included_file(tmp_path):
    (tmp_path / "compose.yaml").write_text("include: [sub/compose.yaml]\nservices:\n  main: {image: alpine}\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "base.yaml").write_text("services:\n  base: {image: busybox}\n")
    (sub / "compose.yaml").write_text("services:\n  s:\n    extends: {service: base, file: base.yaml}\n")
    project = load_project(str(tmp_path / "compose.yaml"))
    assert project.services["s"].image == "busybox"


def test_ipv6_ports():
    assert [p.to_flag() for p in parse_port("[::1]:8080:80")] == ["[::1]:8080:80"]
    assert [p.to_flag() for p in parse_port("[::1]::80")] == ["[::1]::80"]
    assert [p.to_flag() for p in parse_port("[::1]:8080-8081:80-81/udp")] == \
        ["[::1]:8080:80/udp", "[::1]:8081:81/udp"]
    assert [p.to_flag() for p in parse_port({"target": 80, "host_ip": "::1", "published": 8080})] == \
        ["[::1]:8080:80"]
    with pytest.raises(ComposeError, match="invalid port mapping"):
        parse_port("[::1]80")


def test_dotenv_expands_references_without_breaking_literal_dollars(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELL_USER", "bob")
    monkeypatch.delenv("word", raising=False)
    (tmp_path / ".env").write_text(
        "BASE=/srv\n"
        "DATA=${BASE}/data\n"
        "HOME_DIR=\"/home/${SHELL_USER}\"\n"
        "LITERAL='${BASE}'\n"
        "PASS=pa$word\n"
        "DOUBLE=a$$b\n"
        "KNOWN=$BASE/x\n"
        "DEF=${MISSING:-fallback}\n")
    project = _one(tmp_path, """
services:
  a:
    image: alpine
    environment: [DATA, HOME_DIR, LITERAL, PASS, DOUBLE, KNOWN, DEF]
""")
    assert project.services["a"].environment == {
        "DATA": "/srv/data", "HOME_DIR": "/home/bob", "LITERAL": "${BASE}",
        "PASS": "pa$word", "DOUBLE": "a$$b", "KNOWN": "/srv/x", "DEF": "fallback"}


def test_required_variable_errors_are_compose_errors(tmp_path):
    with pytest.raises(ComposeError, match="boom"):
        _one(tmp_path, "services:\n  a:\n    image: ${NOPE_X:?boom}\n")
    (tmp_path / ".env").write_text("A=${NOPE_Y:?from dotenv}\n")
    with pytest.raises(ComposeError, match="from dotenv"):
        _one(tmp_path, "services:\n  a:\n    image: alpine\n")


def test_entrypoint_and_command_reset_are_kept(tmp_path):
    project = _one(tmp_path, "services:\n  a:\n    image: alpine\n    entrypoint: []\n    command: []\n")
    a = project.services["a"]
    assert (a.entrypoint, a.command) == ([], [])
