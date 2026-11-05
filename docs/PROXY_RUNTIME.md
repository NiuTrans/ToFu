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

The browser also admits every independent RPC immediately. Its in-flight map
correlates replies and owns cancellation; it is not an admission quota. Removing
the earlier 32-request rejection preserves each request's existing deadline,
abort signal, method allowlist and response identity. The regression issues
128 reads before any reply, cancels one and delivers the others out of order.

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

The WebSocket handshake snapshots case-insensitive headers, cookies and direct
peer before running the existing credential resolver on the serving loop's
demand executor. It joins physical validation/audit work through cancellation
before any socket accept/registration. Credential precedence, current owner
validation and post-auth principal binding remain unchanged; no auth-result
cache or second worker pool exists. **Problem:** synchronous credential storage
can delay all serving-loop traffic during a handshake. **Decision:** offload
only finite auth through the existing owned-command boundary. **Alternative
rejected:** caching permission verdicts weakens revocation; abandoning cancelled
validation can register a late connection. Verification: `test_push_auth_offloop.py`
and existing WebSocket auth/ownership/shutdown suites.

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

## Browser connection evidence

**Problem:** two failed browser health requests cannot distinguish a stopped
server from a disconnected client or forwarding failure. Calling this a server
outage, claiming replies are paused, or retaining that verdict after a fresh
push heartbeat misrepresents the evidence.

**Decision:** `frontend/src/backend-availability-monitor.ts` reports connection
unavailability after two failed probes and distinguishes the browser's local
offline signal in both the banner and tab title as that signal changes. Tasks
may continue on the server; reconnect revalidates their authoritative state.
The push owner's existing latency publications carry the actual inbound-frame
timestamp. A newer heartbeat received within the health-probe timeout clears
the warning and fences older in-flight HTTP failures. A bare WebSocket open,
repeated cached reading, stale timestamp, or outer-proxy auth denial does not
prove a server outage or a new recovery. Planned restart suppression, snooze,
visibility-aware polling, and independent storage readiness remain intact.

**Alternatives rejected:** longer probe deadlines only postpone ambiguous
alarms; treating every socket-open notification as healthy can hide a silent
connection; automatically restarting the server interrupts unrelated work
without evidence that the process failed. Verification lives in
`test_frontend_backend_offline_monitor.py`, `test_push_interleaved_latency.py`
and `test_frontend_push_half_open_reconnect.py`.

## Hidden-page traffic

**Served build identity.** The HTML asset-tag owner publishes the build identity
from the manifest it just validated, before returning the new page. Otherwise
the first heartbeat can advertise an old bundle and immediately reload that
already-current page, cancelling its requests and replacing its socket. The
manifest's existing identity key also avoids a second background graph validation.
An older background read may publish only while its observed previous identity
is still current; it cannot roll back the identity of a newly served page.
Health and heartbeat reads remain in-memory, and failed validation retains the
last valid identity. Alternatives rejected: a longer poll interval postpones
the inconsistency; reading the manifest inside each heartbeat restores serving-
loop filesystem stalls. `test_served_build_identity.py` covers initial coherence,
no duplicate graph read, validation failure and interleaved old background work.

Direct pages retain the 15-second client-log cadence. Constrained pages use a
60-second cadence with ±15% jitter, skip periodic sends while hidden or known
offline, and retain the existing bounded 400-line buffer and pagehide beacon.

Push delivery still uses one WebSocket per page so no cross-tab leader can lose
subscription authority. Foreground tabs probe every 4 seconds with an 8-second
timeout. Hidden tabs probe every 20 seconds with a 30-second timeout; returning
to the foreground restores the fast cadence immediately. Data frames remain
proof of life and all existing reconnect/catch-up semantics remain intact.
The badge measures application ping/pong elapsed time, including proxy and
browser scheduling; it is not a snapshot-byte or sync-completion counter.
Data disarms the half-open watchdog without discarding the latest ping's
separate RTT correlation. The next probe supersedes that one timestamp and
socket teardown clears it; stale/superseded pongs cannot update the badge.
**Problem:** an interleaved data frame discarded its matching pong, freezing an
old poor RTT despite continuing traffic. **Decision:** separate RTT correlation
from watchdog proof of life without a timer or more frequent probes.
**Alternatives rejected:** leaving the watchdog armed disconnects busy healthy
sockets; accepting every pong regresses readings across probes/socket lifetimes.
Verification: interleaved latency, half-open reconnect and socket-generation
regressions retain total-silence timeout and recovery behavior.

