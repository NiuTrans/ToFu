# Constrained proxy runtime

Tofu keeps the same server URL and requires no browser-side setup when it is
served through a VS Code forwarded port. The server publishes the non-secret
`constrained-proxy` transport profile when `VSCODE_PROXY_URI` is present; a
`/proxy/<port>/` or `/absproxy/<port>/` path is the browser fallback when an
older page has no profile. `TOFU_PROXY_TRANSPORT_PROFILE=direct` or
`constrained-proxy` is the explicit diagnostic override.

## Request budget

The typed frontend transport governs parsed reads only under the constrained
profile:

- at most 6 reads execute concurrently;
- at most 256 reads wait in the priority queue;
- foreground reads precede normal and background reads, FIFO within a class;
- caller abort and timeout apply while a request is queued;
- at most 128 explicitly marked, identical safe GETs may be single-flight;
- authentication headers, caller request IDs, bodies, writes, raw responses,
  SSE, and WebSocket traffic are never coalesced.

Project directory browsing is a semantically read-only POST and opts into the
same queue explicitly. Other writes preserve their original ordering and
bypass it.

## WebSocket control RPC

Under `constrained-proxy`, allowlisted small reads may use JSON-RPC 2.0 on the
already-open `/api/push` WebSocket, avoiding a new forwarded-port HTTP request
and its proxy setup/queueing cost. This is not a generic URL tunnel. The method
registry currently contains only `project.browse`; identity is resolved once
at the WebSocket auth boundary, and direct-profile clients continue to use
HTTP exclusively. The machine contract is
[`contracts/control_rpc_v1.yaml`](../contracts/control_rpc_v1.yaml).

The channel admits at most four requests per socket and 120 per minute. Global
blocking work derives from the launch-time `TOFU_CONTROL_RPC_WORKERS` budget
(personal 2..8, distributed 32). Requests are at most 16 KiB; Hypercorn rejects
WebSocket messages above 64 KiB; results are at most 2 MiB. Each method has a
10-second server deadline and the project client has a 12-second total
lifetime. Timeouts and cancellation settle the browser request immediately,
but the global worker slot is retained until an already-running filesystem
call actually exits, preventing timed-out calls from creating an unbounded
thread queue.

RPC responses use a reliable 64-frame lane ahead of lossy event data. A slow
client that fills it is disconnected, so correlated Promises fail visibly
rather than losing one response forever. Socket-unavailable, disconnected, and
rolling-version method-not-found failures use the existing governed HTTP
endpoint with the remaining deadline. Capacity, timeout, validation, and
domain failures do not launch a duplicate HTTP request. Client abort sends
`$/cancelRequest`; reconnect never replays an old request ID.

### Codex source alignment

The design adopts the failure semantics already exercised by the vendored
Codex app-server rather than copying its implementation language or its much
larger workload-specific queue sizes:

- `codex/codex-rs/app-server-transport/src/transport/mod.rs` uses typed,
  bounded channels and returns an explicit overload error before work enters a
  saturated processor;
- `codex/codex-rs/app-server/src/transport.rs` uses `try_send` and disconnects
  a slow WebSocket when reliable outbound delivery can no longer be promised;
- `codex/codex-rs/app-server/src/connection_rpc_gate.rs` keeps admitted work
  counted until the handler really finishes and prevents new handlers during
  connection shutdown.

Tofu applies those three invariants to its existing authenticated push socket.
Its per-connection worker count includes filesystem calls whose browser
deadline has already expired; one tab cannot consume every global worker by
issuing another request after each timeout.

## Static asset mirror

The production build phase validates the frontend and atomically mirrors only
`static/vite` into host-local temporary storage. Durable user state is never
copied or reclaimed. Requests for other static paths still use the repository.
An absent, unsafe, oversized, incomplete, or disk-constrained mirror falls back
to the authoritative source tree without failing startup.

Defaults and hard ceilings are:

