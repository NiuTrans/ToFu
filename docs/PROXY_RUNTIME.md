# Constrained proxy runtime

Tofu keeps the same server URL and requires no browser-side setup when it is
served through a VS Code forwarded port. The server publishes the non-secret
`constrained-proxy` transport profile when `VSCODE_PROXY_URI` is present; a
`/proxy/<port>/` or `/absproxy/<port>/` path is the browser fallback when an
older page has no profile. `TOFU_PROXY_TRANSPORT_PROFILE=direct` or
`constrained-proxy` is the explicit diagnostic override.

## Request budget

There is no client-side read queue. An earlier governor (6 concurrent parsed
reads plus a priority queue under the constrained profile) was removed: after
a server restart the boot salvo held every lane with cold-storage reads for
tens of seconds and foreground user actions queued behind them with no bound,
which cost far more than the proxy admission it protected. Requests now
execute immediately; the browser's own connection limits and the server are
the only governors.

What remains under the constrained profile:

- at most 128 explicitly marked, identical safe GETs may be single-flight
  (coalesced — merged, never queued);
- authentication headers, caller request IDs, bodies, writes, raw responses,
  SSE, and WebSocket traffic are never coalesced;
- allowlisted read methods ride the WebSocket control-RPC lane below instead
  of the forwarded-port HTTP path.

Project directory browsing is a semantically read-only POST and uses that
control-RPC lane with an HTTP fallback. Other writes preserve their original
ordering.

## WebSocket control RPC

Under `constrained-proxy`, allowlisted small reads may use JSON-RPC 2.0 on the
already-open `/api/push` WebSocket, avoiding a new forwarded-port HTTP request
and its proxy setup/queueing cost. This is not a generic URL tunnel. The method
registry currently contains only `project.browse`; identity is resolved once
at the WebSocket auth boundary, and direct-profile clients continue to use
HTTP exclusively. The machine contract is
[`contracts/control_rpc_v1.yaml`](../contracts/control_rpc_v1.yaml).

Problem: directory browsing had separate HTTP/RPC validation, so transport
selection changed whether malformed paths failed before filesystem access.
Decision: both adapters use `lib/project_contracts/browse.py`; the HTTP
schema derives from that lightweight owner without importing filesystem/tool
services. Blank/null paths retain the home view; other
paths admit at most 4096 characters and no NUL. `showHidden` requires a boolean.
Alternatives considered: copying RPC checks into HTTP leaves two drifting
policies; accepting arbitrary types can convert invalid input into a 500.

The channel starts allowlisted work on demand, without per-socket concurrency,
per-minute rate or global worker admission quotas. `TOFU_CONTROL_RPC_WORKERS`
sizes idle thread reuse; handlers use the common demand-driven executor rather
than queueing behind the ASGI default pool. Request context is copied into
physical workers. Requests remain at most 16 KiB and responses 2 MiB; actual
method deadlines, validation, ownership and duplicate in-flight IDs remain
under their original authorities.

Timeout/cancellation settles the browser waiter while physical accounting
remains active until the filesystem handler actually exits, including after
socket or event-loop close. Socket close stops new dispatch and retires idle
threads, while running reads finish with no late response. RPC responses keep
a reliable FIFO lane until delivery or actual disconnect, without capacity
rejection or oldest-frame eviction. Socket-unavailable, disconnected and
rolling-version method-not-found failures retain the declared HTTP fallback;
real timeout, validation and domain errors do not launch duplicate work.

Problem: count gates rejected useful browse requests, timed-out handlers could
starve unrelated work, and reliable response backlog disconnected callers.
Decision: reuse the existing executor ownership, context and cancellation
boundaries; accept demand and retain response ownership. Alternatives rejected:
raising constants retains nested bottlenecks; a fixed default pool still queues;
releasing physical accounting when a waiter ends misstates resource lifetime;
dropping responses strands correlated promises. No new scheduler or mutation
protocol is introduced.

## Static asset mirror

The production build phase validates the frontend and atomically mirrors only
`static/vite` into host-local temporary storage. Durable user state is never
copied or reclaimed. Requests for other static paths still use the repository.
An absent, unsafe, oversized, incomplete, or disk-constrained mirror falls back
to the authoritative source tree without failing startup. The cache directory
name includes the POSIX euid where `os.getuid` exists; on Windows the per-user
temporary directory is used (uid token 0) so startup never calls a missing
POSIX API.

Defaults and hard ceilings are:

| Budget | Default | Hard ceiling |
|---|---:|---:|
| bytes per generation | 96 MiB | 128 MiB |
| files per generation | 4,096 | 16,384 |
| bytes in one file | 16 MiB | 32 MiB |
| retained complete generations | 3 | 3 |
| free-space reserve | 256 MiB | 16 GiB configurable maximum |

The per-generation default tracks the shipped tree (~68 MiB in 2026-09);
the old 64 MiB default made every boot skip the mirror and serve statics
off the network volume.

