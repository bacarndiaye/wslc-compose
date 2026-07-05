# wslc-compose

**docker-compose style orchestration for [WSL containers (wslc)](https://learn.microsoft.com/windows/wsl/wsl-container).**

Microsoft's new `wslc` CLI (WSL container preview) manages single containers much like
`docker run`, but has [no Compose support yet](https://github.com/microsoft/WSL/issues/40948).
`wslc-compose` fills that gap: point it at the `docker-compose.yml` / `compose.yaml` you
already use with Docker or Podman, and it drives `wslc` for you — networks, named volumes,
bind mounts, dependency ordering, project-scoped naming and labels included.

It installs a `wslc` wrapper command, so the muscle memory from `docker compose` /
`podman compose` just works — any other subcommand is passed through to the real wslc CLI:

```console
$ cd my-project        # contains compose.yaml
$ wslc compose up -d
Network my-project_default created
Volume my-project_data created
Creating my-project-db-1 ...
Creating my-project-web-1 ...
$ wslc compose ps
NAME               SERVICE  IMAGE         STATUS   PORTS
my-project-db-1    db       postgres:16   running
my-project-web-1   web      nginx:alpine  running  0.0.0.0:8080->80/tcp
$ wslc compose logs -f web
$ wslc compose down -v
$ wslc images            # ← not a compose command: forwarded to wslc.exe verbatim
```

(`wslc-compose` is also installed as a standalone command if you prefer.)

## Requirements

- Windows 11 with the **WSL container preview** installed (`wslc` available) —
  see the [official docs](https://learn.microsoft.com/windows/wsl/wsl-container)
- Python ≥ 3.9 (inside a WSL distro or on Windows)

## Install

One-liner (works even on WSL distros without pip/venv/pipx — it bootstraps
[uv](https://docs.astral.sh/uv/) standalone if needed):

```console
curl -fsSL https://raw.githubusercontent.com/bacarndiaye/wslc-compose/main/install.sh | sh
```

Or, if you already have a Python package manager:

```console
pipx install git+https://github.com/bacarndiaye/wslc-compose
# or
uv tool install --from git+https://github.com/bacarndiaye/wslc-compose wslc-compose
# or
pip install --user git+https://github.com/bacarndiaye/wslc-compose
```

From inside WSL, `wslc-compose` finds `wslc.exe` automatically (PATH or
`C:\Program Files\WSL\wslc.exe`) and transparently converts Linux paths of bind
mounts to Windows paths with `wslpath`. Set `WSLC_COMPOSE_BIN` to override the
binary location.

## Commands

| Command | Description |
|---|---|
| `up [-d] [--build] [--force-recreate] [--scale svc=N] [SERVICE...]` | create networks/volumes, build missing images, start services in `depends_on` order; recreates containers whose config changed |
| `down [-v]` | stop & remove the project's containers and networks (`-v`: also named volumes) |
| `ps [-q]` | list project containers with status and ports |
| `logs [-f] [-n N] [-t] [SERVICE...]` | show (and follow) logs, multiplexed and prefixed per container |
| `exec SERVICE CMD...` | run a command in a running service container |
| `start / stop / restart [SERVICE...]` | lifecycle of existing containers |
| `pull [SERVICE...]` | pull service images |
| `build [--no-cache] [SERVICE...]` | build services that have a `build:` section |
| `config` | print the fully resolved configuration |
| `version` | show wslc-compose and wslc versions |

Global options: `-f FILE`, `-p PROJECT_NAME`, `--env-file FILE`, `--profile NAME`,
`--dry-run` (print the `wslc` commands instead of running them — great for debugging).

## Supported compose features

- `services` with `image` or `build` (context, dockerfile, args, target, pull)
- `command`, `entrypoint`, `container_name`, `hostname`, `domainname`, `user`, `working_dir`
- `environment` (list & map form), `env_file`, `.env` + full variable interpolation
  (`${VAR}`, `${VAR:-default}`, `${VAR:?error}`, `${VAR:+alt}`, `$$` escaping)
- `ports` (short & long syntax, `ip:host:container`, `/udp`, ranges)
- `volumes`: named volumes, bind mounts (relative, absolute, `~`, and Windows `E:\...` paths), `:ro`, `tmpfs`
- `networks` incl. `aliases` and `external: true` — service names and aliases are DNS-resolvable
  between containers, exactly like Docker
- `depends_on` (list & map form) → topological start order
- `deploy.replicas` / `--scale`, resource limits (`cpus`, `mem_limit`,
  `deploy.resources.limits`), `shm_size`, `ulimits`, `stop_signal`, GPU reservations → `--gpus`
- `labels`, `profiles`, `stdin_open`, `tty`, `dns*`
- project-scoped naming (`<project>-<service>-<n>`, `<project>_<network>`) and tracking labels
  (`com.wslc-compose.*`), so several projects coexist cleanly

## Current wslc limitations (preview)

`wslc` is a preview; some Docker features have no equivalent yet, and `wslc-compose`
tells you (warning at load time) instead of failing silently:

- **`restart:` policies** — not supported by wslc; ignored.
- **`healthcheck`** and `depends_on: condition: service_healthy` — not supported;
  conditions fall back to *service_started*.
- **One network per container** — wslc `run` accepts a single `--network`; the first
  network of a service is used.
- **Bind mounts need Windows host paths** — handled automatically via `wslpath`,
  but paths living *inside* a distro filesystem (`\\wsl.localhost\...`) may not be mountable.
- **Session mount limit** — current preview allows ~15 mounted volumes per WSL session
  (`Too many volumes have been mounted (limit: 15)`), fixed in a future WSL release;
  restart WSL (`wsl --shutdown`) to reset.
- `privileged`, `cap_add`, `devices`, `sysctls`, `secrets`, `configs`, `extra_hosts`,
  `logging` — not configurable with wslc; ignored with a warning.

When wslc gains native Compose support ([microsoft/WSL#40948](https://github.com/microsoft/WSL/issues/40948))
or a Docker-compatible API endpoint ([microsoft/WSL#40976](https://github.com/microsoft/WSL/issues/40976)),
this tool becomes a friendly bridge you can drop.

## Try the demo

A full-featured example (shared network with aliases, named volume, bind mount,
env interpolation, `depends_on`, published ports) lives in [`examples/demo`](examples/demo):

```console
cd examples/demo
wslc-compose up -d
curl http://localhost:8088                 # nginx serving the bind-mounted page
wslc-compose exec app cat /data/log.txt    # worker writing to the shared volume
wslc-compose logs -f
wslc-compose down -v
```

An image-build example lives in [`examples/build`](examples/build).

## Development

```console
pip install -e .[dev]  # or: pip install -e . pytest ruff
pytest
ruff check src tests
```

## License

[MIT](LICENSE)
