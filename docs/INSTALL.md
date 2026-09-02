# Tofu — Install Guide

You only need this page if the one-command install in the README didn't
work, or if you want to pre-configure something (API key, port,
install directory, …).

For the happy path, just run the command from the README and skip this
file entirely.

---

## Pre-configuring the install (Linux / macOS)

Windows users: just run the `.exe` from the release page — no flags.
The sections below apply only to the Linux/macOS `install.sh`.

`install.sh` accepts the flags below. They write to `.env` and start
the server when finished.

Run `bash install.sh --help` to see the complete, current flag list. Help and
argument validation run before cloning, downloading, creating an environment,
or writing an install log. Unknown flags and missing/invalid values exit with
code 2 instead of being ignored. API-key values are always redacted from the
diagnostic option summary. The safest interactive path is to omit a key and add
it later in **Settings → Providers**. For unattended installs, use a
permission-restricted single-line secret file; putting a key directly in a
command exposes it to shell history and process listings.

On a rerun, an existing valid `.env` port is preserved unless `--port` is
explicitly supplied. This prevents a routine dependency refresh from silently
moving a live installation back to port `15000`.

Every runtime surface uses the same deliberately small `.env` syntax:
`NAME=value`, optional surrounding single or double quotes, and full-line
comments beginning with `#`. A `#` inside a value remains data, and explicit
process environment variables take precedence over the file. This keeps
startup, bootstrap repair, `serverctl doctor`, and support bundles from
interpreting one configuration differently.

| Flag | Purpose |
|---|---|
| `--api-key-file <path>` | Read one LLM API key from a local secret file (recommended for unattended installs) |
| `--port 8080` | Server port (default: explicit `PORT` environment value, then `15000`) |
| `--dir <path>` | Install directory (default `~/tofu`) |
| `--use-conda` | Force the legacy conda path, skipping the default uv fast path (see below) |
| `--no-launch` | Install only; don't start the server |

Example:

```bash
curl -fsSL https://raw.githubusercontent.com/rangehow/ToFu/main/install.sh \
  | bash -s -- --api-key-file ~/.config/tofu/llm-api-key --port 8080
```

`--api-key <value>` remains accepted for compatibility, but prints a warning
because the value is visible in argv/history. The installer refuses empty,
multi-line, oversized, unreadable, conflicting, or non-round-trippable key
inputs before it creates files or downloads anything. A single key cannot have
leading/trailing whitespace or quotes, or contain the comma reserved by
`LLM_API_KEYS` as its multi-key delimiter. The resulting `.env` is forced to
mode `0600` after every update.

---

## What the installer actually does

The installer is `install.sh`. It has two backends and picks the fast one
automatically:

### Fast path (default): uv

