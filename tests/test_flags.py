import json

import pytest

from wslc_compose import LABEL_CONFIG_FILES, LABEL_CONFIG_HASH, LABEL_PROJECT, engine
from wslc_compose.flags import build_args, image_name, run_args
from wslc_compose.loader import load_project
from wslc_compose.model import (
    BuildConfig,
    FileMount,
    FileResource,
    HealthCheck,
    Network,
    Project,
    Service,
    Volume,
    VolumeMount,
)

COMPOSE = """
name: demo
services:
  web:
    image: nginx:alpine
    stop_grace_period: 1m30s
    ports: ["8088:80"]
    volumes:
      - ./html:/usr/share/nginx/html:ro
      - data:/data
    environment:
      FOO: bar
    entrypoint: ["/docker-entrypoint.sh", "nginx"]
    command: ["-g", "daemon off;"]
    networks:
      front:
        aliases: [www]
    mem_limit: 256M
    cpus: 0.5
    user: "101"
    working_dir: /srv
  job:
    build:
      context: ./src
      dockerfile: Dockerfile.dev
      args: {VERSION: "1.2"}
      target: dev
networks:
  front:
volumes:
  data:
"""


def make_project(tmp_path):
    (tmp_path / "compose.yaml").write_text(COMPOSE)
    (tmp_path / "html").mkdir()
    (tmp_path / "src").mkdir()
    return load_project(str(tmp_path / "compose.yaml"))


def test_config_hash_is_versioned_and_order_independent(tmp_path):
    project = make_project(tmp_path)
    web = project.services["web"]
    original = web.config_hash()

    web.environment = dict(reversed(list(web.environment.items())))
    web.labels = dict(reversed(list(web.labels.items())))
    web.network_aliases = {name: list(reversed(aliases)) for name, aliases in web.network_aliases.items()}

    assert original.startswith("v2:")
    assert original == web.config_hash()


def test_config_hash_matches_shared_golden_vector():
    service = Service(
        name="web", image="fixture", stdin_open=True, gpus="all", tty=True, stop_timeout_seconds=90
    )
    service.healthcheck = HealthCheck(test=["CMD-SHELL", "true"], interval="30s", timeout="5s", start_period="10s", retries=3)

    assert service.config_hash() == (
        "v2:7761dc3ba3aec83fa3b3e25cad2dab47fd4dc3356f16d936db7e445f6acbbd29"
    )


def test_config_hash_matches_shared_shell_operator_vector():
    service = Service(name="web", image="fixture", command=["sh", "-c", "echo a && echo b"])
    assert service.config_hash() == "v2:b4690ac86a52bb4afac721d21138fd2fc10e9474ac4023dc85faee2a50615bc9"


def test_config_hash_matches_shared_ascii_escaping_and_unicode_key_order():
    service = Service(name="web", image="fixture", labels={"\ue000": "<>&'\"é😀\u007f\n", "\U00010000": "astral"})
    assert service.config_hash() == "v2:f89618003f02eb194df1cf76766b87371842c116134f5c31a376ee969fe0f0f4"


def test_run_args_include_stdin_and_gpu_flags(tmp_path):
    project = make_project(tmp_path)
    service = project.services["web"]
    service.stdin_open = True
    service.gpus = "all"
    service.tty = True
    service.stop_timeout_seconds = 90

    args = run_args(project, service)

    assert "-i" in args
    assert "-t" in args
    assert args[args.index("--gpus") + 1] == "all"
    assert args[args.index("--stop-timeout") + 1] == "90"


def test_static_ipv4_is_emitted_but_stays_outside_shared_hash(tmp_path):
    project = make_project(tmp_path)
    service = project.services["web"]
    original_hash = service.config_hash(project)
    service.network_ipv4_addresses["demo_front"] = "10.0.0.51"
    args = run_args(project, service)
    assert args[args.index("--ip") + 1] == "10.0.0.51"
    assert service.config_hash(project) == original_hash
    service.network_ipv4_addresses["demo_front"] = "10.0.0.52"
    assert service.config_hash(project) == original_hash
    service.network_mode = "none"
    assert "--ip" not in run_args(project, service)


