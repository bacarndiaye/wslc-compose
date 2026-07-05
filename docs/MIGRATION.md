# Migrating an existing wslc setup to wslc-compose

You already run containers with raw `wslc run` commands (or a home-grown shell script)
and want `wslc-compose` to manage them from the `docker-compose.yml` you use elsewhere —
**without rebuilding every image or breaking what currently runs**.

This is the full process, in order. Every step is safe to re-run; nothing here touches a
container that wslc-compose already manages (they are recognized by their
`com.wslc-compose.project` label).

Everything below was battle-tested on a real multi-service dev stack (Go backend,
several Node dev servers, bind-mounted sources, named volumes, a user-defined network),
migrated live during the wslc preview. The pitfalls in step 0 and the appendix are not
theoretical — each one was hit at least once.

---

## Step 0 — Pick ONE terminal elevation, and stick to it

This is the single most important step, and the one nobody expects.

**wslc keeps a separate session per Windows user *and per elevation level*.** An
elevated ("Run as administrator") terminal talks to a session like
`wslc-cli-admin-<user>`; a normal terminal talks to `wslc-cli-<user>`. The two sessions
share **nothing**:

- containers, images, networks and volumes created in one are **invisible** in the other;
- each session has its **own image store** (a fresh session re-pulls / re-builds everything);
- ports published as `127.0.0.1:PORT` land on the same Windows loopback, so the two
  sessions can silently **conflict on ports** while `wslc list` shows nothing.

Check what you actually have before doing anything:

```console
wslc system session list
```

If containers "disappear" at any point during the migration, this is the first thing to
re-check — you are almost certainly in a terminal with a different elevation than the
one that created them.

**Recommendation: use a normal (non-elevated) terminal for everything**, today and every
day after. If your current containers were created from an elevated terminal, this
migration is the right moment to move them down: you will retag/rebuild in the normal
session (step 4) and stop the elevated leftovers (step 5).

## Step 1 — Inventory what runs today

From the terminal chosen in step 0:

```console
wslc list -a                 # containers: names, images, ports, status
wslc images                  # images you may want to reuse (avoid rebuilds)
wslc network list
wslc volume list
```

Write down, per container: name, image, published ports, mounts, network. You will need
this to map each container to a compose service and to know what to stop in step 5.

## Step 2 — Install wslc-compose and prepare the compose file

```console
curl -fsSL https://raw.githubusercontent.com/bacarndiaye/wslc-compose/main/install.sh | sh
```

If you already have a `docker-compose.yml` from Docker/Podman days, use it **unchanged**.
Otherwise write one service per container from your step 1 inventory.

Then validate without touching anything:

```console
wslc-compose -f docker-compose.yml -p myproject config      # resolved configuration
wslc-compose -f docker-compose.yml -p myproject --dry-run up -d   # exact wslc commands
```

Read the `--dry-run` output carefully:

- bind-mount sources must appear as **Windows paths** (`E:\...`) — a Linux path there
  means the mount will come up empty in the container;
- pick the `-p`/`--project-name` now and keep it forever: it prefixes every container,
  network and volume name.
- unsupported keys (`restart:`, `healthcheck:`, ...) print warnings, not errors — your
  file keeps working.

## Step 3 — Make sure the session is healthy

The preview caps bind mounts at **~15 per session** (error `0x8007000e`,
`Too many volumes have been mounted`), and `wslc build` counts too (it mounts the build
context). A migration creates many mounts at once, so start from a session with budget.
Probe it:

```console
wslc run --rm -v 'C:\Windows\Temp:/probe' alpine true && echo "session OK"
```

- Probe fails with the volume-limit error → from Windows: `wsl --shutdown`, reopen your
  terminal (same elevation!), re-probe.
