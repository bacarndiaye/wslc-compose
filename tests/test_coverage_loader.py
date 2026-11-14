"""Edge cases of the compose loader: parsing errors, merge rules and less common keys."""

import os

import pytest

from wslc_compose import loader
from wslc_compose.loader import (
    ComposeError,
    find_compose_file,
    load_dotenv,
    load_project,
    parse_port,
)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def _load(tmp_path, body, **kwargs):
    return load_project(_write(tmp_path / "compose.yaml", body), **kwargs)


def _svc(tmp_path, body, name="a"):
    return _load(tmp_path, body).services[name]


# --- discovery, .env and project name -------------------------------------------


def test_find_compose_file_prefers_compose_yaml(tmp_path):
    assert find_compose_file(str(tmp_path)) is None
    _write(tmp_path / "docker-compose.yml", "services: {}\n")
    assert find_compose_file(str(tmp_path)) == os.path.join(str(tmp_path), "docker-compose.yml")
    _write(tmp_path / "compose.yaml", "services: {}\n")
    assert find_compose_file(str(tmp_path)) == os.path.join(str(tmp_path), "compose.yaml")


def test_dotenv_skips_noise_and_accepts_export_prefix(tmp_path):
    path = _write(tmp_path / ".env", "\n# comment\nNOT_AN_ASSIGNMENT\nexport  A=1\nB=${A}2\n")
    assert load_dotenv(path, environ={}) == {"A": "1", "B": "12"}


def test_dotenv_unterminated_quote_is_kept_literally(tmp_path):
    path = _write(tmp_path / ".env", "A=\"open\nB='x' trailing\n")
    assert load_dotenv(path, environ={}) == {"A": "\"open", "B": "'x' trailing"}


def test_dotenv_missing_file_is_empty(tmp_path):
    assert load_dotenv(str(tmp_path / "nope.env"), environ={}) == {}


def test_dotenv_interpolation_error_names_the_file(tmp_path, monkeypatch):
    monkeypatch.delenv("WSLC_TEST_REQUIRED", raising=False)
    env = _write(tmp_path / ".env", "A=${WSLC_TEST_REQUIRED:?must be set}\n")
    with pytest.raises(ComposeError, match="must be set") as info:
        _load(tmp_path, "services:\n  a: {image: alpine}\n")
    assert str(info.value).startswith(env)


def test_project_name_that_normalizes_to_nothing_is_rejected(tmp_path):
    with pytest.raises(ComposeError, match="cannot derive a valid project name from '!!!'"):
        _load(tmp_path, "services:\n  a: {image: alpine}\n", project_name="!!!")


def test_project_name_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    body = "name: From-File\nservices:\n  a: {image: alpine}\n"
    assert _load(tmp_path, body).name == "from-file"
    assert _load(tmp_path, body, project_name="Cli_Name").name == "cli_name"
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "envname")
    assert _load(tmp_path, body).name == "envname"


def test_explicit_env_file_is_recorded(tmp_path, monkeypatch):
    monkeypatch.delenv("WSLC_TEST_TAG", raising=False)
    env = _write(tmp_path / "custom.env", "WSLC_TEST_TAG=3.19\n")
    project = _load(tmp_path, "services:\n  a: {image: 'alpine:${WSLC_TEST_TAG}'}\n", env_file=env)
    assert project.env_file == os.path.abspath(env)
    assert project.services["a"].image == "alpine:3.19"


# --- file structure errors --------------------------------------------------------


def test_no_compose_file_given():
    with pytest.raises(ComposeError, match="no compose file given"):
        load_project([])


@pytest.mark.parametrize("body, message", [
    ("- just\n- a list\n", "not a valid compose file"),
    ("include: other.yaml\nservices:\n  a: {image: alpine}\n", "'include' must be a list"),
    ("include: [{}]\nservices:\n  a: {image: alpine}\n", "malformed 'include' entry"),
    ("include: [{path: []}]\nservices:\n  a: {image: alpine}\n", "malformed 'include' entry"),
    ("include: ['']\nservices:\n  a: {image: alpine}\n", "malformed 'include' entry"),
    ("services:\n  a: {image: alpine}\nnetworks: [front]\n", "networks: expected mapping"),
    ("services:\n  a:\n", "service 'a' is empty"),
    ("services:\n  a: {command: echo}\n", "service 'a' needs 'image' or 'build'"),
])
def test_structural_errors(tmp_path, body, message):
    with pytest.raises(ComposeError, match=message):
        _load(tmp_path, body)


