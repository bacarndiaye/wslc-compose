#!/bin/sh
# wslc-compose installer — gives you `wslc compose ...` (docker/podman style)
# and `wslc-compose ...`, even on distros without pip/venv/pipx.
#
#   curl -fsSL https://raw.githubusercontent.com/bacarndiaye/wslc-compose/main/install.sh | sh
#
# Environment:
#   WSLC_COMPOSE_SOURCE  package to install (default: the GitHub repo;
#                        set it to a local checkout path to install from source)
set -eu

SOURCE="${WSLC_COMPOSE_SOURCE:-git+https://github.com/bacarndiaye/wslc-compose}"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

install_with_pipx() {
    say "Installing with pipx"
    pipx install --force "$SOURCE"
}

install_with_uv() {
    say "Installing with uv"
    "$1" tool install --force --from "$SOURCE" wslc-compose
}

bootstrap_uv() {
    # stdout is captured by the caller: status messages must go to stderr
    UV_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/wslc-compose/uv"
    if [ ! -x "$UV_DIR/uv" ]; then
        say "Bootstrapping uv (standalone, no pip required) into $UV_DIR" >&2
        mkdir -p "$UV_DIR"
        curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$UV_DIR" UV_NO_MODIFY_PATH=1 sh >&2
    fi
    echo "$UV_DIR/uv"
}

command -v curl >/dev/null 2>&1 || fail "curl is required"

if command -v pipx >/dev/null 2>&1; then
    install_with_pipx
elif command -v uv >/dev/null 2>&1; then
    install_with_uv uv
else
    install_with_uv "$(bootstrap_uv)"
fi

BIN_DIR="$HOME/.local/bin"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        say "Note: add $BIN_DIR to your PATH, e.g.:"
        printf '    echo '\''export PATH="$HOME/.local/bin:$PATH"'\'' >> ~/.%src\n' "$(basename "${SHELL:-bash}")"
        ;;
esac

say "Done. Try it:"
printf '    wslc compose --help\n    wslc-compose version\n'