def test_run_args_include_healthcheck_flags(tmp_path):
    project = make_project(tmp_path)
    service = project.services["web"]
    service.healthcheck = HealthCheck(
        test=["CMD-SHELL", "curl -f http://localhost/ || exit 1"], interval="30s",
        timeout="5s", start_period="10s", retries=3,
    )

    args = run_args(project, service)

    assert args[args.index("--health-cmd") + 1] == "curl -f http://localhost/ || exit 1"
    assert args[args.index("--health-interval") + 1] == "30s"
    assert args[args.index("--health-timeout") + 1] == "5s"
    assert args[args.index("--health-start-period") + 1] == "10s"
    assert args[args.index("--health-retries") + 1] == "3"


def test_disabled_healthcheck_emits_no_healthcheck(tmp_path):
    project = make_project(tmp_path)
    project.services["web"].healthcheck = HealthCheck(disabled=True)

    assert "--no-healthcheck" in run_args(project, project.services["web"])


def test_run_args_emit_structured_mount(tmp_path):
    project = make_project(tmp_path)
    service = project.services["web"]
    service.volumes = [VolumeMount("bind", str(tmp_path / "data"), "/data", True, ["propagation=rshared"], True)]

    args = run_args(project, service, path_mapper=lambda path: f"WIN({path})")

    assert args[args.index("--mount") + 1] == (
        f"type=bind,source=WIN({tmp_path / 'data'}),target=/data,readonly,propagation=rshared"
    )


def test_config_hash_matches_structured_mount_vector():
    service = Service(name="web", image="fixture", volumes=[
        VolumeMount("bind", "C:/data", "/data", True, ["propagation=rshared"], True)
    ])

    assert service.config_hash() == "v2:4aed1aa484bbbd21d5b9dd0bb844b5cd4b38cc8d61167714ad7a2b443f5df6a6"


def test_config_hash_matches_internal_network_declaration_vector():
    service = Service(name="web", image="fixture", networks=["demo_private"])
    project = Project(name="demo", directory=".", services={"web": service}, networks={
        "private": Network(key="private", name="demo_private", internal=True)
    })

    assert service.config_hash(project) == "v2:c3f312f032f286e3d0e3b9206d517d7554619ae1a7d7a319ffcae9005fd71295"

    network = project.networks["private"]
    network.subnet, network.gateway, network.ip_range = "10.1.0.0/24", "10.1.0.1", "10.1.0.128/25"
    network.driver, network.driver_opts, network.labels = "bridge", {"mtu": "1400"}, {"purpose": "test"}
    assert service.config_hash(project) == "v2:c3f312f032f286e3d0e3b9206d517d7554619ae1a7d7a319ffcae9005fd71295"


def test_config_hash_matches_volume_declaration_vector():
    service = Service(name="web", image="fixture", volumes=[VolumeMount("volume", "demo_data", "/data")])
    project = Project(name="demo", directory=".", services={"web": service}, volumes={
        "data": Volume(key="data", name="demo_data", driver="guest", driver_opts={"size": "1G"}, labels={"purpose": "cache"})
    })

    assert service.config_hash(project) == "v2:ea8b1dc7e8795f36d6114622ecd7dee6129c864d9c469f23a06c55b35b201a2d"


def test_config_hash_matches_build_details_vector():
    service = Service(name="web", image="fixture", build=BuildConfig(
        context=".", dockerfile="Dockerfile.custom", args={"B": "2", "A": "1"}, target="release", pull=True, no_cache=True
    ))

    assert service.config_hash() == "v2:164d845a98d8c8ab7bde344740746eece3ae176962411708b951be1497a0b0a9"


def test_run_args_mount_file_backed_resources_readonly(tmp_path):
    source = tmp_path / "secret.txt"
    source.write_text("secret")
    service = Service(name="web", image="fixture", secrets=[FileMount("token", "/run/secrets/token")])
    project = Project(name="demo", directory=".", services={"web": service}, secrets={
        "token": FileResource("token", str(source))
    })

    args = run_args(project, service, path_mapper=lambda path: f"WIN({path})")

    assert f"WIN({source}):/run/secrets/token:ro" in args