def test_include_into_null_section(tmp_path):
    _write(tmp_path / "extra.yaml", "services:\n  b: {image: busybox}\nvolumes:\n  data: {}\n")
    project = _load(tmp_path, "include: [extra.yaml]\nservices:\n  a: {image: alpine}\nvolumes:\n")
    assert sorted(project.services) == ["a", "b"]
    assert project.volumes["data"].name == f"{project.name}_data"


def test_include_list_of_paths_in_mapping(tmp_path):
    _write(tmp_path / "one.yaml", "services:\n  one: {image: busybox}\n")
    _write(tmp_path / "two.yaml", "services:\n  two: {image: busybox}\n")
    project = _load(tmp_path, "include:\n  - path: [one.yaml, two.yaml]\nservices:\n  a: {image: alpine}\n")
    assert sorted(project.services) == ["a", "one", "two"]


# --- YAML tags and booleans -------------------------------------------------------


def test_override_tag_on_a_sequence_replaces_instead_of_appending(tmp_path):
    base = _write(tmp_path / "compose.yaml", "services:\n  a:\n    image: alpine\n    ports: ['8080:80']\n")
    over = _write(tmp_path / "over.yaml", "services:\n  a:\n    ports: !override ['9090:90']\n")
    project = load_project([base, over])
    assert [p.to_flag() for p in project.services["a"].ports] == ["9090:90"]


def test_override_tag_on_a_whole_service(tmp_path):
    base = _write(tmp_path / "compose.yaml",
                  "services:\n  a:\n    image: alpine\n    ports: ['8080:80']\n    tty: true\n")
    over = _write(tmp_path / "over.yaml", "services:\n  a: !override {image: busybox}\n")
    a = load_project([base, over]).services["a"]
    assert (a.image, a.ports, a.tty) == ("busybox", [], False)


def test_numeric_booleans(tmp_path):
    a = _svc(tmp_path, "services:\n  a: {image: alpine, tty: 1, stdin_open: 0.0}\n")
    assert (a.tty, a.stdin_open) == (True, False)


def test_non_string_scalar_is_not_a_boolean(tmp_path):
    # YAML timestamps are neither booleans nor strings
    with pytest.raises(ComposeError, match=r"volumes\.data\.external: expected a boolean"):
        _load(tmp_path, "services:\n  a: {image: alpine}\nvolumes:\n  data: {external: 2024-01-01}\n")


# --- simple service keys ----------------------------------------------------------


def test_scalar_list_keys_accept_a_single_string(tmp_path):
    a = _svc(tmp_path, "services:\n  a: {image: alpine, dns: 1.1.1.1, tmpfs: /run, profiles: debug}\n")
    assert (a.dns, a.tmpfs, a.profiles) == (["1.1.1.1"], ["/run"], ["debug"])


def test_labels_list_form(tmp_path):
    a = _svc(tmp_path, "services:\n  a:\n    image: alpine\n    labels: ['a=1', 'b', 'c=x=y']\n")
    assert a.labels == {"a": "1", "b": "", "c": "x=y"}


@pytest.mark.parametrize("body, message", [
    ("labels: oops", "labels: expected list or mapping, got str"),
    ("environment: FOO=1", "environment: expected list or mapping, got str"),
    ("healthcheck: 'curl localhost'", "healthcheck: expected mapping"),
    ("healthcheck: {test: [EXEC, true]}", "healthcheck.test: expected CMD, CMD-SHELL, or NONE"),
    ("stop_grace_period: soon", "stop_grace_period: invalid duration 'soon'"),
    ("stop_grace_period: 1m30", "stop_grace_period: invalid duration '1m30'"),
    ("cap_add: [NET_ADMIN, 'CAP_']", "service 'a': cap_add contains an empty capability"),
    ("env_file: [{required: false}]", "service 'a': env_file entries need a 'path'"),
    ("deploy: {replicas: many}", "service 'a': deploy.replicas must be a number"),
    ("deploy: {replicas: [1]}", "service 'a': deploy.replicas must be a number"),
    ("deploy: {replicas: -1}", "service 'a': deploy.replicas cannot be negative"),
    ("depends_on: b", "service 'a': depends_on: expected list or mapping"),
    ("depends_on: {b: yes}", "service 'a': depends_on.b: expected a mapping"),
])
def test_invalid_service_settings(tmp_path, body, message):
    with pytest.raises(ComposeError, match=message):
        _load(tmp_path, f"services:\n  a:\n    image: alpine\n    {body}\n  b:\n    image: alpine\n")


@pytest.mark.parametrize("value, seconds", [(10, 10), ("'45'", 45), ("1m30s", 90), ("200ms", 1)])
def test_stop_grace_period(tmp_path, value, seconds):
    a = _svc(tmp_path, f"services:\n  a: {{image: alpine, stop_grace_period: {value}}}\n")
    assert a.stop_timeout_seconds == seconds


