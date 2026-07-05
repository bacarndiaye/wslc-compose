# wslc-compose

[![CI](https://github.com/bacarndiaye/wslc-compose/actions/workflows/ci.yml/badge.svg)](https://github.com/bacarndiaye/wslc-compose/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)

**docker-compose style orchestration for [WSL containers (wslc)](https://learn.microsoft.com/windows/wsl/wsl-container).**

Microsoft's new `wslc` CLI (the *WSL container* public preview, June 2026) manages single
containers much like `docker run`, but it has **no Compose support yet** — the WSL team
tracks the feature request in [microsoft/WSL#40948](https://github.com/microsoft/WSL/issues/40948)
and a Docker-compatible API endpoint in [microsoft/WSL#40976](https://github.com/microsoft/WSL/issues/40976).

`wslc-compose` fills that gap **today**: point it at the `docker-compose.yml` /
`compose.yaml` you already use with Docker or Podman, and it drives `wslc` for you —
networks with DNS, named volumes, bind mounts, dependency ordering, project-scoped
naming, config-drift detection and scaling included.

---

## Table of contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Migrating an existing wslc setup](#migrating-an-existing-wslc-setup)
- [Command reference](#command-reference)
- [Compose file support](#compose-file-support)
- [Variable interpolation](#variable-interpolation)
- [Naming conventions and labels](#naming-conventions-and-labels)
- [How `up` decides what to do](#how-up-decides-what-to-do)
- [Networking](#networking)
- [Volumes and path translation](#volumes-and-path-translation)
- [Known wslc preview limitations](#known-wslc-preview-limitations)
- [Troubleshooting](#troubleshooting)
- [Examples](#examples)
- [Project layout](#project-layout)
- [Development](#development)
- [License](#license)

---

## How it works

`wslc-compose` is a thin, dependency-light Python CLI (only PyYAML) that:

1. **Loads** your compose file (`compose.yaml`, `compose.yml`, `docker-compose.yml` or
   `docker-compose.yaml`, searched in the current directory and its parents), applies
   `.env` + environment variable interpolation, and normalizes everything into an
   internal model.
2. **Plans** the work: which networks/volumes must exist, which images must be built or
   pulled, in which order services must start (`depends_on` topological sort), and which
   existing containers are stale (config-hash comparison).
3. **Executes** plain `wslc` commands (`run`, `build`, `network create`, `volume create`,
   `stop`, `remove`, `logs`, ...). There is no daemon, no state file, no magic — run any
   step with `--dry-run` to see the exact `wslc` invocations and paste them in a terminal
   yourself if you want.

Because state lives entirely in wslc (via container labels), `wslc-compose` and the raw
`wslc` CLI can be mixed freely.

Two executables are installed:

| Command | Purpose |
|---|---|
| `wslc-compose` | the standalone compose CLI |
| `wslc` | a wrapper giving the docker/podman UX: `wslc compose up -d`; **any other subcommand is forwarded verbatim to the real wslc CLI** (`wslc images`, `wslc attach`, ...). A recursion guard makes sure the wrapper never invokes itself. |

## Requirements

- **Windows 11** with the **WSL container preview** installed — `wslc` must work in a
  terminal. See the [official documentation](https://learn.microsoft.com/windows/wsl/wsl-container).
- **Python ≥ 3.9**, inside a WSL distro or on Windows.
  (No pip/venv on your distro? The installer below handles that.)

## Installation

### One-liner (recommended)

Works even on WSL distros without `pip`, `venv` or `pipx` — it bootstraps a standalone
[uv](https://docs.astral.sh/uv/) if no Python package manager is found:

```console
curl -fsSL https://raw.githubusercontent.com/bacarndiaye/wslc-compose/main/install.sh | sh
```

Commands are installed into `~/.local/bin` (make sure it is on your `PATH`).

### With an existing package manager

```console
pipx install git+https://github.com/bacarndiaye/wslc-compose
# or
uv tool install --from git+https://github.com/bacarndiaye/wslc-compose wslc-compose
# or
pip install --user git+https://github.com/bacarndiaye/wslc-compose
```

### From a local checkout

```console
git clone https://github.com/bacarndiaye/wslc-compose
cd wslc-compose
WSLC_COMPOSE_SOURCE=$PWD sh install.sh     # or: pip install -e .
```

### Locating the wslc binary

`wslc-compose` finds the wslc CLI automatically, in this order:

1. `$WSLC_COMPOSE_BIN` (explicit override)
2. `wslc.exe` / `wslc` on `PATH` (Windows interop makes `wslc.exe` visible inside WSL)
3. `C:\Program Files\WSL\wslc.exe` (default install location)

## Quick start

```console
$ cd my-project              # contains compose.yaml
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
$ wslc compose exec web sh
$ wslc compose down -v

$ wslc images                # ← not a compose command: forwarded to wslc.exe verbatim
```

`wslc-compose up -d`, `wslc-compose ps`, ... work identically if you prefer the
standalone command.

## Migrating an existing wslc setup

Already running containers with raw `wslc run` commands or a home-grown script?
**[docs/MIGRATION.md](docs/MIGRATION.md)** walks through the whole move, end to end:
inventorying what runs, reusing already-built images (retag instead of rebuild),
retiring the old containers without breaking published ports, verifying, and rolling
back — plus a recovery cheat sheet for the preview's sharp edges (per-elevation
sessions, mount budget, wedged sessions).

## Command reference

### Global options

Apply to every subcommand and go **before** it: `wslc-compose -f other.yml up -d`.

| Option | Description |
|---|---|
| `-f, --file FILE` | compose file to use (default: auto-detect in `.` and parent directories) |
| `-p, --project-name NAME` | project name (default: `name:` key, else directory name) |
| `--env-file FILE` | alternate `.env` file for interpolation |
| `--profile NAME` | enable a compose profile (repeatable) |
| `--dry-run` | print every `wslc` command instead of executing it |
| `--version` | show the wslc-compose version |

### `up [SERVICE...]`

Create networks and volumes, build missing images, then create/start containers in
`depends_on` order. Idempotent: up-to-date running containers are left alone.

| Option | Description |
|---|---|
| `-d, --detach` | do not attach to logs after starting |
| `--build` | rebuild images of services that have a `build:` section |
| `--force-recreate` | recreate containers even if their configuration is unchanged |
| `--scale SERVICE=N` | override the number of replicas (repeatable) |
| `-t, --timeout SEC` | stop timeout when recreating (default 10) |

Without `-d`, logs of the started services are followed after startup; `Ctrl+C`
detaches **without stopping the containers**.

### `down`

Stop and remove all of the project's containers, then remove its non-external networks.

| Option | Description |
|---|---|
| `-v, --volumes` | also remove the project's non-external named volumes |
| `-t, --timeout SEC` | stop timeout (default 10) |

### `ps [SERVICE...]`

List the project's containers (name, service, image, status, ports).
`-q` prints container IDs only.

### `logs [SERVICE...]`

Show container logs, multiplexed and prefixed per container
(`my-project-web-1 | ...`).

| Option | Description |
|---|---|
| `-f, --follow` | follow log output |
| `-n, --tail N` | show only the last N lines |
| `-t, --timestamps` | show timestamps |

### `exec SERVICE CMD [ARG...]`

Run a command in a running container of SERVICE.

| Option | Description |
|---|---|
| `--index N` | pick the Nth replica (default 1) |
| `-u, --user USER` | run as user |
| `-w, --workdir DIR` | working directory |
| `-e, --env K=V` | extra environment variables (repeatable) |
| `-T, --no-tty` | disable TTY allocation (for scripts/pipes) |

A TTY is allocated automatically when stdin is a terminal.

### `start / stop / restart [SERVICE...]`

Lifecycle of existing project containers, without recreating them.
`restart` is emulated as stop + start (wslc has no native restart).

### `pull [SERVICE...]`

Pull the images of services that have an `image:` key.

### `build [--no-cache] [SERVICE...]`

Build every selected service that has a `build:` section, tagging the result
`<project>-<service>` (unless `image:` names it explicitly).

### `config`

Print the fully resolved configuration (after interpolation, normalization, name
prefixing) as YAML — useful to debug what wslc-compose actually sees.

### `version`

Show the wslc-compose and wslc versions.

## Compose file support

| Compose key | Mapping to wslc |
|---|---|
| `image` | `wslc run <image>` / `wslc pull` |
| `build` (`context`, `dockerfile`, `args`, `target`, `pull`) | `wslc build -t <project>-<service>` |
| `command`, `entrypoint` (string or list) | trailing args / `--entrypoint` |
| `container_name` | `--name` (disables scaling for that service) |
| `environment` (list & map), `env_file` | `-e`, `--env-file` |
| `ports` (short & long syntax, `ip:host:container`, `/udp`, ranges `8000-8005`) | `-p` |
| `volumes` — named volumes | `wslc volume create` + `-v name:/path` |
| `volumes` — bind mounts (`./rel`, `/abs`, `~`, `E:\win\path`), `:ro` | `-v` with [path translation](#volumes-and-path-translation) |
| `tmpfs` (top-level list or `type: tmpfs`) | `--tmpfs` |
| `networks` incl. `aliases`, `external: true`, custom `name:` | `wslc network create`, `--network`, `--network-alias` |
| `depends_on` (list & map) | topological start/creation order |
| `deploy.replicas` | number of containers (see also `--scale`) |
| `deploy.resources.limits.cpus` / `.memory`, `cpus`, `mem_limit` | `--cpus`, `-m` |
| `deploy.resources.reservations.devices` (gpu), `gpus` | `--gpus` |
| `shm_size`, `ulimits`, `stop_signal` | `--shm-size`, `--ulimit`, `--stop-signal` |
| `hostname`, `domainname`, `dns`, `dns_search`, `dns_opt` | `-h`, `--domainname`, `--dns*` |
| `user`, `working_dir` | `-u`, `-w` |
| `labels` (list & map) | `-l` (merged with the tracking labels below) |
| `profiles` | service skipped unless its profile is enabled or it is named explicitly |
| `stdin_open`, `tty` | `-i`, `-t` |
| `name` (top level) | default project name |

Keys that wslc cannot honor yet are **accepted and reported as a warning** instead of
failing, so your existing files keep working: `restart`, `healthcheck`, `privileged`,
`cap_add`/`cap_drop`, `devices`, `extra_hosts`, `sysctls`, `secrets`, `configs`, `init`,
`pid`, `ipc`, `read_only`, `security_opt`, `logging`.

Rejected with an explicit error (no silent surprise): anonymous volumes
(`- /data` without a source), references to undeclared networks/volumes, circular
`depends_on`, scaling a service that sets `container_name`.

## Variable interpolation

Identical to docker compose:

| Syntax | Behavior |
|---|---|
| `$VAR`, `${VAR}` | value, empty if unset |
| `${VAR:-default}` | default if unset **or empty** |
| `${VAR-default}` | default only if unset |
| `${VAR:?message}` / `${VAR?message}` | abort with message if missing |
| `${VAR:+alt}` / `${VAR+alt}` | alt if set |
| `$$` | literal `$` (e.g. `$$(cmd)` reaches the container shell as `$(cmd)`) |

Precedence: process environment > `.env` file (in the project directory, or
`--env-file`). Shell constructs like `$(date)` are left untouched.

## Naming conventions and labels

Everything is scoped by project so multiple projects coexist cleanly:

| Object | Name |
|---|---|
| container | `<project>-<service>-<n>` (or `container_name:`) |
| network | `<project>_<network-key>` (external networks keep their name) |
| volume | `<project>_<volume-key>` (external volumes keep their name) |
| built image | `<project>-<service>` (unless `image:` is set) |

Each container gets tracking labels, which is how commands find project containers
(`wslc list -f label=com.wslc-compose.project=<name>`):

```
com.wslc-compose.project           project name
com.wslc-compose.service           service name
com.wslc-compose.container-number  replica index (1..N)
com.wslc-compose.config-hash       hash of the resolved service config
```

## How `up` decides what to do

For every desired container, `up` compares the stored `config-hash` label against the
hash of the freshly resolved service configuration:

| Existing container | Action |
|---|---|
| running, hash matches | *"is up-to-date"* — untouched |
| stopped, hash matches | started |
| hash differs, or `--force-recreate` | stopped, removed, recreated |
| missing | created |
| replica index above the requested scale | removed |

Changing anything in the service definition (image, env, ports, mounts, ...) or in
interpolated variables therefore triggers a clean recreation of just the affected
services on the next `up`.

## Networking

- Services without a `networks:` key join the project's `default` network
  (`<project>_default`), created on demand.
- On user-defined wslc networks, containers resolve each other by **container name**
  and by **network alias** — `wslc-compose` always adds the service name as an alias,
  so `db:5432` style URLs from your Docker compose files work unchanged. (Verified
  against the preview: alias and name DNS both resolve.)
- `external: true` networks are required to exist and are never created/removed.
- ⚠️ wslc accepts a **single `--network` per container**. If a service lists several
  networks, the first one is used (documented preview limitation).

## Volumes and path translation

- **Named volumes** are created on demand (`wslc volume create`) and removed by
  `down -v` (external ones never).
- **Bind mounts**: relative paths are resolved against the compose file's directory,
  `~` is expanded. wslc expects **Windows host paths**, so when running inside WSL,
  Linux paths are translated automatically with `wslpath -w`
  (`/mnt/e/proj/html` → `E:\proj\html`), with a manual `/mnt/<drive>/...` fallback.
  Paths already in Windows form (`E:\...`, `\\server\...`) are passed through as-is.
- Paths living **inside a distro filesystem** translate to `\\wsl.localhost\<distro>\...`;
  whether wslc can mount those depends on the preview build — prefer paths under a
  drive (`/mnt/c`, `/mnt/e`, ...).
- `tmpfs` mounts map to `--tmpfs`.

## Known wslc preview limitations

`wslc` is a public preview; `wslc-compose` warns at load time rather than failing:

- **`restart:` policies** — no wslc equivalent yet; ignored (a `restart` after reboot
  is manual: `wslc compose up -d`).
- **`healthcheck`** / `depends_on: condition: service_healthy` — not supported;
  conditions fall back to *service_started*.
- **One network per container** (see [Networking](#networking)).
- **Session mount limit**: the current preview caps mounted volumes at ~15 per WSL
  session — error `Too many volumes have been mounted (limit: 15)` / `0x8007000e`.
  This also affects `wslc build` (the build context is mounted). Microsoft says it
  will be fixed; meanwhile `wsl --shutdown` resets the session (⚠️ stops **all** WSL
  containers and distros).
- **Sessions are per Windows user *and per elevation***: an elevated terminal and a
  normal one talk to two different wslc sessions with separate containers, images,
  networks and volumes — invisible to each other, but competing for the same
  `127.0.0.1` published ports. Pick one elevation and stick to it (details and
  recovery steps in [docs/MIGRATION.md](docs/MIGRATION.md)).
- **Sessions live in the Windows service and survive `wsl --shutdown`.** A wedged
  session (every wslc command hangs) can only be cleared by restarting the service
  from an admin PowerShell: `Restart-Service WslService -Force`. If that restart
  itself hangs (service stuck in `StopPending`), see the force-kill recovery in
  [Troubleshooting](#troubleshooting).
- **Avoid `wslc system session terminate` while containers run** — in the current
  preview it can deadlock the whole wslc service (see above for the recovery).
- **Published ports bind the Windows loopback**, not the distro's: test them from
  the Windows side (browser, `powershell.exe Invoke-WebRequest`), not with a `curl
  localhost` inside WSL.
- `privileged`, `cap_add`, `devices`, `sysctls`, `secrets`, `configs`, `extra_hosts`,
  `logging` — not configurable with wslc; ignored with a warning.

When wslc gains native Compose support ([#40948](https://github.com/microsoft/WSL/issues/40948))
or a Docker Engine API endpoint ([#40976](https://github.com/microsoft/WSL/issues/40976)),
migrating away from this tool is trivial — your compose files never stopped being
standard compose files.

## Troubleshooting

**`wslc CLI not found`** — install the WSL container preview, or point
`WSLC_COMPOSE_BIN` at the binary (e.g. `/mnt/c/Program Files/WSL/wslc.exe`).

**`Too many volumes have been mounted (limit: 15)` / error `0x8007000e`** — preview
session limit (see above). Restart WSL (`wsl --shutdown` from Windows) — this stops
all running WSL containers — then `wslc compose up -d` again.

**A bind-mounted directory appears empty in the container** — the path probably
reached wslc as a Linux path. Check the exact flags with `--dry-run`; sources should
show as `E:\...` style Windows paths. Paths inside the distro filesystem
(`\\wsl.localhost\...`) may not be mountable by the preview.

**`port is already allocated` style errors** — another container (maybe from a raw
`wslc run`) publishes the same host port; `wslc list -a` shows everything, not just
this project. If `wslc list -a` shows *nothing* on that port, the culprit likely
lives in the other elevation's session (see below).

**Containers/images/networks suddenly "gone"** — you probably switched between an
elevated and a normal terminal: each elevation has its own wslc session with its own
objects. `wslc system session list` shows them; go back to the terminal elevation
that created your containers.

**Every wslc command hangs, even `wslc list`** — the session's Windows-side relay is
deadlocked (known preview issue, e.g. after a `system session terminate` with running
containers). `wsl --shutdown` does **not** fix this — sessions survive it. From an
**admin** PowerShell: `Restart-Service WslService -Force` (older builds:
`Restart-Service LxssManager -Force`); this restarts all of WSL.

**`Restart-Service WslService -Force` hangs / the service is stuck in `StopPending`** —
the `-Force` restart asks the service to stop, but a wedged utility VM (a `vmmem`
process that refuses to die) keeps it from stopping, so it sits in `StopPending`
forever and every `wsl.exe` call stays suspended. Don't wait on it — kill the old
service process directly; because `WslService` is set to *Automatic*, Windows
restarts it cleanly with a fresh PID. From an **admin** PowerShell:

```powershell
# 1) confirm the state and grab the stuck PID
Get-Service WslService | Select-Object Status          # -> StopPending
$svcpid = (Get-CimInstance Win32_Service -Filter "Name='WslService'").ProcessId

# 2) force-kill the wedged service process; it auto-restarts (Automatic start)
taskkill /F /PID $svcpid

# 3) verify it came back, then start clean
Get-Service WslService | Select-Object Status          # -> Running (new PID)
wsl.exe --shutdown                                     # clear leftover sessions
```

A leftover `vmmemwslc-*` process may survive even `wsl --shutdown` (it is a protected
Hyper-V worker, not killable with `taskkill`, even as admin). It is inert and no
longer blocks the service — only a full Windows reboot clears it. Prefer killing the
stuck service PID (above) over rebooting.

> Tip: run WSL commands over SSH from another machine, or from a Windows terminal
> that is **not** itself inside WSL — a `wsl --shutdown` (or the kill above) then
> can't cut your own session out from under you.

**A published port works in the browser but not with `curl localhost` inside WSL** —
expected: wslc publishes on the *Windows* loopback, which the distro's loopback
doesn't see. Test from the Windows side.

**`wslc-compose: command not found` inside a script run from PowerShell** —
`bash script.sh` from PowerShell starts a non-login shell without `~/.local/bin` on
PATH. Add `export PATH="$HOME/.local/bin:$PATH"` at the top of the script.

**What is it actually running?** — add `--dry-run` to any command to see every
`wslc` invocation verbatim.

## Examples

Both examples live in this repository and double as integration tests:

- [`examples/demo`](examples/demo) — the full tour: nginx serving a **bind-mounted**
  page on port 8088, an alpine worker writing to a **shared named volume**, redis
  reachable through a **network alias** (`cache`), `.env` **interpolation**,
  `depends_on` ordering:

  ```console
  cd examples/demo
  wslc compose up -d
  curl http://localhost:8088
  wslc compose exec app cat /data/log.txt     # "... (redis: up)" every 5 s
  wslc compose logs -f
  wslc compose down -v
  ```

- [`examples/build`](examples/build) — building an image from a local Dockerfile with
  `build.args`.

## Project layout

```
src/wslc_compose/
  cli.py            argument parsing + the up/down/ps/logs/... commands
  shim.py           the `wslc` wrapper command (compose → cli, rest → wslc.exe)
  loader.py         compose file discovery, parsing, normalization, validation
  interpolation.py  ${VAR...} substitution engine
  model.py          dataclasses: Project / Service / Network / Volume / mounts
  flags.py          pure functions building wslc argv from the model (unit-tested)
  engine.py         wslc binary discovery, subprocess layer, path translation,
                    container/network/volume queries
tests/              25+ unit tests (no wslc needed — pure logic)
examples/           runnable demos (see above)
install.sh          curl-able installer, bootstraps uv when pip/pipx are missing
```

## Development

```console
git clone https://github.com/bacarndiaye/wslc-compose
cd wslc-compose
pip install -e . pytest ruff      # or the uv equivalent
pytest                            # unit tests, no wslc required
ruff check src tests
wslc-compose --dry-run -f examples/demo/compose.yaml up -d   # inspect generated commands
```

CI runs the test matrix (Python 3.9 / 3.12 / 3.14) and ruff on every push and PR.

Issues and PRs are welcome — especially reports of wslc preview behavior changes,
since the CLI surface is still evolving.

## License

[MIT](LICENSE) © Bacar Ndiaye
