import json

from wslc_compose import LABEL_CONFIG_FILES, LABEL_PROJECT
from wslc_compose.flags import build_args, image_name, run_args
from wslc_compose.loader import load_project
from wslc_compose.model import HealthCheck, Service

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

    assert original.startswith("v1:")
    assert original == web.config_hash()


def test_experimental_config_hash_v2_matches_shared_golden_vector():
    service = Service(
        name="web", image="fixture", stdin_open=True, gpus="all", tty=True, stop_timeout_seconds=90
    )
    service.healthcheck = HealthCheck(test=["CMD-SHELL", "true"], interval="30s", timeout="5s", start_period="10s", retries=3)

    assert service.config_hash_v2() == (
        "v2:a77054eda47a36a70a8d427936e96e84bbd6d285e9ac35836a1e0205a953f603"
    )


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


def test_run_args(tmp_path):
    project = make_project(tmp_path)
    args = run_args(project, project.services["web"], path_mapper=lambda p: f"WIN({p})")

    assert args[0] == "run"
    assert "-d" in args
    assert args[args.index("--name") + 1] == "demo-web-1"
    assert f"{LABEL_PROJECT}=demo" in args
    assert f"{LABEL_CONFIG_FILES}={json.dumps([str(tmp_path / 'compose.yaml')], separators=(',', ':'))}" in args
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