On a reasonably modern Linux host (glibc ≥ 2.28) or macOS, the installer
uses [uv](https://github.com/astral-sh/uv):

1. Ensures a `uv` binary (installs it from astral.sh if missing).
2. Creates a project-local virtualenv `.venv` from uv's own managed
   CPython (Python 3.12) — no system/conda interpreter needed.
3. `uv pip install -r requirements.txt` — installs the whole dependency
   stack from prebuilt manylinux wheels (typically ~1–2 min, **zero**
   from-source builds).
4. Runs an import smoke-test (`lxml`, `fitz`/PyMuPDF, `PIL`/Pillow,
   `cryptography`, …), then `scripts/verify_pdf_stack.py` checks the exact
   PyMuPDF trio and performs a real Markdown extraction. **If either check
   fails — e.g. an old glibc with no
   compatible wheel — it falls back cleanly to the conda path.**
5. Detects a system `ripgrep`/`fd` (optional speedups; the app degrades
   to a pure-Python search fallback if absent — never a source build).
6. Installs and verifies the Playwright Chromium binary unless explicitly
   skipped.
7. Writes `.tofu_env.json` (`backend: uv`) so `python server.py` re-execs
   into the venv even from an unactivated shell.
8. Installs the modern text tools `sd`, `goawk` and `miller` (pinned
   static binaries — musl/Go, no glibc floor) into the env's `bin/`,
   which the server prepends to `PATH` on every boot. This is a standard
   step on both install paths, not an optional extra: the run_command
   tool guidance tells the agent to prefer `sd` over `sed` for
   substitutions and `mlr` over hand-rolled `awk` for CSV/TSV/JSON
   column work. Hosts with no reachable download source keep GNU
   `sed`/`awk` as the documented fallback.

### Fallback path: conda

The installer switches to conda automatically when `--use-conda` was passed,
the host glibc is < 2.28 (PyMuPDF/Pillow ship no compatible wheel for it), or
the uv import smoke-test failed.
The conda path:

1. Locates conda, or installs a private Miniforge as a project sibling.
2. Creates a `tofu` conda env (Python 3.12).
3. Installs binary/system-sensitive dependencies from conda-forge. A small
   pip-only group is then installed into that exact env; the rich PDF trio is
   exact-pinned and installed together with dependency resolution. This avoids
   the `GLIBC_2.25 not found` trap for packages such as `lxml` while preventing
   a conda PyMuPDF + unrelated PyMuPDF4LLM split-brain.
4. Installs `ripgrep`, `fd-find`, and Chromium shared libs from
   conda-forge — no `sudo`, no system packages.
5. Installs the Playwright Chromium binary.
6. Writes `.tofu_env.json` so `python server.py` re-execs into the right
   interpreter even from an unactivated shell.
7. Installs `sd`, `goawk` and `miller` from conda-forge (falling back to
   pinned static binaries when a feedstock is unavailable) — the same
   standard text-tool set as the fast path's step 8.

Both paths then configure `.env`, start the server through the project-local
lifecycle manager, and wait for readiness. The installer runs the live runtime
probe in the foreground and then returns; the server stays managed in the
background. There is no terminal process to keep open and no need to press
Ctrl+C. The completion message prints the exact URL plus status, stop, and
restart commands. Unless `--skip-playwright` was selected, the install also
requires Chromium to launch and render text. Any required runtime validation
failure leaves its evidence and the managed process available for diagnosis,
but makes the installer exit non-zero instead of reporting success.

Stopping or restarting a live worker is intentionally different from read-only
status/diagnostic commands: an interactive terminal asks for confirmation, and
a non-interactive caller must consume a one-time `shutdown`/`restart` approval
made by a human in the Tofu UI. If no worker is live, `stop` remains idempotent
and needs no approval.

To audit or repair PDF parsing after an environment change, run
`python scripts/verify_pdf_stack.py`. A healthy result reports all three pinned
versions and `Markdown smoke passed`; the command exits non-zero otherwise.

## Listener and TLS guardrails

Plain HTTP is the proxy-safe default. Direct HTTPS is an explicit choice with
`TOFU_TLS=1`, or with a complete `TLS_CERTFILE` / `TLS_KEYFILE` pair. Boolean
values accept `0/1`, `false/true`, `no/yes`, `off/on`, and
`disabled/enabled`. An invalid value, half-configured pair, missing configured
file, or failed certificate generation stops startup with a concrete error;
Tofu never silently downgrades an explicit TLS request to HTTP.

## Optional Linux system-supervisord ownership

The standalone installer normally uses the project-local lifecycle manager;
that is the zero-configuration path on Linux and macOS. A Linux host that
already standardizes on system supervisord may hand the same personal worker
to it with:

```bash
# Inspect the exact target-host config without changing supervisord.
bash deploy/supervisor/install.sh --dry-run

# Install it. The script uses root/passwordless sudo for the system config,
# while the Tofu worker itself remains under the checkout's non-root owner.
bash deploy/supervisor/install.sh
```

The repository stores only `deploy/supervisor/tofu.conf.template`; it never
stores a developer's checkout, interpreter, username, home directory, or log
path. The installer resolves those values from its own location and
`.tofu_env.json`, supports both `/etc/supervisor/conf.d` and
`/etc/supervisord.d` (or an explicit `--config-dir`), renders atomically, and
requires a live runtime health check before reporting success. A stale
environment marker after moving a checkout fails with an instruction to rerun
`install.sh`; it is never silently replaced with another Python. Spaces,
Unicode and ordinary punctuation in target paths are encoded for supervisord;
any value that cannot be round-tripped safely is rejected before system state
changes.

If the port already belongs to this checkout's project-local worker, the handoff
goes through `serverctl.py stop` and therefore retains the human approval gate.
An unidentifiable listener is not killed. A parse, activation, startup, or
health failure restores the previous supervisord config and, after a manual
worker handoff, attempts to restore project-local ownership. Use `--no-handoff`
when an operator wants any occupied port to be a hard preflight failure.

## Database deployment modes

The standalone installer and Docker Compose are the personal topology:
`TOFU_DEPLOYMENT_MODE=personal`, `TOFU_PROCESS_ROLE=all`, and one SQLite
authority at `data/tofu.db`. They never install or start a PostgreSQL server.

The distributed topology is delivered through Kubernetes. Each API, worker,
or scheduler Pod runs a local Storage Sidecar that connects to the same
platform-managed PostgreSQL service. Redis supplies only cross-replica leases,
limits, presence hints, and wakeups; PostgreSQL remains the task/event
authority. Both credentials arrive through absolute secret-file mounts and
must use verified TLS. See [the storage contract](STORAGE.md).

The supported chart is `deploy/helm/tofu`. It never renders credential values
or a PostgreSQL/Redis server and refuses tag-only images. Create the referenced
Secret from already provisioned files, then supply the two immutable release
digests:

```bash
kubectl create namespace tofu-system --dry-run=client -o yaml | kubectl apply -f -
kubectl -n tofu-system create secret generic tofu-external-services \
  --from-file=postgres-dsn=/secure/path/postgres-dsn \
  --from-file=redis-url=/secure/path/redis-url
helm upgrade --install tofu deploy/helm/tofu \
  --namespace tofu-system \
  --set-string images.api.digest=sha256:<api-digest> \
  --set-string images.worker.digest=sha256:<worker-digest>
```

The pre-install/pre-upgrade migration Job must finish before Deployments are
eligible. The default release has one API replica, one worker, and one
scheduler; API and worker autoscaling stay disabled until the durable execution
composition root and claim/heartbeat/fencing fault-injection gates pass. The
chart currently validates distributed wiring, not horizontal execution
support. Every Pod uses a memory-backed private connection handoff between the
application and its Storage Sidecar. See the
[distributed rollout runbook](EPIC_D_SCALE_ROLLOUT_RUNBOOK.md) before opening
PostgreSQL writes.

An old `data/pgdata/` directory is never deleted or reused by current startup.
The installer reports it and leaves it untouched. Export it with the matching
legacy PostgreSQL major, verify the export, then use the controlled external
PostgreSQL migration workflow; do not point current application Pods at the
directory.

---

## Troubleshooting

### Start here: one diagnostic ladder

For a bug report or an assistant, one command collects the whole bounded,
credential-redacted bundle even when the server is offline:

```bash
python serverctl.py support-bundle \
  --output "tofu-support-$(date +%Y%m%d-%H%M%S).json"
```

It includes the lifecycle report and recent installer, manager, worker,
application, error, storage, PostgreSQL, resource-pressure, watchdog, and
faulthandler log tails when those files exist. It does not open conversation
storage or the database and never contacts external services; its lifecycle
diagnostics may probe the manager and health endpoint over loopback. Log lines
can still quote user-provided text or unrecognized secret formats, and absolute
paths/platform metadata may identify a host or user. Credential redaction is
best-effort, so you must review the JSON before sharing it. Use
`--no-logs` for a metadata-only artifact. The file is created with mode `0600`
and an existing path is never overwritten.

For interactive troubleshooting, run these in order. All four are read-only:

```bash
python serverctl.py status --json
python serverctl.py doctor --json
python serverctl.py logs -n 200
python healthcheck.py --runtime
```

`status` answers who owns the process and whether it is ready. In JSON,
`ready` means manager-aligned readiness, while `applicationReachable` says
whether the identity-checked application actually answers. If the latter is
true while `ready` is false, `portDrift` and `applicationUrl` identify the
usable endpoint without hiding the lifecycle fault. `doctor` checks manager,
lock, port, memory, recovery, and backup state and emits copyable fixes;
`lifecycleHealthy` is its unambiguous blocking-error result.
`logs` supplies the recent causal evidence. The runtime healthcheck verifies the
HTTP endpoint, storage, index page, credentials, and browser engine. This is the
manual expansion of the support bundle; do not hand-query the database or guess
which process to kill.

When a report includes a conversation ID copied from the sidebar, inspect that
one conversation through the same operations entry point:

```bash
python serverctl.py inspect-conversation mt18xr3wfs0rbq
```

The command is read-only, auto-detects sidecar versus legacy storage, renders
turn-native conversations through the server's canonical projection, and shows
only matching bounded log evidence. Settings and log lines use the same
best-effort credential redaction as durable diagnostics. Its transcript remains
the actual conversation and may contain private text or credentials pasted by
the user, so review it before sharing; add `--no-logs` when log evidence is
unnecessary.

If startup itself failed, the installer prints these commands automatically.
Use `python serverctl.py start` to retry after correcting the reported cause.

### "GLIBC_2.25 not found" or "GLIBC_2.28 not found" at startup

A pip-installed wheel of `lxml` (or similar) is shadowing the conda-forge
build. Fix it by re-running the installer:

```bash
curl -fsSL https://raw.githubusercontent.com/rangehow/ToFu/main/install.sh \
  | bash -s -- --reset-env
```

`--reset-env` rebuilds the selected environment from scratch. On the default uv
path it deletes `.venv` only when an installer ownership marker or the matching
`.tofu_env.json` proves the target belongs to this checkout; ambiguity fails
closed. On the conda path it removes the explicitly named conda environment.
**Destructive** — only use it for a Tofu-owned environment.

### Conda solver hangs forever

Almost always an outdated `conda` you installed yourself. Easiest fix:
let the installer drop a private Miniforge next to the project, and use
that instead of touching yours:

```bash
curl -fsSL https://raw.githubusercontent.com/rangehow/ToFu/main/install.sh \
  | bash -s -- --force-sibling-conda
```

Conda-specific options (`--env`, `--min-conda`, `--no-update-conda`,
`--force-sibling-conda`, and `--with-docling`) select the conda backend
explicitly; they are never silently ignored by the default uv path.

### A legacy `data/pgdata/` directory is reported

This is preservation, not a startup error. Current Tofu does not own database
server processes or local PostgreSQL data directories. Keep the directory
read-only until a matching legacy PostgreSQL toolchain has produced and
verified an export. The personal service continues on SQLite; a distributed
cutover imports the verified export into an external PostgreSQL authority.

### Behind a corporate proxy that blocks GitHub releases

The Miniforge download will fail. Workarounds:

- Pre-download `Miniforge3-<platform>.sh` manually and set
  `TOFU_MINIFORGE_LOCAL=/path/to/Miniforge3-...sh` before running the
  installer.
- Or set `TOFU_MINIFORGE_MIRRORS="https://your-mirror/..."` (one URL per
  line) — the installer tries each in order before giving up.

### Install log

Every `install.sh` run writes a full transcript (ANSI colours stripped) with
mode `0600`. Once the source checkout exists, its normal location is
`<install-dir>/logs/install-YYYYMMDD_HHMMSS-PID.log`. An error before the source
is cloned may leave the private staging log beside the requested install
directory; the installer always prints the exact path to copy when filing an
issue.

---

## Deployment and storage configuration

| Env var | Effect |
|---|---|
| `TOFU_DEPLOYMENT_MODE=personal|distributed` | Selects the topology; absence means `personal` |
| `TOFU_DISTRIBUTED_PREVIEW_MODE=read-only` | Mandatory temporary distributed latch; rejects HTTP/WebSocket/Sidecar writes and background execution |
| `TOFU_PROCESS_ROLE=all|api|worker|scheduler` | Declares lifecycle ownership; personal mode requires `all` |
| `TOFU_POSTGRES_DSN_FILE` | Absolute PostgreSQL secret file; required only in distributed mode |
| `TOFU_REDIS_URL_FILE` | Absolute `rediss://` secret file; required only in distributed mode |
| `TOFU_REPLICA_ID` | Stable, unique replica identity; required only in distributed mode |
| `TOFU_STORAGE_SQLITE_READ_POOL` | SQLite query-only pool size (personal probe: 2..12) |
| `TOFU_STORAGE_RPC_CAPACITY` | Active Sidecar RPC ceiling (personal probe: 2..12; distributed: 64) |
| `TOFU_STORAGE_IDLE_TRIM_RSS_MIB` | Sidecar RSS threshold for returning free arenas when the last active RPC exits (personal: 128..512 MiB from the writer-cache budget; distributed: 1,024 MiB; hard ceiling: 16 GiB) |
| `TOFU_STORAGE_IDLE_TRIM_COOLDOWN_SECONDS` | Minimum interval between Sidecar idle heap trims (default 300 s; bounded to 30..3,600 s) |
| `TOFU_MCP_STDIO_IDLE_SECONDS` | Idle window before a local MCP stdio process tree exits while its cached tool catalog remains available (personal probe: 180..600 s; distributed: 1,800 s; `0` disables; hard ceiling: 86,400 s) |
| `TOFU_NUMERIC_THREADS` | Process-wide ceiling for implicit OpenBLAS/OpenMP/MKL/NumExpr pools (personal probe: 1..4; distributed: 4; hard ceiling: 32); smaller library-specific values remain valid, larger inherited host values are clamped |
| `TOFU_EXECUTOR_IDLE_SECONDS` | Quiet window before burst-grown serving-loop worker generations retire (personal probe: 300..1,800 s; distributed: 3,600 s; `0` disables; hard ceiling: 86,400 s); capacity is preserved and recreated lazily |
| `TOFU_PROJECT_REFRESH_IDLE_SECONDS` | Idle window before reconstructible project summary/status/watch consumers exit (personal probe: 30..300 s; distributed: 600 s; `0` keeps consumers resident; hard ceiling: 86,400 s); queue capacity and coalescing are unchanged |
| `TOFU_TREE_INDEX_WALK_JOBS` | Process-wide project tree scan-worker ceiling shared by concurrent roots (personal probe: 2..8; distributed/hard ceiling: 16) |
| `TOFU_TREE_INDEX_MAX_ENTRIES` | Both the per-build admission ceiling and process-wide retained-path ceiling (personal probe: 50,000..600,000; 8 GiB reference: 409,600; probe-failure fallback: 100,000; distributed/hard ceiling: 600,000) |
| `TOFU_TREE_INDEX_MEM_ROOTS` | Secondary in-memory project-root ceiling; the shared entry ceiling may evict sooner (personal probe: 2..4; distributed: 4; hard ceiling: 8) |
| `TOFU_MAX_SSE_PER_PRINCIPAL` | Shared live SSE socket/lease ceiling across chat and conversation streams (personal probe: 8..24; 8 GiB reference/probe fallback: 12; distributed: 64; hard ceiling: 128); zero/malformed overrides do not disable the bound |
| `TOFU_SSE_SLOT_TTL` | Crash-reclaim lease window for admitted SSE streams (default 300 s; clamped to 45..3,600 s and refreshed at most every one third of the window) |
| `TOFU_TASK_MAX_API_ROUNDS` | Inherited model API-round ceiling per root task (personal: 192; distributed: 512; request overrides remain hard-capped at 1,024) |
| `TOFU_STORAGE_PG_READ_POOL` / `TOFU_STORAGE_PG_WRITE_POOL` | PostgreSQL pool requests (32/16, automatically capped to the 80% server budget) |
| `TOFU_STORAGE_MIN_FREE_BYTES` | Data-filesystem startup reserve (personal probe: 1% of volume, clamped to 256 MiB..2 GiB) |
| `TOFU_STORAGE_RECOVERY_COPY_BUDGET_MIB` | Total admitted SQLite recovery-copy footprint on one volume (personal: 50% of launch-probed data volume, clamped to 4..512 GiB; probe-failure fallback: 64 GiB; distributed: 1 TiB; explicit hard ceiling: 8 TiB) |
| `TOFU_RUN_PYTHON_CACHE=auto|1|0` / `TOFU_RUN_PYTHON_CACHE_MAX_MIB` | Auto-select repeat-heavy network-workspace Python bytecode caching, force it for experiments, or disable it; set its bounded local ceiling (personal probe: 16..64 MiB) |
| `TOFU_SERVER_PYTHON_CACHE=auto|1|0` / `TOFU_SERVER_PYTHON_CACHE_MAX_MIB` / `TOFU_SERVER_PYTHON_CACHE_DIR` | Let the stdlib lifecycle manager select, force, disable, size, or relocate its project/interpreter-scoped host-local server bytecode cache (personal probe: 16..64 MiB; distributed: 128 MiB; hard ceiling: 512 MiB); an existing `PYTHONPYCACHEPREFIX` or `PYTHONDONTWRITEBYTECODE` wins |
| `TOFU_TOKEN_COUNT_CACHE_CAPACITY` | Digest-only large-text token-count entries (personal probe: 64..512 in normal profiles; distributed: 1,024; hard ceiling: 4,096) |
| `TOFU_STORAGE_PREFLIGHT_MAX_MS` | Maximum accepted filesystem preflight latency |

Removed variables `TOFU_DB_BACKEND`, `TOFU_REQUIRE_PG`, and
`TOFU_REPLICA_RING` fail startup instead of selecting a compatibility path.

---

## Docker

```bash
git clone https://github.com/rangehow/ToFu.git && cd ToFu
docker compose up -d
docker compose ps
```

Wait until `docker compose ps` reports the `tofu` service healthy, then open
<http://localhost:15000>. If it does not become healthy, run
`docker compose logs --tail=200 tofu` for the startup cause.

Compose publishes port 15000 on host loopback (`127.0.0.1`) by default. Docker
bridge NAT makes a request from the host browser look non-loopback inside the
container, so the Compose profile explicitly admits those bridge peers in
personal `open` mode while the host publication remains local-only. Do not
change the published address to `0.0.0.0` in open mode. For LAN, reverse-proxy,
or public access, first put `TOFU_AUTH_MODE=private` in `.env`, start once, and
explicitly recover the one-shot URL (the credential is intentionally redacted
from general logs):

```bash
docker compose exec tofu python serverctl.py login-url
```

Open that URL once to install the browser cookie; only then deliberately change
the host-side port binding. `login-url` reads the mode and token from their
canonical stores, rejects broad token-file permissions and stale/revoked
tokens, and shows the credential only because the operator explicitly asked.

All data persists in named Docker volumes. No flags are needed. For production
SQLite recovery snapshots, put an absolute host/NAS path in the mount-source
variable (the directory must already be writable by Docker):

```bash
TOFU_BACKUP_VOLUME=/mnt/remote-backup/tofu docker compose up -d
```

Compose mounts that source at `/app/data/backups` and configures automatic
SQLite snapshots to use it. The default `tofu-backups` volume is convenient
but normally remains on the same host and is not disaster recovery.

The checked-in Compose service builds the current checkout; it does not use a
published image by default. Upgrade it with:

```bash
git pull --ff-only
docker compose up -d --build
docker compose ps
```

If the fast-forward pull refuses local changes, resolve or preserve those
changes explicitly; do not treat `docker compose pull` as a successful source
upgrade.

The Dockerfile has two non-root final targets. `api` excludes the browser and
database-server toolchains. `worker` adds Playwright/Chromium; Compose targets
it because the personal `all` process combines serving and worker features.