def test_ulimits_mapping_and_scalar_forms(tmp_path):
    a = _svc(tmp_path, """
services:
  a:
    image: alpine
    ulimits:
      nproc: 65535
      nofile: {soft: 1024, hard: 4096}
      core: {hard: 0}
""")
    assert a.ulimits == ["nproc=65535", "nofile=1024:4096", "core=-1:0"]


def test_gpu_reservations_and_shorthand(tmp_path):
    project = _load(tmp_path, """
services:
  all:
    image: cuda
    deploy:
      resources:
        reservations:
          devices:
            - capabilities: [compute]
            - capabilities: [gpu]
  counted:
    image: cuda
    deploy:
      resources:
        reservations:
          devices: [{capabilities: [gpu], count: 2}]
  none:
    image: cuda
    deploy:
      resources:
        reservations:
          devices: [{driver: nvidia}]
  short:
    image: cuda
    gpus: all
""")
    gpus = {name: svc.gpus for name, svc in project.services.items()}
    assert gpus == {"all": "all", "counted": "2", "none": None, "short": "all"}


def test_resource_limits_v2_and_deploy(tmp_path):
    project = _load(tmp_path, """
services:
  v2: {image: alpine, mem_limit: 64m, cpus: 0.25}
  deploy:
    image: alpine
    mem_limit: 64m
    cpus: 0.25
    deploy: {resources: {limits: {memory: 128m, cpus: 1.5}}}
""")
    v2, dep = project.services["v2"], project.services["deploy"]
    assert (v2.mem_limit, v2.cpus) == ("64m", "0.25")
    assert (dep.mem_limit, dep.cpus) == ("128m", "1.5")


def test_extra_hosts_forms_warn(tmp_path):
    project = _load(tmp_path, """
services:
  listed: {image: alpine, extra_hosts: ['db:10.0.0.5']}
  mapped:
    image: alpine
    extra_hosts: {cache: 10.0.0.6}
  plain: {image: alpine}
""")
    assert project.services["listed"].extra_hosts == ["db:10.0.0.5"]
    assert project.services["mapped"].extra_hosts == ["cache:10.0.0.6"]
    assert project.services["plain"].extra_hosts == []
    warned = [w.split(":")[0] for w in project.warnings if "extra_hosts" in w]
    assert warned == ["listed", "mapped"]


# --- ports -------------------------------------------------------------------------


def test_port_with_too_many_parts():
    with pytest.raises(ComposeError, match="invalid port mapping: '1:2:3:4'"):
        parse_port("1:2:3:4")


def test_port_range_mismatch():
    with pytest.raises(ComposeError, match="port range mismatch in '8000-8001:80'"):
        parse_port("8000-8001:80")


def test_port_ip_with_host_range():
    assert [p.to_flag() for p in parse_port("127.0.0.1:8000-8001:80-81")] == [
        "127.0.0.1:8000:80", "127.0.0.1:8001:81"]


# --- volumes -----------------------------------------------------------------------


@pytest.mark.parametrize("spec, message", [
    ("{type: volume, source: data}", "volume entry missing target"),
    ("/data", "anonymous volumes are not supported by wslc: '/data'"),
    ("data:/data:ro:extra", "invalid volume spec: 'data:/data:ro:extra'"),
])
def test_invalid_volume_specs(tmp_path, spec, message):
    with pytest.raises(ComposeError, match=message):
        _load(tmp_path, f"services:\n  a:\n    image: alpine\n    volumes: [{spec}]\nvolumes:\n  data: {{}}\n")


def test_undefined_named_volume(tmp_path):
    with pytest.raises(ComposeError, match="service 'a' references undefined volume 'cache'"):
        _load(tmp_path, "services:\n  a:\n    image: alpine\n    volumes: ['cache:/cache']\n")


def test_volume_without_config_and_external_volume(tmp_path):
    project = _load(tmp_path, """
name: proj
services:
  a:
    image: alpine
    volumes: ['data:/data', 'shared:/shared']
volumes:
  data:
  shared: {external: true}
""")
    assert [m.source for m in project.services["a"].volumes] == ["proj_data", "shared"]


