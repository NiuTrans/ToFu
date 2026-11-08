# Android local kernel

This is the server half of "this phone runs the complete Tofu harness". The
Android app starts the normal Tofu server on the device and talks to it over
loopback, as one more kernel next to URL and SSH kernels.

The runtime decision, kernel kinds and security rules are in
[`ANDROID_KERNELS.md`](ANDROID_KERNELS.md):
- A proot Ubuntu arm64 userland runs the unmodified Linux server through
  `local_kernel.py` (Option B at targetSdk 34, decided; device test S1 is
  pending).
- The in-process API below serves embedding hosts and tests.

## Owners

| Concern | Owner |
|---|---|
| Entry: launch policy, CLI, in-process API | `local_kernel.py` |
| Serve path shared with `python server.py` | `server.serve_until_shutdown` |
| Pre-bound sockets and the listening report | `lib/hypercorn_runtime.py` `serve(..., sockets=, on_listening=)` |
| Install-secret admission | `lib/api_keys/_firstrun.py` `install_personal_key`, `lib/api_keys/_crud.py` `require_personal_key_secret` |
| Default deny and the Host fence | `routes/api_v1/auth.py` `auth_before_request`, `local_kernel_host_fence` (registered first by `lib/app/app_assembly.py`), `local_kernel_authority_refused`; push socket in `routes/push.py` |
| Updates and restarts refused | `routes/api_v1/update.py` |
| Device pairing refused | `routes/api_v1/auth.py` `_is_public`, `routes/api_v1/desktop.py` |
| One kernel per data directory | `lib/server_boot/lock.py` `_acquire_instance_lock`, `_pid_is_live_server` |

`local_kernel.py` publishes `app.extensions['tofu_local_kernel'] = {'host':
'127.0.0.1:<port>'}` before serving. The Host fence and the update refusal key
on that value, and so does the pairing refusal. A server started any other way
has no such entry and behaves as before.

## Launch contract

The launcher (the app's `KernelProcess`) does this:

1. **Command.** `python local_kernel.py --data-dir <data dir> [--port N]`,
   from the payload root.
   - `--port` defaults to 0, which lets the OS choose a free port.
   - Never put the secret in argv. Other apps can read argv, and so can
     anything that lists processes.
   - Start a kernel only after the previous one on that data directory has
     exited. Before binding, the CLI takes the server's single-instance lock
     `<data dir>/.server.lock`, the one `python server.py` and `asgi.py`
     take. If another kernel or a desktop server holds it, the CLI writes
     `another Tofu server already serves this data directory (<dir>)` to
     stderr, prints no stdout record, and exits 2.
2. **Environment.** Keep it minimal, with no secrets. Before the first `lib`
   import, the entry sets:
   - `TOFU_DATA_DIR`, from `--data-dir`;
   - `TOFU_AUTH_MODE=private`. This is forced: the environment wins over
     `data/config/auth.json`, so a settings change cannot loosen the gate.

   After binding, it also publishes `_TOFU_RUNTIME_HOST=127.0.0.1` and
   `_TOFU_RUNTIME_PORT=<bound port>` for the lifespan owners.
3. **Secret, on stdin.** The first line is the per-install secret:
   - `tofu_admin_` followed by 64 lowercase hex digits, i.e. 256 bits from
     SecureRandom, generated once per install and kept Keystore-wrapped by
     the app;
   - anything else exits with status 2 before boot, and the error never
     echoes the value.
4. **Port report, on stdout.** stdout carries exactly one line, written once
   the listener accepts connections:
   ```json
   {"event": "listening", "host": "127.0.0.1", "port": 43567, "pid": 4242}
   ```
   Everything else the process or its children print goes to stderr.
5. **Readiness.** Poll `GET http://127.0.0.1:<port>/api/v4/meta` without a
   credential.
   - The boot gate answers 503 while the background boot runs, and 503
     `server_boot_failed` if it failed (`POST /api/v4/boot/retry` re-runs it;
     that call needs the secret).
   - 200 means ready.
6. **Requests.** Every other request carries the secret:
   - **WebView:** the HttpOnly cookie `tofu_session=<secret>` for host
     `127.0.0.1`, installed before the first page load. Never `/?token=`.
   - **Native calls:** `Authorization: Bearer <secret>` or `x-api-key: <secret>`.

   Every request, the WebSocket `/api/push` included, must send
   `Host: 127.0.0.1:<port>`. Any other authority gets 403.
7. **Stop.** Send SIGTERM; SIGINT and SIGHUP behave the same.
   - The kernel drains and exits 0.
   - A drain that outlives `TOFU_SHUTDOWN_HARD_SECS` (existing setting,
     default 30 s, `lib/server/server_shutdown.py`) exits 1. The launcher's
     minimal environment does not set it, so the default applies.
   - **Stop deadline.** 35 s after SIGTERM, a kernel that is still running is
     stopped by force (the app sends SIGQUIT to proot, which kills every
     guest; android/README.md "Local kernel"). This is the desktop lifecycle
     manager's escalation rule (`server_manager.py`
     `_terminate_timeout_seconds`): the drain deadline plus
     `WORKER_TERMINATE_TIMEOUT_MARGIN` (5 s), so the kernel's own deadline
     always fires first and its bounded storage stop is never cut short. It is
     a liveness bound, not a tuning knob: a kernel that ignores SIGTERM would
     otherwise keep the data directory's instance lock, and so block every
     later start, indefinitely.
