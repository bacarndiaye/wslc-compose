# Changelog

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