def test_config_hash_matches_file_resource_vector(tmp_path):
    source = tmp_path / "secret.txt"
    source.write_text("secret")
    service = Service(name="web", image="fixture", secrets=[FileMount("token", "/run/secrets/token")])
    project = Project(name="demo", directory=".", services={"web": service}, secrets={
        "token": FileResource("token", str(source))
    })

    assert service.config_hash(project) == "v2:fc35d18446cec55bb53d049552b93c0976d76391234c74a8b89e1cdbbc602e3e"


@pytest.mark.parametrize("mode", ["host", "none", "container:shared"])
def test_run_args_emit_network_mode(tmp_path, mode):
    project = make_project(tmp_path)
    service = project.services["web"]
    service.network_mode = mode

    args = run_args(project, service)

    assert args[args.index("--network") + 1] == mode


def test_run_args_resolve_service_network_mode(tmp_path):
    project = make_project(tmp_path)
    project.services["web"].network_mode = "service:job"

    args = run_args(project, project.services["web"])

    assert args[args.index("--network") + 1] == "container:demo-job-1"


def test_run_args_reject_unknown_service_network_mode(tmp_path):
    project = make_project(tmp_path)
    project.services["web"].network_mode = "service:missing"

    with pytest.raises(ValueError, match="unknown service"):
        run_args(project, project.services["web"])


def test_run_args(tmp_path):
    project = make_project(tmp_path)
    args = run_args(project, project.services["web"], path_mapper=lambda p: f"WIN({p})")

    assert args[0] == "run"
    assert "-d" in args
    assert args[args.index("--name") + 1] == "demo-web-1"
    assert f"{LABEL_PROJECT}=demo" in args
    assert f"{LABEL_CONFIG_FILES}={json.dumps([str(tmp_path / 'compose.yaml')], separators=(',', ':'))}" in args
    assert any(arg.startswith(f"{LABEL_CONFIG_HASH}=v2:") for arg in args)
    assert "FOO=bar" in args
    assert "8088:80" in args

    bind = f"WIN({tmp_path / 'html'}):/usr/share/nginx/html:ro"
    assert bind in args
    assert "demo_data:/data" in args

    assert args[args.index("--network") + 1] == "demo_front"
    aliases = [args[i + 1] for i, a in enumerate(args) if a == "--network-alias"]
    assert sorted(aliases) == ["web", "www"]

    assert args[args.index("--entrypoint") + 1] == "/docker-entrypoint.sh"
    # image followed by remaining entrypoint parts then command
    image_idx = args.index("nginx:alpine")
    assert args[image_idx + 1 :] == ["nginx", "-g", "daemon off;"]

    assert args[args.index("-m") + 1] == "256M"
    assert args[args.index("--cpus") + 1] == "0.5"
    assert args[args.index("-u") + 1] == "101"
    assert args[args.index("-w") + 1] == "/srv"


def test_build_args(tmp_path):
    project = make_project(tmp_path)
    job = project.services["job"]
    assert image_name(project, job) == "demo-job"

    args = build_args(project, job, no_cache=True, path_mapper=lambda p: f"WIN({p})")
    assert args[:3] == ["build", "-t", "demo-job"]
    assert args[args.index("-f") + 1] == f"WIN({tmp_path / 'src' / 'Dockerfile.dev'})"
    assert "VERSION=1.2" in args
    assert args[args.index("--target") + 1] == "dev"
    assert "--no-cache" in args
    assert args[-1] == f"WIN({tmp_path / 'src'})"


def _service_with_runtime_options():
    service = Service(name="web", image="x", restart="always", cap_add=["NET_ADMIN", "SYS_TIME"], cap_drop=["MKNOD"])
    return Project(name="demo", directory=".", services={"web": service}), service