**Connection recovery ownership.** A failed send or silence watchdog retires the
logical socket immediately, rejects its correlated RPCs and schedules the
existing reconnect without awaiting transport close. A delayed or throwing
`close()` cannot strand work, and late old-socket callbacks cannot settle a new
socket's requests. Temporary server close code 1011 reconnects; intentional 1000
and policy refusal 1008 remain terminal. An explicit connect consumes a pending
retry so that an older timer cannot reconnect after a policy refusal. Every
inbound frame is transport proof of life, including an unmatched pong; only a
matched pong supplies RTT. A retired socket never supplies either. Alternatives
rejected: awaiting TCP teardown retains the original disconnect deadlock;
raising watchdog timeouts hides it; accepting old socket callbacks corrupts
request ownership. `test_push_recovery_lifecycle.py` and the existing half-open,
latency and generation suites specify these boundaries.

## Request parsing work

The compatibility middleware only decodes bodies beginning with the JSON string
token, after JSON whitespace: only that shape can be the supported double-encoded
JSON wrapper. Ordinary objects, arrays and scalars go directly to the handler's
parser without a discarded full-body decode/parse first. Invalid requests still
reach the same handler validation and double-encoded bodies retain their original
behavior. The prefix check neither copies nor truncates the body. In a real
Quart ABBA fixture with identical 2.245 MB requests, 16 requests per leg,
median latency changed from 15.77/16.47 ms to 9.43/10.37 ms; this measures parsing,
not the user's network. `test_http_compat_middleware.py` covers both paths.

## Query re-encoding

**Problem:** code-server's `/proxy/<port>/` route rebuilds the upstream URL as
`encodeURI(path + '?' + qs.stringify(req.query))`, so every escape in a query
reaches the app encoded twice (`%5B` → `%255B`); `/absproxy/<port>/` forwards
the original URL unchanged. Scalar args carry no escapes and are unaffected.
Path args and the page stream's JSON `streams` arg are not: after Quart's one
decode they are still percent-encoded, which empties path lookups and refuses
every multiplexed conversation stream with HTTP 400. Without a live stream, a
history page read at a head newer than the replica is never applied, so older
Turns fail to load in any conversation whose head is moving.

**Decision:** the argument parser undoes the extra encoding where the value's
type makes it unambiguous: `decode_proxy_path_arg` for path args (bounded; it
stops at an existing path) and `decode_proxy_json_value` for JSON args. JSON
text never begins with `%`, so a value that does is decoded until it does not;
a correctly encoded value is never touched.

**Alternatives considered:** patching the IDE's proxy lives outside this
repository, is lost on an IDE upgrade and needs an IDE restart; `/absproxy/`
would require the app to serve every page under the port prefix; a `%`-free
wire encoding of `streams` (base64url) changes the contract for every client;
decoding every query arg twice corrupts correctly encoded values containing
`%`. Specs: `tests/diagnostics/test_request_parser.py` and
`tests/conversation/test_conversation_sync_multiplex.py`.

## WebSocket encoding responsiveness

**Problem:** whole-message WebSocket compression ran synchronously on the HTTP
serving loop. A 2026-10-27 sample found 62 of 218 loop-thread observations in
wsproto's deflate encoder; live task snapshots reached 2.02 MB. Even loopback
application pings ranged from 1.49 to 986.67 ms (25 samples, median 52.52 ms).
Those observations identify a server contributor, not the user's full network
path. The authenticated forwarded URL could not be measured from the diagnostic
client (outer proxy returned 401).

**Decision:** `lib/http_transport/websocket_fairness.py` adapts the existing
Hypercorn application/send boundary per stream, for H1 and H2. Messages of at
least 64 KiB bytes/characters encode in the serving loop's existing demand
executor; small control frames keep the synchronous fast path. This threshold
selects an execution lane, never limits message size or admitted work. A codec
lock protects wsproto's connection state while the worker runs; a separate send
lock preserves outbound order without holding codec ownership over network
drain. Repeated cancellation joins the exact encoder before closing the stream,
and failed encoding fences later sends. Compression, whole-message framing,
owner checks, inbound message bounds and upstream flow control remain intact.
No new pool, timer, cache or durable state is added: a socket owns two async
locks and at most one encoding future/borrowed payload while sending.