8. **Exit statuses.**
   - 0: a requested stop. This includes the in-app `POST
     /api/v4/update/shutdown`.
   - 2: refused before boot: a usage or secret-format error, or another
     server holds this data directory. Restarting does not help.
   - Anything else is a crash.
9. **Restart budget.** Restart a crashed kernel. Enter safe mode after 5
   crashes within 120 s, until the user starts it again. These are the
   desktop lifecycle manager's existing liveness constants
   (`server_manager.py` `DEFAULT_MAX_FAILURES`, `DEFAULT_FAILURE_WINDOW`).
   A requested stop never spends the budget.

**In-process API** (one kernel per process lifetime):
- `port = local_kernel.start(data_dir, secret, port=0)` blocks until the
  listener accepts connections, or raises the startup error.
- `local_kernel.stop(timeout)` drains and joins the kernel thread. It returns
  True once the thread exited. It arms no `os._exit` backstop, because the
  host owns the process.
- A second `start` raises `RuntimeError`.
- `start` refuses to run after `lib` was imported with a different data
  root.
- `start` takes no instance lock, because its host owns the process. A second
  host on the same data directory is still refused by storage's project lease
  (`.storage-sidecar.lock`) at boot phase 3; that process stays up and answers
  `503 server_boot_failed`.

## What a local kernel serves, and what it refuses

- **Listeners.** One TCP listener, on 127.0.0.1 only.
  - The loopback bind keeps the UDP 15001 LAN discovery responder off.
  - Storage children listen on loopback, as on desktop.
  - Channels (DX, GitHub, Feishu) are outbound clients and open no listener.
- **Default deny.** Private mode answers 401 on every route without a
  credential, except the boundary's reviewed public surface:
  - probes: `/api/health`, `/api/ready`, `/health/*`, `/api/v4/meta`;
  - API self-description: OpenAPI, docs, capabilities, `keys/whoami`,
    `auth/mode`, `users/me`;
  - `/static/*` and favicons;
  - sign-in pages (`/login`, `/signup`, `/dashboard`);
  - endpoints whose payload is its own credential: signed billing webhooks,
    and relay sign-up/login.

  The exact list is `_PUBLIC` in `tests/server/test_local_kernel_boundary.py`.
  That test enumerates every registered route and method (766 pairs today) and
  fails when any route outside the list is admitted without a credential, or
  when a listed route stops being admitted.
- **Wrong secret.** A presented but wrong secret is refused (401), even on
  public probes.
- **Host fence.** Any `Host` other than `127.0.0.1:<port>` gets 403, on HTTP
  (static assets included) and on the push socket. This defeats DNS
  rebinding from pages in the device's browsers. The HTTP fence runs right
  after request correlation, ahead of the API-version, boot and storage
  gates, so the answer is 403 during boot as well.
- **Launcher-owned updates.**
  - `GET /api/v4/update/check`, `POST /api/v4/update/apply` and `POST
    /api/v4/update/restart` answer 409. An app update replaces the payload,
    and the launcher restarts the process.
  - In-place re-exec is refused regardless, because no re-exec requester is
    installed.
- **No device pairing.** Devices dial the server they pair with, and nothing
  off the phone can reach a loopback kernel. `POST /api/desktop/pair` is
  therefore not public here (401 without the secret), and both it and `POST
  /api/v4/desktop/pair-code` answer 409 with the secret. No pairing code is
  minted and no `agents:bridge` key is created.