def test_run_args_apply_restart_and_capabilities_when_supported(monkeypatch):
    monkeypatch.setattr(engine, "run_options", lambda: frozenset({"--restart", "--cap-add", "--cap-drop"}))
    project, service = _service_with_runtime_options()
    args = run_args(project, service, 1)
    assert args[args.index("--restart") + 1] == "always"
    assert [args[i + 1] for i, a in enumerate(args) if a == "--cap-add"] == ["NET_ADMIN", "SYS_TIME"]
    assert [args[i + 1] for i, a in enumerate(args) if a == "--cap-drop"] == ["MKNOD"]


def test_run_args_omit_unsupported_options(monkeypatch):
    monkeypatch.setattr(engine, "run_options", lambda: frozenset())
    project, service = _service_with_runtime_options()
    args = run_args(project, service, 1)
    assert "--restart" not in args and "--cap-add" not in args and "--cap-drop" not in args


def test_restart_and_capabilities_do_not_change_the_shared_hash():
    plain = Service(name="web", image="x")
    _, rich = _service_with_runtime_options()
    rich.name, rich.image = "web", "x"
    assert plain.config_hash() == rich.config_hash()


def test_healthcheck_cmd_form_keeps_argument_boundaries(tmp_path):
    # #45
    project = make_project(tmp_path)
    service = project.services["web"]
    service.healthcheck = HealthCheck(test=["CMD", "sh", "-c", "wget -q -O- http://localhost/ || exit 1"])
    args = run_args(project, service)
    assert args[args.index("--health-cmd") + 1] == "sh -c 'wget -q -O- http://localhost/ || exit 1'"
    service.healthcheck = HealthCheck(test=["CMD", "pg_isready", "-U", "postgres"])
    args = run_args(project, service)
    assert args[args.index("--health-cmd") + 1] == "pg_isready -U postgres"  # unchanged for simple args


def test_secret_with_unreadable_windows_path_does_not_break_the_hash(tmp_path):
    # #41: the README example `file: C:/Users/me/...` under WSL
    from wslc_compose.model import FileMount, FileResource
    project = make_project(tmp_path)
    service = project.services["web"]
    project.secrets = {"nuget": FileResource("nuget", "C:/Users/me/.nuget/private.NuGet.Config")}
    service.secrets = [FileMount("nuget", "/run/secrets/nuget")]
    first = service.config_hash(project)
    assert first == service.config_hash(project)
    project.secrets["nuget"].file = "C:/Users/me/other.config"
    assert service.config_hash(project) != first


def test_env_files_label_tracks_paths_and_contents(tmp_path):
    # #46: outside the shared v2 hash, so upgrading does not change any config-hash
    from wslc_compose import LABEL_LOCAL_CONFIG
    from wslc_compose.metadata import container_labels
    project = make_project(tmp_path)
    service = project.services["web"]
    env_file = tmp_path / "app.env"
    env_file.write_text("A=1\n")
    no_files = container_labels(project, service, 1)[LABEL_LOCAL_CONFIG]
    original_hash = service.config_hash(project)
    service.env_files = [str(env_file)]
    with_file = container_labels(project, service, 1)[LABEL_LOCAL_CONFIG]
    env_file.write_text("A=2\n")
    changed = container_labels(project, service, 1)[LABEL_LOCAL_CONFIG]
    assert len({no_files, with_file, changed}) == 3
    assert service.config_hash(project) == original_hash


