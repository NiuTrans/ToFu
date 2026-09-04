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
| `--dir <path>` | Install directory (default `~/tofu`) |
| `--env <name>` | Conda environment name; selects conda (default `tofu`) |
| `--port <n>` | Server port, 1-65535 (default: explicit `PORT` environment value, then `15000`) |
| `--api-key-file <path>` | Read one LLM API key from a local secret file (recommended for unattended installs) |
| `--api-key <key>` | Legacy compatibility only; the value is visible in argv/history |
| `--no-launch` | Install only; don't start or probe the server |
| `--skip-playwright` | Skip the optional browser-engine download |
| `--skip-node` | Legacy no-op (frontend bundles are prebuilt) |
| `--no-update-conda` | Select conda; don't update installer-owned conda |
| `--reset-env` | Recreate the selected env (destructive; ownership-gated) |
| `--use-conda` | Force the legacy conda path, skipping the default uv fast path |
| `--min-conda <n>` | Minimum existing conda major version (default `24`) |
| `--force-sibling-conda` | Select conda and use a private sibling Miniforge |
| `--with-docling` | Select conda and install PDF parsing (~2 GB) |
| `--python <version>` | Python version (default `3.12`) |

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

## Offline / mirror / proxy

`install.sh` honours five optional environment variables for hosts that cannot
reach the public download sources directly. Leaving them unset keeps the
default public behaviour.

