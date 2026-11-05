# Changelog

## 0.5.0 — 2026-11-05

### Added
- `depends_on: required: false` (#31): optional dependencies are not pulled in by selection
  and a failing condition only warns. A non-boolean `required` is rejected.
- wslc ≥ 3.0.5 options (#34, validated on 3.0.5.0): `restart: always` → `--restart always`,
  `cap_add` / `cap_drop` → `--cap-add` / `--cap-drop`. Detected from `wslc run --help`;
  older wslc keeps the previous behaviour with a clear warning. Changing them recreates the
  container (reconciled with `inspect`, the shared config hash is unchanged).
- `examples/host-gateway`: the `host.docker.internal:host-gateway` rules were validated end to
  end on a real WSLC session (#28).

### Changed
- `restart: unless-stopped` / `on-failure[:N]` warn that wslc only supports `always` and `no`.
- `install.sh` / `install.ps1` install the PyPI release by default (0.3.3).
- A silent failure of the host-gateway session script now reports its cause and exit code.

### Documentation
- Debian/Ubuntu install (PEP 668, `externally-managed-environment`); `pip install --user`
  removed from the install instructions (#26).
- Install commands pin the latest release; the landing page shows the live version and
  lists the new features.

### CI / tooling
- Test suite also runs on Windows (#35); `install.sh` is tested on a stock Debian (#26).
- `scripts/bump_version.py` + a test keep the version pinned in the docs in sync (#27).

## 0.4.0 — 2026-11-05

### Added
- `depends_on: condition: service_healthy` and `service_completed_successfully` are now
  honored by `up`: dependents are created only once the dependency is healthy / has exited
  with code 0. Fails fast when the dependency is unhealthy, exited, has no healthcheck or
  exited non-zero. New `up --wait-timeout SEC` option.

### Changed
- An unknown `depends_on` condition is now an error instead of a warning.
- `depends_on` on an undefined service is rejected when a condition is given.

### Documentation
- Landing page: new feature cards (healthchecks, multiple files/include, secrets/configs), healthcheck row corrected, version badge.

## 0.3.3 — 2026-11-05

### Changed
- `install.sh` / `install.ps1` now install the latest 0.x release from PyPI by default
  instead of the Git `main` branch. Set `WSLC_COMPOSE_SOURCE=git+https://github.com/bacarndiaye/wslc-compose`
  to get unreleased changes.

## 0.3.2 — 2026-11-05

### Documentation
- PyPI is now the primary installation method: `pipx install "wslc-compose==0.*"` leads the
  README, the landing page and the migration guide, with upgrade commands and the one-line
  installer documented as a fallback (#20).
- README pin example updated to `==0.3.1` (#19); the PyPI project page now shows the
  refreshed README.

## 0.3.1 — 2026-11-03

### Fixed
- `healthcheck` is supported (mapped to wslc `--health-*` options): the misleading
  "healthchecks are not supported … ignoring" warning is gone and the README is corrected.
  Only `depends_on: condition: service_healthy` is still treated as `service_started` (#17).

### Documentation
- Installation from PyPI (`pip install "wslc-compose==0.*"`) in the README, landing page and
  migration guide (#16).

### CI
- Releases are published to PyPI automatically through Trusted Publishing (#15).

## 0.3.0 — 2026-11-03

### Added
- Multiple compose files: repeat `-f`, join paths with `:` (`;` on Windows) or use
  `COMPOSE_FILE`; files are overlaid like `docker compose` (#13, fixes #2).
- Compose `include:` with per-file relative path resolution, nested includes and
  conflict/cycle errors (#10, rework of #7 by @kant2002).
- A file may omit `services` as long as the merged result has one (#13, fixes #3).
- Multi-network orchestration with IPv4 IPAM, static addresses and aliases per endpoint,
  health checks, structured mounts, GPU/stdin/TTY/stop-grace-period mapping, canonical
  `v2:` configuration fingerprint and observed-state reconciliation, and
  `extra_hosts: host-gateway` access to published ports (#9 by @froilson).
- Windows PowerShell installer `install.ps1` (#6 by @kant2002).
- File-backed `secrets`/`configs` validation and documentation (#11, based on #1 by @yrod4adesso).

### Fixed
- WSLc JSON/NDJSON list output, localized Windows tables, UTF-8 output, and `wslc` lookup that
  skips our own wrapper (#5 by @froilson, fixes #4).
- Undefined secret/config references no longer crash with a `KeyError`; a secret and a config
  sharing a key no longer mount each other's file; `build.secrets` is rejected explicitly (#11).
- CI lint is green again with an explicit ruff rule set (#12).