def test_long_syntax_mount_options_are_translated_or_reported(tmp_path):
    project = _load(tmp_path, """
services:
  a:
    image: alpine
    volumes:
      - type: tmpfs
        target: /scratch
        tmpfs: {size: 64m, mode: 1777}
      - type: bind
        source: ./src
        target: /src
        bind: {create_host_path: true, recursive: 'off', propagation: rshared, selinux: z}
      - type: volume
        source: data
        target: /data
        volume: {nocopy: true, subpath: sub, bogus: 1}
volumes:
  data: {}
""")
    tmpfs, bind, vol = project.services["a"].volumes
    assert tmpfs.options == ["tmpfs-size=64m", "tmpfs-mode=1777"]
    assert bind.options == ["bind-recursive=false"]
    assert vol.options == []
    assert [w for w in project.warnings if "mount" in w] == [
        f"a: mount /src: bind.{key} is not supported by wslc (ignoring it)"
        for key in ("propagation", "selinux")
    ] + [
        f"a: mount /data: volume.{key} is not supported by wslc (ignoring it)"
        for key in ("nocopy", "subpath", "bogus")
    ]


# --- networks ----------------------------------------------------------------------


@pytest.mark.parametrize("networks, message", [
    ("front: oops", r"networks\.front: expected mapping"),
    ("front: {ipam: {config: [], labels: {}}}", r"networks\.front\.ipam: unsupported setting"),
    ("front: {ipam: {config: [{subnet: 10.0.0.0/31}]}}", "subnet needs network, gateway and broadcast"),
])
def test_invalid_network_definitions(tmp_path, networks, message):
    with pytest.raises(ComposeError, match=message):
        _load(tmp_path, f"services:\n  a: {{image: alpine, networks: [front]}}\nnetworks:\n  {networks}\n")


@pytest.mark.parametrize("networks", ["front", "{front: oops}"])
def test_invalid_service_networks(tmp_path, networks):
    with pytest.raises(ComposeError, match="networks must be a list or endpoint mapping"):
        _load(tmp_path, f"services:\n  a: {{image: alpine, networks: {networks}}}\nnetworks:\n  front: {{}}\n")


def test_networks_sharing_a_runtime_name_are_rejected(tmp_path):
    with pytest.raises(ComposeError, match="resolve to the same runtime name"):
        _load(tmp_path, """
services:
  a: {image: alpine, networks: [one, two]}
networks:
  one: {name: shared}
  two: {name: shared}
""")


def test_ipv4_address_on_network_without_subnet(tmp_path):
    a = _svc(tmp_path, """
name: p
services:
  a:
    image: alpine
    networks:
      front: {ipv4_address: 172.30.0.9, x-note: kept}
networks:
  front: {}
""")
    assert a.network_ipv4_addresses == {"p_front": "172.30.0.9"}


# --- secrets & configs -------------------------------------------------------------


@pytest.mark.parametrize("top, svc, message", [
    ("secrets: [tok]", "", "secrets: expected a mapping"),
    ("secrets: {tok: tok.txt}", "", "secret 'tok': expected a mapping with a 'file' source"),
    ("secrets: {tok: {file: tok.txt}}", "secrets: tok", "service 'a': secrets: expected a list"),
    ("secrets: {tok: {file: tok.txt}}", "secrets: [{target: x}]", "service 'a': secret entries require a source"),
    ("configs: {cfg: {file: tok.txt}}", "configs: [42]", "service 'a': config entries require a source"),
])
def test_invalid_secret_definitions_and_references(tmp_path, top, svc, message):
    _write(tmp_path / "tok.txt", "x")
    with pytest.raises(ComposeError, match=message):
        _load(tmp_path, f"services:\n  a:\n    image: alpine\n    {svc}\n{top}\n")


def test_secret_with_foreign_windows_file_is_not_checked_on_linux(tmp_path, monkeypatch):
    compose = _write(tmp_path / "compose.yaml", "services:\n  a: {image: alpine, secrets: [tok]}\n"
                                                "secrets:\n  tok: {file: 'C:\\secrets\\tok.txt'}\n")
    monkeypatch.setattr(loader.os, "name", "posix")  # WSL side: the file lives on Windows
    project = load_project(compose)
    assert project.secrets["tok"].file == "C:\\secrets\\tok.txt"
    assert project.services["a"].secrets[0].target == "/run/secrets/tok"


# --- include path rebasing ---------------------------------------------------------