## Decisions

**Entry and port report.**
- *Problem.* The launcher needs an ephemeral loopback port and a
  machine-readable way to learn it. `server.py`, the serverctl option
  validator and the lifecycle manager all assume a fixed port from 1 to
  65535: the manager health-checks a stable endpoint.
- *Decision.*
  - A separate entry, `local_kernel.py`, shares `server.serve_until_shutdown`
    with `python server.py`.
  - It binds loopback before the lifespan, so the watchdog, the LAN guard and
    the first-boot key all see the real port.
  - It reports once listening, as one stdout line (CLI) or the return value of
    `start()`.
  - The desktop entry passes no pre-bound sockets; its bind timing and startup
    order are unchanged.
- *Alternatives considered.*
  - `--port 0` in `server.py`: this would change the shared validator and the
    manager's stable-endpoint contract.
  - A port file in the data directory: stale files and cleanup races.
  - An inherited report fd: a new flag.
  - Reporting at bind time: the launcher would race a socket that is bound
    but not yet listening.

**Secret transport and admission.**
- *Problem.* An app-generated, Keystore-held secret has to pass the existing
  boundary, which only knows storage credentials.
- *Decision.*
  - The secret arrives on stdin.
  - `install_personal_key` admits it as the personal owner's admin key. It
    uses the same atomic `credential.create_if_owner_empty` operation as the
    first-run bootstrap, and runs at the same startup phase.
  - Later boots must prove it is a current admin credential of that owner. A
    revoked or unknown secret, or an owner whose credentials do not include
    it, fails the boot with `PermissionError`. The boot fails closed and the
    gate stays private.
  - Only the SHA-256 is stored; no `.first_run_token` is written.
- *Alternatives considered.*
  - A server-minted token read back from `.first_run_token`: it duplicates the
    secret on disk, and the app cannot hold it in Keystore before first boot.
  - argv: readable by others.
  - An environment variable: a new knob, and inherited by every child.
  - A 0600 file named by an environment variable (the design's alternative):
    not built, because stdin needs no new setting.

**Default deny and the Host fence.**
- *Problem.* Every app on the device, and every web page through DNS
  rebinding, reaches 127.0.0.1. Open mode would give loopback peers a
  synthetic admin.
- *Decision.*
  - Force private mode, and reuse `auth_before_request` and the push gate.
  - Add one Host check, active only when the local kernel published its
    authority. It is registered before the other request gates, so no gate
    answers a foreign authority first.
  - Pin the public surface with a route-enumeration test.
- *Alternatives considered.*
  - A local-kernel auth scheme: a parallel mechanism.
  - A Unix socket: the WebView cannot use it.
  - TLS on loopback: it does not stop other apps on the same device.

**Updates and restarts belong to the launcher.**
- *Problem.*
  - On a non-git payload, self-update overlays a downloaded GitHub tarball
    onto the payload, outside the app's SHA-256-pinned update path.
  - An in-place restart would re-run the CLI without its stdin secret.
- *Decision.* While the local kernel is published, update check, apply and
  restart answer 409.
- *Alternatives considered.*
  - Rely on non-git detection: apply still overlays.
  - Leave the update blueprint unmounted: application assembly would need
    launch-mode knowledge.

## Known limits

- Only a real device can settle these (design §8):
  - W^X at targetSdk 34;
  - foreground-service survival;
  - per-host cookie isolation between 127.0.0.1 and 127.0.0.2;
  - cold start under proot.

## Verification

- `tests/server/test_local_kernel_listener.py`: loopback bind, port 0,
  listening report, shared serve path.
- `tests/api_keys/test_first_run_token_lifecycle.py`: install-secret
  admission.
- `tests/server/test_local_kernel_boundary.py`: route enumeration,
  transports, Host fence, push socket, update and pairing refusal.
- `tests/server_boot/test_instance_lock_reclaim.py`: a live
  `local_kernel.py` holder keeps its lock.
- `tests/server/test_local_kernel_entry.py` (`api` + `slow`): real CLI and
  in-process boots, and a second CLI kernel on the same data directory
  exiting 2 while the first keeps serving. Checks the stdin secret, the stdout record, no listener
  off loopback, no UDP 15001, and that the secret never appears in argv,
  environ, logs or data. SIGTERM must exit 0.