| Variable | Effect |
|---|---|
| `TOFU_PYPI_INDEX` | Base URL override for pip/uv package installs. Exported as `PIP_INDEX_URL`, `UV_INDEX_URL`, and `UV_DEFAULT_INDEX`, with the host added to `PIP_TRUSTED_HOST` / `UV_INSECURE_HOST` (so plain-HTTP corp mirrors work). Applied once before the uv/conda fork, so both backends inherit it. Empty = public PyPI. |
| `TOFU_PLAYWRIGHT_MIRROR` | Base URL for the Playwright browser download, exported as `PLAYWRIGHT_DOWNLOAD_HOST`. Empty = upstream `cdn.playwright.dev`. |
| `TOFU_CONDA_MIRROR` | Base URL for conda-forge; the installer expects `<base>/conda-forge/<arch>/repodata.json` to resolve. Applied only when the installer owns its sibling conda, writing `CONDA_BASE/.condarc` (never the user's `~/.condarc`). Empty = no override. |
| `TOFU_MINIFORGE_LOCAL` | Absolute path to a pre-downloaded `Miniforge3-<platform>-<arch>.sh`. When set and readable, the installer runs it directly and skips all Miniforge download/mirror logic — the offline/air-gapped escape hatch. |
| `TOFU_MINIFORGE_MIRRORS` | Whitespace-separated Miniforge installer URLs, tried in order before the built-in fallback chain. |

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

The command is read-only, resolves the active Sidecar/fastpath SQLite authority,
renders turn-native conversations and compaction receipts through the server's
canonical operations, and shows only matching bounded log evidence. Settings
and log lines use the same
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

See [Offline / mirror / proxy](#offline--mirror--proxy) for the full set of
mirror variables. In particular, pre-download the Miniforge installer and set
`TOFU_MINIFORGE_LOCAL=/path/to/Miniforge3-<platform>-<arch>.sh`, or set
`TOFU_MINIFORGE_MIRRORS` to an ordered list of reachable installer URLs.

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
| `TOFU_STORAGE_SQLITE_WRITER_QUEUE_CAPACITY` | Total waiting-job ceiling for the sole SQLite writer (personal probe: 8..64; 8 GiB reference: 16; probe-failure fallback: 8; distributed: 128; hard ceiling: 1,024). Saturation returns retryable `database_busy`; acquisition timeout immediately releases a still-queued operation payload. |
| `TOFU_STORAGE_EVENT_QUEUE_CAPACITY` / `TOFU_STORAGE_EVENT_QUEUE_MAX_MIB` / `TOFU_STORAGE_EVENT_BATCH_MAX_MIB` | Durable task-event waiting objects, their serialized-byte envelope, and one Sidecar RPC frame budget. Defaults derive from the writer queue: probe-failure 256 / 64 MiB, 8 GiB reference 512 / 64 MiB, distributed 4,096 / 512 MiB; batches retain at most 500 events and default to 60 MiB. Hard override ceilings are 8,192 objects / 1,024 MiB waiting / 60 MiB per batch. |
| `TOFU_STORAGE_RPC_CAPACITY` | Active Sidecar RPC ceiling (personal probe: 2..12; distributed: 64) |
| `TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB` | Independent process-wide weighted budgets for serialized storage frame bodies in the Sidecar and in every application/worker client (personal probe: 128..512 MiB; 8 GiB reference and probe-failure fallback: 128 MiB; distributed: 1,024 MiB; hard range: 128..8,192 MiB). The Sidecar reserves declared request bytes before allocation and one maximum response before encoding; a client reserves each declared response until JSON decode completes. Completed/command responses use the priority FIFO. Waits are bounded to five seconds; client pressure retries only replay-safe reads, never a command whose execution may have started. Decoded semantic objects remain governed by RPC/operation limits rather than this frame-body budget. The value is per process, not one host-wide aggregate. |
| `TOFU_WEBHOOK_SUBSCRIPTION_CAPACITY` / `TOFU_WEBHOOK_QUEUE_CAPACITY` / `TOFU_WEBHOOK_BUFFER_MAX_MIB` / `TOFU_WEBHOOK_EVENT_MAX_KIB` / `TOFU_WEBHOOK_MAX_ATTEMPTS` | Outbound-webhook durable subscription, immediate/retry residency, aggregate bytes, per-event bytes and actual-attempt ceilings. Personal fallback/reference defaults: 64 subscriptions, 128 immediate + 64 retry items, 16 MiB aggregate, 512 KiB/event, 5 attempts; distributed: 2,048 / 2,048 + 1,024 / 256 MiB / 1 MiB / 5. Hard ceilings: 4,096 subscriptions/items, 512 MiB, 4 MiB/event, 8 attempts. |
| `TOFU_PUSH_CLIENT_CAPACITY` / `TOFU_PUSH_OWNER_CLIENT_CAPACITY` / `TOFU_PUSH_EVENT_QUEUE_CAPACITY` / `TOFU_PUSH_EVENT_QUEUE_MAX_MIB` / `TOFU_PUSH_EVENT_MAX_MIB` | Unified Push-WebSocket process/owner connections and each client's lossy event item/byte/single-frame envelope. Probe-failure fallback: 32 / 12 / 512 / 4 MiB / 2 MiB; 8 GiB reference: 64 / 12 / 1,000 / 4 MiB / 2 MiB; distributed: 256 / 64 / 1,000 / 16 MiB / 8 MiB. Hard ceilings are 256 / 128 / 4,096 / 16 MiB / 8 MiB. Capacity rejection is retryable; sustained queue loss or one oversized/unencodable frame disconnects the socket so durable reconciliation runs. |
| `TOFU_STORAGE_IDLE_TRIM_RSS_MIB` | Sidecar RSS threshold for returning free arenas when the last active RPC exits (personal: 128..384 MiB from the writer-cache budget; distributed: 1,024 MiB; hard ceiling: 16 GiB) |
| `TOFU_STORAGE_IDLE_TRIM_COOLDOWN_SECONDS` | Minimum interval between Sidecar idle heap trims (personal default: 60 s; distributed: 300 s; bounded to 30..3,600 s). Trims still require zero active RPCs and RSS above `TOFU_STORAGE_IDLE_TRIM_RSS_MIB` |
| `TOFU_MCP_STDIO_IDLE_SECONDS` | Idle window before a local MCP stdio process tree exits while its cached tool catalog remains available (personal probe: 180..600 s; distributed: 1,800 s; `0` disables; hard ceiling: 86,400 s) |
| `TOFU_MCP_CRED_PROBE_WORKERS` / `TOFU_MCP_CRED_PROBE_TIMEOUT_SECONDS` | Process slots and read deadline for unattended, reconstructible MCP credential-health probes (personal probe: 1..4 slots; 8 GiB reference: 2; probe-failure fallback: 1; distributed: 8; hard slot ceiling: 16; timeout default: 30 s, hard range: 1..300 s). A saturated probe is deferred to maintenance; ordinary user-owned MCP calls remain unlimited unless their server declares `timeout` |
| `TOFU_NUMERIC_THREADS` | Process-wide ceiling for implicit OpenBLAS/OpenMP/MKL/NumExpr pools (personal probe: 1..4; distributed: 4; hard ceiling: 32); smaller library-specific values remain valid, larger inherited host values are clamped |
| `TOFU_EXECUTOR_IDLE_SECONDS` | Quiet window before burst-grown serving-loop worker generations retire (personal probe: 300..1,800 s; distributed: 3,600 s; `0` disables; hard ceiling: 86,400 s); capacity is preserved and recreated lazily |
| `TOFU_PROJECT_REFRESH_IDLE_SECONDS` | Idle window before reconstructible project summary/status/watch and provider-diagnostic consumers exit (personal probe: 30..300 s; distributed: 600 s; `0` keeps consumers resident; provider diagnostics clamp nonzero values to 15..3,600 s); queue capacity and coalescing are unchanged |
| `TOFU_TREE_INDEX_WALK_JOBS` | Process-wide project tree scan-worker ceiling shared by concurrent roots (personal probe: 2..8; distributed/hard ceiling: 16) |
| `TOFU_TREE_INDEX_MAX_ENTRIES` | Both the per-build admission ceiling and process-wide retained-path ceiling (personal probe: 50,000..600,000; 8 GiB reference: 409,600; probe-failure fallback: 100,000; distributed/hard ceiling: 600,000) |
| `TOFU_TREE_INDEX_MEM_ROOTS` | Secondary in-memory project-root ceiling; the shared entry ceiling may evict sooner (personal probe: 2..4; distributed: 4; hard ceiling: 8) |
| `TOFU_MAX_SSE_PER_PRINCIPAL` | Shared live SSE socket/lease ceiling across chat and conversation streams (personal probe: 8..24; 8 GiB reference/probe fallback: 12; distributed: 64; hard ceiling: 128); zero/malformed overrides do not disable the bound |
| `TOFU_SSE_SLOT_TTL` | Crash-reclaim lease window for admitted SSE streams (default 300 s; clamped to 45..3,600 s and refreshed at most every one third of the window) |
| `TOFU_MAX_INFLIGHT_TASKS` / `TOFU_AGENT_WORKERS` | Root-task admission and physical Agent execution concurrency. Personal defaults scale 1..48 from effective CPU and launch-probed memory/RSS headroom (8 GiB reference: 4; 64 CPU / 64 GiB reference with 48 GiB available: 18; a very large host reaches 48 inside the default 64 GiB worker cap). Raising only these values cannot bypass the process/cgroup admission envelope |
| `TOFU_TASK_RUNTIME_TASK_CAPACITY` / `TOFU_TASK_RUNTIME_EVENT_CAPACITY` / `TOFU_TASK_RUNTIME_REPLAY_MAX_MIB` / `TOFU_TASK_RUNTIME_EVENT_MAX_MIB` | Process-memory terminal-record target per task kind plus per-task replay count, ordinary serialized-tail target, and complete single-event ceiling. Probe-failure profile: 64/1,024/2/4; 8 GiB reference: 128/2,048/4/8; distributed: 512/4,096/8/16; hard caps: 1,024/8,192/16/16. Active work is governed separately and never evicted. A valid event above the tail target occupies the window alone; an event above its hard ceiling resets only reconstructible memory replay and advances the absolute cursor. Explicit runtime arguments can lower but not widen the launch policy. |
| `TOFU_CHAT_TASK_TERMINAL_TTL_SECONDS` | Hot Python residency after a chat task receives its immutable terminal stamp. Personal launch probe: 600..1,800 s; 8 GiB and probe-failure profiles: 600 s; distributed: 3,600 s; explicit hard range: 60..86,400 s. Active tasks are never TTL-evicted. After the hot window, owner-scoped generic task detail/events/SSE reconstruct from `task_results` and the durable event log; this setting deletes neither conversation state nor durable task results. |
| `TOOL_MAX_PARALLEL_WORKERS` | Per-task parallel read-only tool calls (personal probe: 1..4; probe-failure fallback: 1; distributed: 8; hard ceiling: 32). Streaming prefetch reuses at most four workers and retains eight calls per model round. Provider capability diagnostics reuse a process aggregate capped at eight: one provider task in personal mode, two only at the distributed eight-worker profile, with a finite 4..16 pending-task lane. |
| `TOFU_PRODUCTION_LLM_FANOUT` | Per-job ceiling for independent LLM calls inside long-production stages such as research judges, long-form sections, slide pages, and motion scene authors (personal probe: 1..2; 8 GiB reference: 2; probe-failure fallback: 1; distributed: 4; explicit hard ceiling: 8). Motion authors additionally take the smaller image-generation budget and impose a two-worker capability ceiling. Root-task admission remains the process-wide multiplier |
| `TOFU_OPTIONAL_LLM_MAX_429_ATTEMPTS` | Actual upstream 429-response ceiling per reconstructible project-summary, automatic-title, daily-report-analysis, or optimizer-proposal call (personal/probe-failure: 2; distributed: 8; explicit hard ceiling: 16). Slot-capacity polling does not consume the allowance; terminal no-slot/budget states fall back without another title dispatch. Attended Agent and explicit scheduler prompt execution do not inherit this policy |
| `TOFU_PRODUCTION_LLM_MAX_429_ATTEMPTS` | Actual upstream 429-response ceiling per background production model call (personal probe: 4..8; 8 GiB reference: 8; probe-failure fallback: 4; distributed: 16; explicit hard ceiling: 64). Capacity polling does not consume this budget; interactive chat retains its separate retry policy |
| `TOFU_PRODUCTION_IMAGE_FANOUT` | Per-job ceiling for independent background image-generation calls (personal probe: 1..2; 8 GiB reference: 2; probe-failure fallback: 1; distributed/explicit hard ceiling: 4). Encoded and decoded image replies remain bounded separately by the owning capability |
| `TOFU_PRODUCTION_IMAGE_MAX_429_ATTEMPTS` | Actual upstream 429-response ceiling per background image-generation call (personal probe: 4..8; 8 GiB reference: 8; probe-failure fallback: 4; distributed: 16; explicit hard ceiling: 64). Interactive image generation retains its 120-cycle compatibility safety cap unless a production caller opts into this policy |
| `TOFU_PRODUCTION_TTS_FANOUT` | Per-job ceiling for independent narration-segment synthesis (personal probe: 1..2; 8 GiB reference: 2; probe-failure fallback: 1; distributed: 4; explicit hard ceiling: 8). Audio bytes remain bounded by the script/chunk contracts until ordered assembly |
| `TOFU_AGENT_QUEUE_CAPACITY` | Finite FIFO wait capacity before an Agent slot is acquired (default `workers * 8`, clamped to 8..512; explicit hard ceiling: 4,096). Saturation fails the accepted attempt visibly instead of entering an unbounded executor queue |
| `TOFU_AGENT_STUCK_REPLACEMENTS` | Maximum physical replacement threads retained behind reaper-proven wedged calls (personal default `ceil(workers / 4)`, clamped to 1..4; explicit hard ceiling: `min(16, workers)`). Logical concurrency never increases |
| `TOFU_TASK_RSS_RESERVE_MB` | Measured per-active-task RSS admission envelope used to derive the personal hard concurrency ceiling (512 MiB on smaller workers; 1,024 MiB when the worker hard limit exceeds 3 GiB) |
| `TOFU_PROCESS_RSS_RELIEF_MB` / `TOFU_PROCESS_RSS_RECYCLE_MB` | Worker soft-relief and hard-recycle boundaries. New task and request admission also use these local-process limits even when the shared host/cgroup still has ample memory |
| `TOFU_STORAGE_PG_READ_POOL` / `TOFU_STORAGE_PG_WRITE_POOL` | PostgreSQL pool requests (32/16, automatically capped to the 80% server budget) |
| `TOFU_STORAGE_MIN_FREE_BYTES` | Data-filesystem startup reserve (personal probe: 1% of volume, clamped to 256 MiB..2 GiB) |
| `TOFU_STORAGE_RECOVERY_COPY_BUDGET_MIB` | Total admitted SQLite recovery-copy footprint on one volume (personal: 50% of launch-probed data volume, clamped to 4..512 GiB; probe-failure fallback: 64 GiB; distributed: 1 TiB; explicit hard ceiling: 8 TiB). A same-filesystem fastpath may atomically replace older verified backups after the new hard-linked point is fully verified; rollback points never rotate automatically |
| `TOFU_STORAGE_SQLITE_BACKUP_TIMEOUT_SECONDS` | Full verified-backup deadline derived from the same copy budget (personal default: 1,800..21,600 s; probe-failure fallback: 5,896 s; explicit hard ceiling: 86,400 s) |
| `TOFU_STORAGE_FASTPATH_WAL_REBASE_MAX_MIB` | Admission watermark for each local/durable WAL before a full fastpath shadow rebase. The effective trigger is one quarter of authority bytes with a 64 MiB floor, capped per WAL at 2% of launch-time free disk and 16 GiB (probe-failure fallback: 512 MiB; distributed: 16 GiB; explicit maximum: 16 GiB). Shipper startup rechecks both the local-front and durable-shadow volumes and uses the smaller 2% envelope, so the two-WAL trigger envelope is at most 4% of observed free space. At the first physical commit at/above the watermark, later transactions receive retryable `database_busy` before `BEGIN` until the shipper's raw checkpoint creates headroom; one already-started bounded commit segment remains atomic. Image-plus-tail publication keeps its separate capacity check; lower overrides trade disk headroom for more database-sized sequential copies and earlier pressure |
| `TOFU_RUN_PYTHON_CACHE=auto|1|0` / `TOFU_RUN_PYTHON_CACHE_MAX_MIB` | Auto-select repeat-heavy network-workspace Python bytecode caching, force it for experiments, or disable it; set its bounded local ceiling (personal probe: 16..64 MiB) |
| `TOFU_SERVER_PYTHON_CACHE=auto|1|0` / `TOFU_SERVER_PYTHON_CACHE_MAX_MIB` / `TOFU_SERVER_PYTHON_CACHE_DIR` | Let the stdlib lifecycle manager select, force, disable, size, or relocate its project/interpreter-scoped host-local server bytecode cache (personal probe: 16..64 MiB; distributed: 128 MiB; hard ceiling: 512 MiB); an existing `PYTHONPYCACHEPREFIX` or `PYTHONDONTWRITEBYTECODE` wins |
| `TOFU_TOKEN_COUNT_CACHE_CAPACITY` | Digest-only repeated-text token-count entries (personal probe: 64..512 in normal profiles; distributed: 1,024; hard ceiling: 4,096) |
| `TOFU_USAGE_CACHE_CAPACITY` | Recent per-conversation provider-usage anchors used by the exact-first token counter (personal probe: 128..2,048; 8 GiB reference: 256; probe-failure fallback: 128; distributed: 4,096; hard ceiling: 8,192). Capacity/TTL eviction safely falls through to the next local counter tier; retained model/role/signature fields are bounded and memory-pressure relief may clear the reconstructible working set. |
| `TOFU_RATE_LIMIT_MEMORY_BUCKET_CAPACITY` | Process-local exact sliding-window endpoint/client buckets and authenticated API-key token-pairs (personal probe: 512..4,096; 8 GiB reference: 1,024; probe-failure fallback: 512; distributed: 4,096; hard ceiling: 16,384). The endpoint/client store derives a second envelope of at most 128 timestamps per configured bucket, capped at 1,048,576 total; both stores reclaim LRU state. Sidecar-backed endpoint/client counters ignore this knob. |
| `TOFU_TOOL_SEARCH_TERM_CACHE_CAPACITY` | Process-wide short-text tokenization working set for Tool Search (personal probe: 512..4,096 entries; 8 GiB reference: 1,024; probe-failure fallback: 512; distributed: 4,096; hard ceiling: 16,384). Inputs above 1,024 characters remain fully searchable but bypass the cache, so the entry limit is also a bounded text-residency budget. The same launch signal derives MCP pre-request catalog indexes (4 lean, 8 at 8 GiB, 32 distributed/hard ceiling) and 24-hour sticky selection states (1,024 lean, 2,048 at 8 GiB, 4,096 distributed/hard ceiling); no separate knobs can multiply this resident budget. Capacity resolves once per process, and an already materialized positive value is clamped without repeating the unused launch probe. Sticky TTL uses a process-monotonic LRU prefix, so live-state cleanup is independent of wall-clock corrections and does not scan the whole state budget. |
| `TOFU_TOOL_RESULT_CACHE_CAPACITY` | Per-task FIFO of reusable and streaming-prefetched execution receipts (personal probe: 64..256 entries; 8 GiB reference: 128; probe-failure fallback: 64; distributed: 512; hard ceiling: 1,024). Pressure evicts the oldest optimization receipt for safe live re-execution; terminal settlement releases the whole cache before the remaining hot task TTL. |
| `TOFU_MEMORY_METADATA_CACHE_CAPACITY` / `TOFU_MEMORY_METADATA_CACHE_MAX_MIB` | Process-wide parsed-frontmatter LRU for body-free memory summary lists, bounded by both entry count and estimated retained bytes (personal probe: 512..4,096 entries / 4..32 MiB; 8 GiB reference: 2,048 / 16 MiB; probe-failure fallback: 512 / 4 MiB; distributed: 8,192 / 64 MiB; hard ceilings: 16,384 / 128 MiB). Exact file fingerprints invalidate edits; body, provenance, eligibility, and authorization are never cached. |
| `TOFU_PAPER_QA_SOURCE_CACHE_CAPACITY` | Process-wide TTL/LRU of owner-scoped parsed-paper sources used by repeat Q&A starts (personal probe: 1..8 entries; 8 GiB reference: 2; probe-failure fallback: 1; distributed: 8; hard ceiling: 32). Every entry is independently capped at 1,000,000 characters, expires after 600 seconds, is cleared by process memory relief, and is re-authorized with a body-free owner lookup before reuse. |
| `TOFU_PAPER_{REPORT,QA,DEEPEN,INSIGHT,RECOMMEND}_AGENT_TOKEN_BUDGET` / `TOFU_RESEARCH_{SURVEY,IDEATE}_TOKEN_BUDGET` | Per-task logical-token envelopes for open-ended Paper agent loops. Defaults by stage are Report 480k, Q&A 240k, Deepen 320k, Insight 240k, Recommend 160k, Survey 240k, and Ideate 160k. A reached envelope removes tools from the next call so the model can synthesize from gathered evidence; zero/malformed/below-16k values use the stage default and every override is hard-capped at 2,000,000. |
| `TOFU_PAPER_{REPORT,QA,DEEPEN,INSIGHT,RECOMMEND}_AGENT_DISPATCH_BUDGET` / `TOFU_RESEARCH_{SURVEY,IDEATE}_DISPATCH_BUDGET` | Actual agent-loop dispatch attempts per Paper task, including responses without usage metadata and the reserved final tool-less synthesis call. Defaults are 10 for Report/Survey/Ideate and 8 for Q&A/Deepen/Insight/Recommend; zero/malformed/below-2 values use the stage default and the hard ceiling is 32. Provider-internal transport retries retain their separate bounded policy; exact repeats remain governed by the call+world breaker. |
| `TOFU_TRANSLATE_CACHE_MAX_MIB` | Whole on-disk translation-result cache ceiling, divided exactly across 256 hash shards (personal probe: 32..512 MiB; probe-failure fallback: 128 MiB; distributed: 1,024 MiB; hard ceiling: 4,096 MiB) |
| `TOFU_TRANSLATE_MAX_429_ATTEMPTS` | Actual upstream rate-limit-class responses allowed per optional translation dispatch (personal probe: 4..8; probe-failure fallback: 4; distributed: 16; hard ceiling: 64); capacity polling does not count and interactive agent dispatch is unchanged |
| `TOFU_TRANSLATE_WORKERS` / `TOFU_TRANSLATE_QUEUE_CAPACITY` | Shared owner-fair optional-translation workers and finite pending background/send-input tasks (personal probe: 1..2 workers / 4..32 queued; probe-failure fallback: 1/4; distributed: 16/128; hard ceilings: 64/1,024). Attended send work advances only within its owner's queue and falls back to original input on saturation/timeout; no request-local carrier is created. The worker value also caps active MT/LLM calls across synchronous-send and incremental carriers; the queue value independently caps provider waiters, whose saturation returns retryable `server_busy` without model rotation or backoff. |
| `TOFU_TRANSLATE_WORKER_IDLE_SECONDS` | Quiet window before shared translation workers retire and recreate lazily (personal: 60 s; distributed: 600 s; `0` keeps admitted workers resident; hard ceiling: 86,400 s) |
| `TOFU_PDF_PROCESSES` / `TOFU_PDF_PARSE_CAPACITY` / `TOFU_PDF_MAX_PAGES` / `TOFU_PDF_MAX_TEXT_MIB` / `TOFU_PDF_PARSE_TIMEOUT` / `TOFU_PDF_WORKER_IDLE_SECONDS` | Classic local PDF child processes, aggregate direct+pool unfinished inputs, per-document page/text ceilings, caller wait ceiling, and idle child residency. The 8 GiB reference is 1/3/512/4 MiB/1,024 s/60 s idle; probe-failure fallback is 1/3/256/2 MiB/300 s/60 s; distributed is 4/16/2,048/16 MiB/3,600 s/600 s; hard ceilings are 16/64/4,096/64 MiB/3,600 s/86,400 s. A timed-out running child retains admission until it actually settles and is never rerun concurrently in-process; idle children retire automatically, while explicit idle `0` keeps them resident. Requests may only lower page/text limits; retained images are separately hard-capped at 64 and 2,048 px. |
| `TOFU_PDF_VLM_TASK_WORKERS` / `TOFU_PDF_VLM_QUEUE_CAPACITY` | Shared owner-fair whole-PDF execution slots and pending source-PDF allowance (personal probe: 1..2 workers / 2..8 queued; probe-failure fallback: 1/2; distributed: 4/32; hard ceilings: 16/256). Since each upload is already capped at 200 MiB, the 8 GiB reference default of 1/2 bounds compressed input retained by active and queued tasks to 600 MiB |
| `TOFU_PDF_VLM_CALL_WORKERS` / `TOFU_PDF_VLM_MAX_PAGES` | Page-batch model-call concurrency and page ceiling checked before rendering (personal probe: 1..4 calls / 64..256 pages; 8 GiB reference: 2/128; probe-failure fallback: 1/64; distributed: 8/512; hard ceilings: 16/2,048). Legacy `PDF_VLM_MAX_WORKERS` may only lower call concurrency |
| `TOFU_PDF_VLM_TASK_TIMEOUT_SECONDS` / `TOFU_PDF_VLM_MAX_429_ATTEMPTS` | Whole-job deadline and actual upstream rate-limit responses allowed per page-batch call (personal probe: 1,920..7,200 s / 4..8 attempts; probe-failure fallback: 1,920/4; distributed: 14,400/16; hard ceilings: 86,400/64) |
| `TOFU_PDF_VLM_WORKER_IDLE_SECONDS` | Quiet window before whole-PDF worker threads retire and recreate lazily (personal: 60 s; distributed: 600 s; `0` keeps admitted workers resident; hard ceiling: 86,400 s) |
| `TOFU_KNOWLEDGE_MAX_TEXT_CHARS` / `TOFU_KNOWLEDGE_OCR_MAX_PAGES` / `TOFU_KNOWLEDGE_VISUAL_MAX_PAGES` / `TOFU_KNOWLEDGE_MAX_VISUAL_ASSETS` / `TOFU_KNOWLEDGE_MAX_VISUAL_BYTES` / `TOFU_KNOWLEDGE_MAX_ASSET_BYTES` / `TOFU_KNOWLEDGE_MAX_IMAGE_PIXELS` | Local Knowledge text, scanned-PDF OCR, visual traversal/count/aggregate bytes, and individual image encoded/decoded ceilings. Defaults are 12,000,000 chars, 80/80 pages, 160 assets/160 MiB, 25 MiB/asset, and 40 million pixels; hard ceilings are 50,000,000 chars, 500 pages (also clamped by `TOFU_PDF_MAX_PAGES`), 1,000 assets/1 GiB, 100 MiB/asset, and 100 million pixels. Validation, text, OCR, asset/source persistence, and repository commit hold one classic-PDF lease; OCR stops when the text budget is full. At the 8 GiB reference capacity of three PDFs, compressed source residency is at most 150 MiB and retained visual candidates at most 480 MiB before parser-native transient state. |
| `TOFU_KNOWLEDGE_ENRICH_WORKERS` / `TOFU_KNOWLEDGE_ENRICH_OWNER_CAPACITY` | Shared owner-round-robin knowledge-image description calls and finite retained owner IDs (personal probe: 1..2 workers / 4..32 owners; 8 GiB reference: 1/16; probe-failure fallback: 1/4; distributed: 8/128; hard ceilings: 16/512). Each owner turn claims one durable asset, so image bytes never accumulate in the scheduler |
| `TOFU_KNOWLEDGE_ENRICH_WORKER_IDLE_SECONDS` | Quiet window before knowledge-enrichment workers retire and recreate lazily (personal: 60 s; distributed: 600 s; `0` keeps admitted workers resident; hard ceiling: 86,400 s) |
| `TOFU_SWARM_GLOBAL_WORKERS` / `TOFU_SWARM_MAX_PARALLEL` | Process-wide owner-round-robin SubAgent execution and per-session scheduler-thread ceilings (personal probe: 1..4 / 1..4; 8 GiB reference: 2/2; probe-failure fallback: 1/1; distributed: 16/8; hard ceilings: 32/16). Separate conversations therefore share one expensive execution budget instead of multiplying API/tool concurrency |
| `TOFU_SWARM_MAX_AGENTS_PER_WAVE` / `TOFU_SWARM_MAX_AGENTS_PER_SESSION` / `TOFU_SWARM_MAX_RETRIES` | Accepted model work, retained dependency/results state, and automatic retries (personal probe: 2..8 per wave / 6..24 per live session / 1 retry; 8 GiB reference: 4/12/1; probe-failure fallback: 2/6/1; distributed: 16/64/2; hard ceilings: 32/128/4). The wave limit is also emitted as `spawn_agents.agents.maxItems` and repeated at backend admission |
| `TOFU_SWARM_SESSION_CAPACITY` | Process-wide live/terminal in-memory swarm registry (personal probe: 2..8; 8 GiB reference: 4; probe-failure fallback: 2; distributed: 32; hard ceiling: 64). At capacity, durable terminal memory may retire first; productive sessions are never evicted to admit newer work |
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