Verification: `tests/server/test_websocket_send_fairness.py` exercises real H1/H2
sockets, compressed/uncompressed text and binary, Unicode, simultaneous sends,
protocol ping, cancellation, encoder failure and disconnect delivery. The old
encoder fails the heartbeat-progress and serving-thread assertions.

Run `python3 benchmarks/websocket_encoding.py` for a storage-free ABBA comparison:
independent client/server loops, seed 27, four identical 2 MiB messages per leg,
exact decoded equality, compressed wire bytes and concurrent application pings.
On 2026-10-27, synchronous legs had maximum ping RTTs of 428.56/439.39 ms;
offloaded legs had 4.41/2.48 ms. All four sent exactly 6,318,537 WebSocket bytes
for 8,388,608 payload bytes. Whole-burst times were 442.77/453.99 ms versus
438.08/449.40 ms; this does not establish a throughput gain. These isolated
transport results do not predict the user's VS Code/VPN/browser RTT.

**Alternatives considered:** disabling compression would increase proxy traffic.
Cooperative 64 KiB fragmentation reduced measured loop stalls from 432–450 ms to
about 15 ms, but was discarded: a standalone wsproto 1.3.2 codec reproducer
(`TextMessage(fin=False)`, `Pong`, final `TextMessage`) loses inbound compression
state and reports close code 1007. No fragmentation workaround or dependency
patch is shipped. Copying Hypercorn's protocol constructors would duplicate more
ownership logic than the per-stream adapter. The private adapter is guarded by
actual H1/H2 transport tests; HTTP/3 is outside this TCP adapter.

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
the forwarding service under explicit operator authorization, preserving the
live application and in-flight tasks; repeatedly restarting Tofu does not release
proxy-owned sockets. Verify the actual forwarded route again after recovery.

### Operator-installed response cancellation adapter

Problem: older code-server/http-proxy installations listen for an aborted incoming
request but can miss a downstream response closing after the request body ends.
An upstream response can remain paused after its destination disappears, or even
be piped to an already-destroyed destination. A closed TCP connection can also
retain a JavaScript socket descriptor without appearing in `/proc/net/tcp`.

Decision: an explicitly authorized, local forwarding-process repair can install
[`installTofuResponseCloseGuard`](../scripts/forward_proxy_response_close_guard.cjs)
on that process's existing proxy instance, with an explicit target port. It
cancels only an unfinished downstream response's upstream ClientRequest, including
the already-closed race. Scope is HTTP targets `127.0.0.1` or `0.0.0.0` at that
port. Normal finish and upstream close/error detach all adapter listeners;
WebSocket upgrades, other targets, and slow connected readers retain their owners.
The module has no automatic installation and is not an application dependency.

Before installation, verify the forwarding process's UID, command, process birth,
module identity and container-local writable overlay; preserve the exact original
module and hashes. Validate syntax before publication and do not overwrite a
changed module. Keep a reviewed copy of the adapter in the installed module for
process restarts; a new container/image needs separate verification. A transient
loopback Inspector session may apply the same function to the cached instance
without restarting the IDE, provided it verifies the process and closes its
listener afterward. Do not leave a debugger or cleanup daemon running. Rollback
removes the adapter's `proxyReq` listener and Symbol, and restores only an exact
matching module postimage from the preserved original.

Alternatives considered: repeatedly sweeping orphan descriptors treats symptoms;
resetting all upstream connections interrupts active responses; upgrading or
restarting the whole IDE has broader scope. The server-side ACK-progress cleanup
below remains useful but cannot remove descriptors the proxy has already detached
from TCP. Standalone tests against the installed Node 16/http-proxy reproduced
both abandonment races; the adapter reclaimed both while a paused 16 MiB healthy
response completed exactly. Other-port forwarding and upstream error handling
were checked separately. The committed Node tests cover cancellation, finish/error
cleanup, idempotency and target isolation in the frontend Node gate.