| Budget | Default | Hard ceiling |
|---|---:|---:|
| bytes per generation | 64 MiB | 128 MiB |
| files per generation | 4,096 | 16,384 |
| bytes in one file | 16 MiB | 32 MiB |
| retained complete generations | 3 | 3 |
| free-space reserve | 256 MiB | 16 GiB configurable maximum |

Steady-state usage is at most three configured generations (192 MiB by
default, 384 MiB at the hard ceiling). One atomic build may exist transiently;
abandoned build directories older than ten minutes are reclaimed on the next
preparation. The default root is
`/tmp/tofu-static-mirror-<uid>` and must be owned by the process user.

Configuration seams are `TOFU_STATIC_MIRROR=0`,
`TOFU_STATIC_MIRROR_DIR`, `TOFU_STATIC_MIRROR_MAX_BYTES`,
`TOFU_STATIC_MIRROR_MAX_FILES`, `TOFU_STATIC_MIRROR_MAX_FILE_BYTES`, and
`TOFU_STATIC_MIRROR_RESERVE_BYTES`. Startup logs either
`[Static] local Vite mirror ready` with measured files/bytes or the fallback
reason.

## Credential writer budget

Bearer authority remains fail-closed and current on every request:
`credential.validate` checks the token, disabled/revoked/expiry fields, owner
boundary, and bound account status through the read pool. Only the audit-only
`last_used_at` field is coalesced. `credential.touch` runs as a conditional
maintenance command at most once per credential and process per interval,
uses a 250 ms deadline, and cannot grant authority. A failed audit touch does
not deny a request already validated by the read authority.

The default interval is 60 seconds and
`TOFU_CREDENTIAL_TOUCH_INTERVAL_S` is clamped to 15–3,600 seconds. The local
due-time table is capped at 2,048 credentials. The legacy atomic
`credential.authenticate` operation remains registered for rolling wire
compatibility, but application validation uses the split operations.

## Project picker budget

The picker paints a fresh session cache immediately and refreshes in the
background. Closing it or navigating again aborts the old request; generation
checks prevent late results from repainting. Mutations invalidate every alias
of the affected canonical directory.

The cache expires after five minutes and is capped at 16 entries, 128 KiB per
entry, and a conservative 256 KiB total UTF-8 upper bound. It contains only
directory presentation metadata and lives in `sessionStorage`.

Server-side browsing performs exactly one initial `scandir`, examines at most
1,024 total entries, returns at most 512 folders, and caps its estimated JSON
payload at 96 KiB. It reports `truncated` and whether counts are exact. Initial
navigation never enters child directories: code badges and child counts are
cosmetic and remain deferred, so one slow FUSE child cannot block the modal.

## Hidden-page traffic

Direct pages retain the 15-second client-log cadence. Constrained pages use a
60-second cadence with ±15% jitter, skip periodic sends while hidden or known
offline, and retain the existing bounded 400-line buffer and pagehide beacon.

Push delivery still uses one WebSocket per page so no cross-tab leader can lose
subscription authority. Foreground tabs probe every 4 seconds with an 8-second
timeout. Hidden tabs probe every 20 seconds with a 30-second timeout; returning
to the foreground restores the fast cadence immediately. Data frames remain
proof of life and all existing reconnect/catch-up semantics remain intact.

## Verification

The focused budget guards are:

- `tests/test_static_mirror.py`
- `tests/test_credential_validation_budget.py`
- `tests/test_credential_touch_operation.py`
- `tests/test_api_transport_proxy_governor.py`
- `tests/test_api_transport_control_rpc.py`
- `tests/test_push_control_rpc_frontend.py`
- `tests/test_control_rpc.py`
- `tests/test_project_browse_resource_budget.py`
- `tests/test_proxy_project_browse_cache.py`
- `tests/test_proxy_background_budget.py`

In browser network diagnostics, a cold constrained page should show no more
than six governed parsed reads in flight. SQLite metrics should show read
validation per authenticated request but only low-frequency maintenance
`credential.touch` commands rather than one writer transaction per poll.
