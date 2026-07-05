from wslc_compose import LABEL_PROJECT
from wslc_compose.flags import build_args, image_name, run_args
from wslc_compose.loader import load_project

COMPOSE = """
name: demo
services:
  web:
    image: nginx:alpine
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


def test_run_args(tmp_path):
    project = make_project(tmp_path)
    args = run_args(project, project.services["web"], path_mapper=lambda p: f"WIN({p})")

    assert args[0] == "run"
    assert "-d" in args
    assert args[args.index("--name") + 1] == "demo-web-1"
    assert f"{LABEL_PROJECT}=demo" in args
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