def test_included_file_in_subdirectory_rebases_every_path_form(tmp_path):
    sub = tmp_path / "sub"
    absolute_env = _write(tmp_path / "abs.env", "A=1\n")
    _write(sub / "one.env", "B=2\n")
    _write(sub / "two.env", "C=3\n")
    _write(sub / "compose.yaml", f"""
services:
  built:
    build: {{context: ctx, dockerfile: Dockerfile.dev}}
    env_file:
      - one.env
      - {{path: two.env, required: false}}
      - '{absolute_env}'
    volumes:
      - '.\\data:/data'
      - type: volume
        source: named
        target: /named
  placeholder: {{image: alpine}}
volumes:
  named:
""")
    project = _load(tmp_path, "include: [sub/compose.yaml]\nservices:\n  main: {image: alpine}\n")
    built = project.services["built"]
    assert built.build.context == str(sub / "ctx")
    assert built.build.dockerfile == "Dockerfile.dev"
    assert built.env_files == [str(sub / "one.env"), str(sub / "two.env"), absolute_env]
    assert [(m.type, m.source) for m in built.volumes] == [
        ("bind", str(sub / "data")),
        ("volume", f"{project.name}_named"),
    ]


def test_included_null_service_is_still_reported_as_empty(tmp_path):
    _write(tmp_path / "sub" / "compose.yaml", "services:\n  broken:\n")
    with pytest.raises(ComposeError, match="service 'broken' is empty"):
        _load(tmp_path, "include: [sub/compose.yaml]\nservices:\n  main: {image: alpine}\n")


def test_included_non_file_secret_is_left_for_validation(tmp_path):
    _write(tmp_path / "sub" / "compose.yaml",
           "services:\n  s: {image: alpine}\nsecrets:\n  tok: {environment: TOKEN}\n")
    with pytest.raises(ComposeError, match="secret 'tok': only file-backed secrets are supported"):
        _load(tmp_path, "include: [sub/compose.yaml]\nservices:\n  main: {image: alpine}\n")


def test_extends_file_in_project_directory_keeps_paths(tmp_path):
    _write(tmp_path / "base.yaml", "services:\n  base:\n    build: ./ctx\n")
    a = _svc(tmp_path, "services:\n  a:\n    extends: {service: base, file: base.yaml}\n")
    assert a.build.context == str(tmp_path / "ctx")


# --- multi-file merge rules --------------------------------------------------------


def test_merge_null_base_mapping_and_list_to_mapping(tmp_path):
    base = _write(tmp_path / "compose.yaml", """
services:
  a:
    image: alpine
    environment:
    depends_on: [b]
    deploy: {resources: {limits: {cpus: '1'}}}
  b: {image: alpine, healthcheck: {test: [CMD, 'true']}}
""")
    over = _write(tmp_path / "over.yaml", """
services:
  a:
    environment: {A: '1'}
    depends_on: {b: {condition: service_healthy}}
    deploy: {replicas: 2}
""")
    a = load_project([base, over]).services["a"]
    assert a.environment == {"A": "1"}
    assert (a.depends_on, a.depends_on_conditions) == (["b"], {"b": "service_healthy"})
    assert (a.cpus, a.replicas) == ("1", 2)


def test_merge_with_non_mapping_value_takes_the_override(tmp_path):
    base = _write(tmp_path / "compose.yaml", "services:\n  a: {image: alpine, environment: {A: '1'}}\n")
    over = _write(tmp_path / "over.yaml", "services:\n  a: {environment: oops}\n")
    with pytest.raises(ComposeError, match="environment: expected list or mapping, got str"):
        load_project([base, over])


def test_merge_volumes_unparsable_before_interpolation_dedupes_by_text(tmp_path, monkeypatch):
    monkeypatch.delenv("WSLC_TEST_SRC", raising=False)
    monkeypatch.delenv("WSLC_TEST_DST", raising=False)
    spec = "'${WSLC_TEST_SRC:-./a}:${WSLC_TEST_DST:-/data}'"
    base = _write(tmp_path / "compose.yaml", f"services:\n  a:\n    image: alpine\n    volumes: [{spec}]\n")
    over = _write(tmp_path / "over.yaml", f"services:\n  a:\n    volumes: [{spec}]\n")
    mounts = load_project([base, over]).services["a"].volumes
    assert [(m.source, m.target) for m in mounts] == [(str(tmp_path / "a"), "/data")]


def test_merging_long_syntax_volumes_warns_about_options_only_once(tmp_path):
    base = _write(tmp_path / "compose.yaml", """
services:
  a:
    image: alpine
    volumes:
      - {type: bind, source: ./src, target: /src, bind: {propagation: rshared}}
      - ./old:/old
""")
    over = _write(tmp_path / "over.yaml", "services:\n  a:\n    volumes: ['./new:/old']\n")
    project = load_project([base, over])
    assert [(m.source, m.target) for m in project.services["a"].volumes] == [
        (str(tmp_path / "src"), "/src"), (str(tmp_path / "new"), "/old")]
    assert [w for w in project.warnings if "mount" in w] == [
        "a: mount /src: bind.propagation is not supported by wslc (ignoring it)"]
