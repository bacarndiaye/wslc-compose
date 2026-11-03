#Requires -Version 5.1
<#
.SYNOPSIS
    wslc-compose installer — gives you `wslc compose ...` (docker/podman style)
    and `wslc-compose ...`, even on systems without pip/venv/pipx.

.DESCRIPTION
    Run directly from the web:

        irm https://raw.githubusercontent.com/bacarndiaye/wslc-compose/main/install.ps1 | iex

    Environment:
        WSLC_COMPOSE_SOURCE  package to install (default: the GitHub repo;
                             set it to a local checkout path to install from source)
#>

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Source = if ($env:WSLC_COMPOSE_SOURCE) {
    $env:WSLC_COMPOSE_SOURCE
} else {
    'git+https://github.com/bacarndiaye/wslc-compose'
}

function Write-Say {
    param([string]$Message)
    Write-Host "==> " -ForegroundColor Blue -NoNewline
    Write-Host $Message
}

function Write-Fail {
    param([string]$Message)
    Write-Host "error: " -ForegroundColor Red -NoNewline
    Write-Host $Message
    exit 1
}

function Test-Command {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Install-WithPipx {
    Write-Say "Installing with pipx"
    & pipx install --force $Source
}

function Install-WithUv {
    param([string]$Uv)
    Write-Say "Installing with uv"
    & $Uv tool install --force --from $Source wslc-compose
}

function Initialize-Uv {
    # The resolved uv path is returned on the pipeline, so status messages
    # must go to the information/host stream only.
    $dataHome = Join-Path $env:LOCALAPPDATA "wslc-compose"
    $uvDir = Join-Path $dataHome 'uv'
    $uvExe = Join-Path $uvDir 'uv.exe'

    if (-not (Test-Path -LiteralPath $uvExe)) {
        Write-Say "Bootstrapping uv (standalone, no pip required) into $uvDir"
        New-Item -ItemType Directory -Force -Path $uvDir | Out-Null

        $env:UV_INSTALL_DIR = $uvDir
        $env:UV_NO_MODIFY_PATH = '1'
        try {
            Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
        } finally {
            Remove-Item Env:\UV_INSTALL_DIR -ErrorAction SilentlyContinue
            Remove-Item Env:\UV_NO_MODIFY_PATH -ErrorAction SilentlyContinue
        }
    }

    return $uvExe
}

if (Test-Command 'pipx') {
    Install-WithPipx
} elseif (Test-Command 'uv') {
    Install-WithUv 'uv'
} else {
    $uv = Initialize-Uv
    Install-WithUv $uv
}

# uv/pipx install executables into a user-local bin dir. Warn if it isn't on PATH.
$binDir = if ($env:XDG_BIN_HOME) {
    $env:XDG_BIN_HOME
} else {
    Join-Path $env:USERPROFILE '.local\bin'
}

$pathEntries = ($env:PATH -split [IO.Path]::PathSeparator) | ForEach-Object { $_.TrimEnd('\') }
if ($pathEntries -notcontains $binDir.TrimEnd('\')) {
    Write-Say "Note: add $binDir to your PATH, e.g.:"
    Write-Host "    [Environment]::SetEnvironmentVariable('Path', `"`$env:Path;$binDir`", 'User')"
}

Write-Say "Done. Try it:"
Write-Host "    wslc compose --help"
Write-Host "    wslc-compose version"