Steady-state usage is at most three configured generations (288 MiB by
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

## Forwarding-process connection diagnosis

If a loopback read fails with `EADDRNOTAVAIL` while the listener remains up,
inspect `/proc/sys/net/ipv4/ip_local_port_range`, `/proc/net/sockstat`, and
bounded `ss -ntp` samples before blaming the conversation renderer. Count
connections by destination and owning PID; distinguish Tofu's serving process
from the IDE's forwarding process. A proxy can retain upstream sockets with
large unread receive queues after the corresponding server-side peers have
closed. Exhausted source ports can prevent new reads and isolated test
listeners, even while an alternate loopback destination remains responsive.
Tofu does not own the IDE process or its socket lifecycle. Recovery must target
the forwarding service through its operator, preserving the live application
and in-flight tasks; repeatedly restarting Tofu does not release proxy-owned
sockets. Verify the actual forwarded route again after forwarding recovery.

## Verification

The focused budget guards are:

- `tests/static_serving/test_static_mirror.py`
- `tests/storage/test_credential_validation_budget.py`
- `tests/auth/test_credential_touch_operation.py`
- `tests/server/test_api_transport_constrained_proxy.py`
- `tests/api/test_api_transport_control_rpc.py`
- `tests/frontend/test_push_control_rpc_frontend.py`
- `tests/control_rpc/test_control_rpc.py`
- `tests/project_mod/test_project_browse_resource_budget.py`
- `tests/proxy/test_proxy_project_browse_cache.py`
- `tests/proxy/test_proxy_background_budget.py`

In browser network diagnostics, a cold constrained page should show reads
starting immediately (no client-side queue) with only explicitly marked
identical GETs single-flighted. SQLite metrics should show read
validation per authenticated request but only low-frequency maintenance
`credential.touch` commands rather than one writer transaction per poll.

## Server connection reclamation and local port recovery

**Stalled proxy connections and port pressure.** Problem: a proxy that stops
reading can retain tens of thousands of TCP tuples and exhaust the local
random-port pool; retrying requests or restarting the healthy worker does not
release those proxy sockets. Decision: blocked writes and normal connection
close expire only after **300 seconds without observed progress** by default
(`TOFU_HTTP_WRITE_STALL_SECS`, 1–86400s). Falling transport-buffer size or increasing
Linux TCP_INFO acknowledged-byte count renews the interval; a slow reader may
continue indefinitely while progressing. Older kernels and other platforms use
buffer progress. No short TCP_USER_TIMEOUT is installed: kernel retransmission
policy remains in charge after graceful close. Expired writes and abandoned
shutdown peers receive a TCP reset before abort, reclaiming their ports even
when the proxy retains its descriptor. Explicit shutdown still uses its separate
transport grace; durable task/storage budgets remain with their lifecycle owners.
Problem: Python's drained write buffer can leave unacknowledged kernel bytes;
immediate descriptor close truncated ordinary local HTTP responses. Decision:
the transport owner waits for both Python's write buffer and Linux plain-TCP
`SIOCOUTQ` to empty before normal
close after half-closing its write side, under the same ACK-progress deadline
and shutdown grace. Cancellation
aborts before joining upstream cleanup; TLS keeps its shutdown handshake and
unavailable socket observations retain upstream cleanup. Alternatives rejected:
arbitrary sleeps neither prove delivery nor scale with readers, blocking socket
linger stalls the event loop, and replaying requests risks duplicate effects.
Fresh HTTP-client tests cover complete 34 KiB, 484 KiB and 1 MiB close-delimited
responses, including an empty kernel queue with a still-pending Python buffer.
Real slow/unread-peer tests use the validated 1s minimum rather than synthetic
subsecond intervals that can mistake ordinary delayed ACKs for a stall;
deterministic progress/expiry/cancellation tests retain short fault injection.
The drain stays in the existing connection task and reuses its single progress
observer, with no resident pool or new concurrency cap. A local 4 KiB HTTP
close-response ABBA check (20 requests per leg) completed 40/40 after the change
versus 16/40 before. After-change successful-read median/mean were 0.781/67.759ms;
the failed baseline cannot support a throughput comparison. Kernel delivery and
FIN acknowledgement may extend connection lifetime within the existing budget.
Problem: Quart cancels its response coroutine when HTTP disconnect is delivered;
flushing from that coroutine turns ordinary completion into a reset. Decision:
protocol close only wakes the owned read task; the connection task joins its
application tasks and then drains both queues before closing. The read task
replaces upstream's existing `wait_for` child and is joined on every exit.
Idle expiry uses the same close signal. Alternative rejected: shielding an
unowned close coroutine risks socket work surviving its connection. Real Quart
and plain ASGI completion, cancellation, idle and shutdown tests cover this boundary.
A synthetic Quart 484 KiB close-response check completed 20/20 with connection
ownership versus 0/20 when application cancellation could abort the drain.
Its successful-read median was 3746.473ms on this host; this is delivery evidence,
not a throughput claim or a model/tool-calling performance result.
The standard-library `tofu_db/_loopback.py` recovers only Linux pre-dispatch
allocation errors to the same numeric endpoint from an independent loopback
source within the original deadline; probes, manager control, and storage
protocols share that boundary.
Supervisor source identity includes the recovery module. Alternatives rejected:
restarting Cloud IDE disrupts unrelated work; raising host port limits masks
retention; unconditional resets truncate successful responses; replay after
sending a command risks duplicate writes; fixed drain/close deadlines and short
TCP_USER_TIMEOUT can terminate progressing readers. Real socket tests retain unread peer
descriptors across eight repeated stalled responses: all eight source ports
are reusable before client close, and a subsequent health request succeeds.

SQL listeners first use the kernel random pool. On Linux pool exhaustion they
atomically bind the same loopback address on at most 128 randomly selected
unprivileged ports outside the observed pool, emitting a credential-free
warning. Failed admission remains a typed storage failure, never a backend switch.

Recovery adds no resident worker or durable state. Only buffered writes and
connection close own a cancellable progress observer; every observer is joined
on completion, expiry, or cancellation. Real slow-reader and deterministic
buffer/ACK tests verify response completion beyond multiple idle intervals;
retained-descriptor tests reclaim eight stalled ports and then complete a health
request without restarting the server. A four-leg loopback ABBA microcheck (200
fresh requests per leg, 4 KiB response, Python 3.12) measured baseline
0.543/0.532 ms per request and protected 0.608/0.601 ms; this measures transport
overhead separately from user-facing latency.