### Operator-installed exact credential reuse

Problem: the reviewed code-server plaintext-password mode verifies the same
salted session cookie with Argon2 on every forwarded request, even when neither
the configured password nor the cookie has changed. This occupies the shared
native worker pool and makes each small read pay a password-derivation cost.
The installed verifier's ABBA fixture (24 checks per leg) measured median
66.37/66.13 ms before reuse and 0.0019/0.0010 ms with reuse; the first verification
still cost 64.77/67.01 ms. Incorrect cookies and password rotation remained denied.
Live ABBA health probes (12 requests per lane per leg, same authenticated clients)
measured local-forwarder medians of 76.19/115.90 ms versus 5.27/3.93 ms, and
public-forwarder medians of 132.19/75.96 ms versus 12.11/11.03 ms. A 565.85 ms
outlier remained with reuse: this removes repeated authentication work, not all
server or network jitter. These probes originate on the server, not the user's PC.

Decision: an operator may install
[`installTofuForwardAuthReuse`](../scripts/forward_proxy_auth_reuse.cjs) on the
reviewed forwarding process's existing `authenticated` export, with an explicit
port. It only adapts that port's `/proxy/` and `/absproxy/` routes in plaintext
password mode. A WeakMap owns one credential generation per existing configuration
object; each generation retains the exact successfully verified, normalized
cookies. Every request checks the current mode, password, hashed-password and
proxy-enabled state. Password changes replace the generation immediately, and
late verification cannot seed a replaced generation. Failed checks and errors
are never cached. Concurrent identical checks share the same verifier promise;
distinct credentials run immediately. No TTL, retry or concurrency knob is added.

This adapter does not grant Tofu application authority, change login credentials,
skip origin checks or adapt other forwarded ports. Its retention is one Set entry
per distinct successful session cookie in the live credential generation, plus
one promise per active distinct verification. Invalid traffic adds no retained
entries. Configuration collection, observed password rotation, removal of the
adapter or process exit releases the corresponding cache. There is no durable
state or background worker.

Installation must verify the exact upstream HTTP and credential utility module
hashes and the live verifier function, preserve original modules and metadata,
syntax-check before atomic publication, and retain an exact rollback image.
The reviewed upstream authority depends only on those current credentials and
the normalized cookie. A future upstream expiry, session store or revocation
policy requires new review; this adapter must not be carried forward on an
assumed-equivalent implementation. Live installation uses only a temporary
loopback Inspector session, verifies process identity, and closes the listener
afterward. Rollback calls the returned `uninstall()` (without replacing a later
owner), then restores only a matching installed postimage. Neither installation
nor rollback restarts the IDE or Tofu. A new container image needs fresh review.

Alternatives rejected: disabling authentication changes authority; pre-hashing
the configured password changes which existing cookies are valid and logs out
current sessions; increasing the native worker count repeats the same expensive
work; caching a verdict without its credential generation weakens revocation.
`forward_proxy_auth_reuse.test.cjs` covers credential rotation, delayed checks,
independent configurations, port isolation, errors and reversible installation.

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

Problem: even a fully acknowledged response can leave a paused proxy socket in
`CLOSE_WAIT` with unread data. Releasing the server descriptor after its kernel
queue empties forfeits the ability to reset that abandoned peer. Decision:
after plain-Linux-TCP half-close and queue drain, the same connection owner
awaits peer EOF under the existing ACK-progress deadline. A reset completes
teardown; expiry or cancellation resets before upstream cleanup. No new task,
worker, admission limit, or persistent state is introduced. TLS and platforms
without kernel queue observation keep their existing close policy. Alternatives
rejected: treating TCP ACK as application consumption leaves retained tuples;
immediate resets truncate healthy responses; restarting the IDE disrupts other
work. Real-socket regressions hold three unread client descriptors open for
each 4 KiB and 256 KiB response case: the old owner leaves their ports occupied
beyond the configured 1s test deadline, while the new owner reclaims them without
client close. Explicit HTTP close and idle expiry share this policy; complete
34 KiB, 484 KiB and 1 MiB HTTP reads and slow-progress tests protect delivery.
Existing abandoned sockets whose server descriptors were already released
cannot be retroactively reclaimed by this owner.

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