- Probe **hangs** for minutes → the session is wedged; see the
  [appendix](#appendix--recovery-cheat-sheet). `wsl --shutdown` is **not** enough for
  this case.

## Step 4 — Reuse the images you already built

wslc-compose builds/pulls images named `<project>-<service>` (unless the service has an
`image:` key). If your current setup already built equivalent images under other names,
retag them so `up` skips the rebuild:

```console
# for each service with a build: section
wslc tag old-image-name:tag myproject-web
wslc tag other-old-image:dev myproject-api
```

Skip this step if you're fine rebuilding, or if you're changing session elevation (the
new session's image store starts empty — images can't be retagged across sessions, they
will be rebuilt once).

## Step 5 — Retire the old containers

The old containers hold the names and, more importantly, the **published host ports**.
Stop and remove the ones your compose file replaces (and only those):

```console
wslc stop  old_container_name
wslc remove old_container_name
```

If some old containers live in the *other* elevation's session, stop them from a
terminal with **that** elevation — otherwise the ports stay taken while being invisible
to you.

Keep anything not covered by the compose file running; wslc-compose never touches
containers without its project label.

## Step 6 — Bring the stack up

```console
cd /path/to/project
wslc-compose -f docker-compose.yml -p myproject up -d
```

First run: networks and named volumes are created, missing images are built/pulled,
containers start in `depends_on` order. From now on the same command is your idempotent
"make it so" — it only recreates services whose configuration actually changed.

## Step 7 — Verify (from Windows, not from inside WSL)

```console
wslc-compose -f docker-compose.yml -p myproject ps
wslc-compose -f docker-compose.yml -p myproject logs -f
```

⚠️ **Published ports bind the *Windows* loopback, not the WSL distro's.** A
`curl http://localhost:8080` from inside WSL will fail even when everything is fine.
Test the way a browser would, e.g. from WSL via the Windows host:

```console
powershell.exe -NoProfile -Command "Invoke-WebRequest -Uri http://localhost:8080 -UseBasicParsing | Select-Object -ExpandProperty StatusCode"
```

## Step 8 — Rollback plan

Nothing was destroyed that can't be recreated: your old run script still works. To go
back:

```console
wslc-compose -f docker-compose.yml -p myproject down    # add -v to drop named volumes too
# then re-run your previous wslc run commands / script
```

Named volumes survive `down` (without `-v`), so data written by the compose-managed
containers is still there for the old setup.

## Scripting the whole thing

For a repeatable one-command migration (or post-reboot restart), wrap steps 3–7 in a
script. Two hard-earned details for the skeleton:

```bash
#!/bin/bash
set -eu
cd "$(dirname "$0")"

# `bash script.sh` from PowerShell runs a NON-LOGIN shell: ~/.local/bin (where
# wslc-compose lives) is not on PATH there.
export PATH="$HOME/.local/bin:$PATH"

# 1. probe the session before doing anything expensive
wslc run --rm -v 'C:\Windows\Temp:/probe' alpine true \
  || { echo "session out of mount budget: run 'wsl --shutdown' and retry" >&2; exit 1; }

# 2. idempotent up
wslc-compose -f docker-compose.yml -p myproject up -d

# 3. verify from the Windows side (WSL loopback would give false negatives)
for port in 8080 3000; do
    powershell.exe -NoProfile -Command \
      "try { Invoke-WebRequest -Uri 'http://localhost:${port}' -TimeoutSec 3 -UseBasicParsing | Out-Null; exit 0 } catch { exit 1 }" \
      && echo "port ${port}: OK" || echo "port ${port}: not answering yet"
done
```

---

## Appendix — recovery cheat sheet

Field notes from the current preview. Symptoms first:

| Symptom | Cause | Fix |
|---|---|---|
| `Too many volumes have been mounted (limit: 15)` / `0x8007000e` | per-session mount budget exhausted (builds count too) | `wsl --shutdown` from Windows, reopen terminal, `up -d` again |
| **Every** wslc command hangs, even `wslc list` | the session's Windows-side service relay is deadlocked | admin PowerShell: `Restart-Service WslService -Force` (older builds: `Restart-Service LxssManager -Force`) — this kills all of WSL |
| Containers/images/networks suddenly "gone" | you switched between an elevated and a normal terminal (separate sessions) | `wslc system session list`; go back to the right elevation |
| Port taken but `wslc list -a` shows nothing using it | a container in the *other* elevation's session publishes it | stop it from a terminal with that elevation |
| `wslc-compose: command not found` inside a script launched from PowerShell | non-login shell → `~/.local/bin` not on PATH | `export PATH="$HOME/.local/bin:$PATH"` at the top of the script |
| First `up` after a service restart re-pulls/rebuilds everything | image stores are **per session**; a fresh session starts empty | expected — retagging (step 4) only helps within one session |

Things to know before you need them:

- **wslc sessions survive `wsl --shutdown`.** They live in the Windows `WslcService`,
  not in the distro. `wsl --shutdown` resets the *mount budget* but does **not** clear a
  deadlocked session — only restarting the Windows service does.
- **Never run `wslc system session terminate` while containers are running.** In the
  current preview this can deadlock the whole wslc service (every subsequent command
  hangs), and you're in the `Restart-Service` case above.
- **wslc needs a real console.** Invoked from a process without one (CI runners, IDE
  task runners, agent shells), `wslc run` can hang forever and `list`/`images` may
  return empty output with exit 0. Run migrations from an actual interactive terminal.