def test_entrypoint_reset_and_command_reset(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    service = project.services["web"]
    image = image_name(project, service)
    service.entrypoint, service.command = [], ["echo", "hi"]
    args = run_args(project, service)
    assert args[args.index("--entrypoint") + 1] == ""
    assert args[args.index(image) + 1:] == ["echo", "hi"]

    # command: [] keeps the image ENTRYPOINT but drops its CMD
    monkeypatch.setattr(engine, "image_entrypoint", lambda name: ["/docker-entrypoint.sh", "-x"])
    service.entrypoint, service.command = None, []
    args = run_args(project, service)
    assert args[args.index("--entrypoint") + 1] == "/docker-entrypoint.sh"
    assert args[args.index(image) + 1:] == ["-x"]
    monkeypatch.setattr(engine, "image_entrypoint", lambda name: [])
    args = run_args(project, service)
    assert args[args.index("--entrypoint") + 1] == ""
    monkeypatch.setattr(engine, "image_entrypoint", lambda name: None)  # image not local
    assert "--entrypoint" not in run_args(project, service)


def test_healthcheck_options_without_test_tune_the_image_healthcheck(tmp_path):
    project = make_project(tmp_path)
    service = project.services["web"]
    service.healthcheck = HealthCheck(interval="5s", retries=2)
    args = run_args(project, service)
    assert "--health-cmd" not in args
    assert args[args.index("--health-interval") + 1] == "5s"
    assert args[args.index("--health-retries") + 1] == "2"


def test_local_config_digest_records_only_non_default_settings(tmp_path):
    from wslc_compose.model import Service, VolumeMount, local_config_digest
    plain = local_config_digest(Service(name="a", image="x"))
    assert plain == local_config_digest(Service(name="b", image="y"))
    with_options = Service(name="a", image="x", volumes=[
        VolumeMount("tmpfs", None, "/t", options=["tmpfs-size=1000"], structured=True)])
    bigger = Service(name="a", image="x", volumes=[
        VolumeMount("tmpfs", None, "/t", options=["tmpfs-size=2000"], structured=True)])
    digests = {plain, local_config_digest(with_options), local_config_digest(bigger),
               local_config_digest(Service(name="a", image="x", entrypoint=[])),
               local_config_digest(Service(name="a", image="x", command=[]))}
    assert len(digests) == 5


def test_env_file_passed_as_is_when_already_plain(tmp_path):
    from wslc_compose.flags import wslc_env_file
    plain = tmp_path / "plain.env"
    plain.write_text("# comment\nA=1\n\nB=two words\nPASSTHROUGH\n")
    assert wslc_env_file(str(plain)) == str(plain)
    assert wslc_env_file(str(tmp_path / "missing.env")) == str(tmp_path / "missing.env")


def test_env_file_compose_syntax_is_normalized_for_wslc(tmp_path, monkeypatch):
    # wslc rejects `export KEY=...` and keeps quotes / inline comments in values
    import os

    from wslc_compose import flags
    monkeypatch.setattr(flags, "_ENV_DIR", [])
    registered = []
    monkeypatch.setattr(flags.atexit, "register", lambda *a: registered.append(a))
    monkeypatch.setenv("WHO", "bob")
    fancy = tmp_path / "fancy.env"
    fancy.write_text("export E=exported\nQ=\"quoted\"\nC=val # comment\nS='single $x'\nH=${WHO}\nBARE\n")
    copy = flags.wslc_env_file(str(fancy))
    assert copy != str(fancy) and os.path.dirname(copy) == flags._ENV_DIR[0]
    with open(copy, encoding="utf-8") as handle:
        assert handle.read() == "E=exported\nQ=quoted\nC=val\nS=single $x\nH=bob\nBARE\n"
    assert registered and registered[0][1] == flags._ENV_DIR[0]  # removed at exit
    assert flags.wslc_env_file(str(fancy)) == copy  # same private directory is reused
    assert len(registered) == 1


def test_env_file_interpolation_error_is_a_wslc_error(tmp_path):
    from wslc_compose.flags import wslc_env_file
    bad = tmp_path / "bad.env"
    bad.write_text("A=${NOPE_ENV_X:?must be set}\n")
    with pytest.raises(engine.WslcError, match="must be set"):
        wslc_env_file(str(bad))


def test_run_args_pass_the_normalized_env_file(tmp_path, monkeypatch):
    from wslc_compose import flags
    project = make_project(tmp_path)
    service = project.services["web"]
    service.env_files = [str(tmp_path / "x.env")]
    monkeypatch.setattr(flags, "wslc_env_file", lambda path: path + ".normalized")
    args = run_args(project, service)
    assert args[args.index("--env-file") + 1] == str(tmp_path / "x.env") + ".normalized"
