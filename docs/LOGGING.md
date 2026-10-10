# Logging and incident diagnostics contract

This document is the operating contract for Tofu diagnostics. The policy
registry in `lib/log_policy.py`, the incident record schema in
`contracts/log_incident_v1.schema.json`, and the OpenAPI metadata on
`GET /api/v4/logs/diagnostics` are the machine-readable authorities.

## Invariants

1. A request thread never performs durable application-log I/O. It stamps
   context, applies flood control, and uses a bounded non-blocking queue.
2. Every core durable stream has a per-file ceiling, backup count, family
   budget, retention age, and priority in `lib/log_policy.py`. A direct child
   stdout file outside `logs/` must call `register_external_log`; an
   unregistered append-only log is a defect. On POSIX, managed files are `0600`
   and the direct log directory is `0700`; maintenance migrates older permissive
   modes without changing unrecognized operator-owned files. On Windows,
   filesystem ACLs remain authoritative: in-place operations retain existing
   ACLs, while new files and replacement/rotation files inherit their parent's
   ACL. Python mode bits are not an owner-only ACL and maintenance does not
   claim mode migrations.
   Operators must place logs (including external stdout files) under a trusted,
   access-restricted parent directory; Tofu does not audit or rewrite its ACL.
   Windows opens validate no-follow native handles and reject final-component
   reparse points (including junctions), not just symbolic links. Ancestor paths
   must be trusted; this is not a sandbox against ancestor replacement.
3. WARNING and above produce human evidence and a compact JSONL incident
   index. Repeated records may be physically coalesced, but occurrence_delta
   preserves the admitted aggregate count. CRITICAL is never coalesced.
4. Diagnostic logs and Request Inspector archives preserve original credential
   values. There is no automatic credential detection, field-name filtering,
   regex substitution or content-scrubbing pass. BoundedFormatter and the
   JSON-safe value helpers in lib/log_record_values.py enforce only physical
   record size and recursive structure limits. Raw SSE payload capture remains
   explicit opt-in and bounded; archive request values and retained response
   bytes are kept without rewriting. Existing incomplete/scrubbed archives
   retain their historical integrity metadata.
   Problem: full-payload credential scanning delayed HTTP under the Python GIL.
   Decision: remove that feature per the owner's instruction, retaining record,
   retention, capture, compression, owner-access and storage bounds.
   Alternatives rejected: another regex optimization or an opt-in scrubber
   retains unwanted processing; removing resource bounds permits runaway logs.
   Measurement: 1,000 unbounded accesses to one 4 MiB diagnostic string took
   0.39 ms with tracing enabled and 120 additional peak traced bytes.
   Each access returned the original string; no full-string scan or copy occurred.
5. Incident diagnosis never requires the database or storage sidecar. A broken
   storage layer must not make its own evidence unavailable.
6. Model-facing output has a hard byte budget. Models consume a diagnosis
   first, then retrieve one correlation/fingerprint slice of raw evidence if
   needed; they do not ingest whole log files.
7. Identity is explicit. HTTP principal data is stamped at the auth boundary;
   background task lanes bind task, conversation, trace, and user fields and
   clear them before pooled-thread reuse. The global diagnostic API is
   admin-only; any future tenant endpoint must pass a user filter and default
   deny.
8. Every generated HTTP 500 has correlated backend evidence. Exception objects
   are logged with an explicit `(type, value, traceback)` tuple so a call made
   after its `except` block cannot degrade into `NoneType: None`; legacy
   string-only 500 callers record their call stack. Public 500 responses never
   include the private exception text, traceback, SQL, path, or provider body.

HTTP route registration retains only the bounded adapters. The log-clean regex
pipeline, tool-round file-change projection, and text-language cascade import
when their corresponding endpoint is called; diagnostics startup does not pay
for unrelated interactive analysis policies.

**Audit rotation preflight — Problem:** each append resolved its own parent
against the same parent before checking file size. **Decision:** built-in POSIX
string paths reuse one no-follow regular-file stat; mutation still validates the
opened descriptor identity. Windows, path-like, symlink and uncertain cases keep
the original validation. Numeric validation, copytruncate and queue policy stay
unchanged. **Alternatives:** a fast early return duplicated stat work at rotation;
path caches retain stale identity; a native boundary does not remove this work.
Paired owned fixtures measured normal-write completion CPU 36–42% lower and
async completion CPU 33–39% lower. Three 1,000-operation rotation pairs measured
CPU 7–9% lower and passed unchanged tail/RSS guards; earlier 100-operation tail
failures remain recorded. This is component evidence, not HTTP or system speed.
See [admission evidence](evidence/system_audit_rotation_preflight_admission_20261009.json).

## Repeated tool-call evidence

Problem: a locally rejected tool flood looked like a service outage, and the
ordinary SSE log did not establish whether repeated calls came from upstream
or canonical slot reconstruction. Decision: classify `ProviderToolCallFloodError`
as `tool_loop` with a specific response-limit message and manual retry policy.
It does not trigger fallback, automatic turn replay, or route-health penalties.
Silently deduplicating calls was rejected because equal inputs can represent
independent effects; relaxing the guard would allow unbounded generation.

Each streamed rejection records bounded `toolCallEvidence` on the typed error
and one WARNING in the normal incident pipeline. It includes the request
`traceId`, canonical matched position count, distinct call-ID count, at most
six position samples, and parser index/name/split counters. Anthropic native
start identities independently supply wire position count/samples and replayed
start count. Only equal counts with distinct IDs establish `origin=provider_wire`;
other protocols report `canonical_response`, which makes no claim about the
wire origin. Provider-wire evidence cannot distinguish model generation from
internal gateway conversion. No argument body, ID value, prompt or tool result
is included in this evidence. The native parser adds one integer replay counter;
all other work occurs on rejection and reuses existing request-owned state.
An isolated Python 3.12 probe of 33 native calls with tracing enabled took
7.87 ms, retained 79,552 bytes of complete parser state (80,651 peak), and
serialized 350 bytes of rejection evidence. This is an incident cost, not a
new pool/cache or a per-token log. Existing stream/incident retention caps apply.

When raw capture exists, `rawArchiveRef` links archive/task/turn/attempt/round
and trace identities. It identifies a capture attempt, not proof that storage
succeeded or that retention has not expired. The existing archive catalog is
the authority for availability, integrity and truncation. A guard rejection
interrupts an HTTP 200 response; a partial archive is therefore expected and
does not imply transport failure. The anomaly ring also flushes on rejection
using existing per-file/retention bounds; input fragments remain omitted.

The conversation inspector's `--request-evidence` flag follows the selected
authenticated application API to show references for the last four referenced
tasks and up to four archives of each latest request. Reads share its existing
time/request/byte budget; errors and expiration are explicit. It never prints
request or response bodies. Offline SQLite inspection does not switch authority.
Use this metadata to fetch a bounded archive chunk through Request Inspector
when raw-frame verification is needed. Terminal fatal paths also run the normal
tool-round sweep before DONE/persistence, so unadopted announcements stop showing
as running; retries and external/flow-owned lifecycle retain their owners.

## Data flow

The browser console relay accepts the declared `{session, url, entries}`
envelope on the logs/client endpoint under the active API major. Unknown
top-level fields and non-string session/URL values are rejected before any
entry is logged. The page URL is accepted for wire compatibility but is not
written to the log; session labels and messages collapse CR/LF so neither can
forge a second record. The sink retains the newest 200 entries of a burst.

Browser recovered-stall evidence is owned by
`frontend/src/core/browser-responsiveness-monitor.ts`. A visible page passively
observes supported `longtask` and `long-animation-frame` entries of at least
250 ms. It emits `browser.main_thread_stall` through the existing console relay
and requests an urgent flush, into `logs/frontend.log`. Urgent diagnostics
bypass the inferred push-health gate while respecting browser offline status
and existing single-flight/queue bounds; no retry loop is introduced. A relay request in
flight no longer discards unrelated console output; the existing ring bounds
and endpoint recursion guard still apply. Each entry type emits at most once
per five seconds; a subsequent checkpoint includes suppressed count/maximum.
There are at most two observers and two counters, zero polling timers, no new
requests during idle, and no transcript/argument/result copies. A local Node
20.19 probe processing 10,000 synthetic same-window stalls emitted one record
in 4.75 ms wall / 7.96 ms process CPU; this measures the diagnostic callback,
not browser rendering or overall application latency. Visibility
hides disconnect observers; resume reinstalls them without historical buffered
entries. Composition disposal releases observers and the listener.

Evidence includes page-monotonic start/duration/delivery delay, current canonical
main-lane conversation/turn/attempt/task and latest tool-call identifiers,
transport status, and, where the browser supports it, the slowest long-frame
script path/function, script duration, blocking time and forced layout time.
Script URLs omit query/fragment; script attribution and then transport/status
are omitted if needed to keep the structured record inside the existing
800-character relay budget. Identifier labels are capped at 64 characters.
Context is sampled **after** the stall, can
already have advanced or settled, and does not establish that the latest tool
caused it. The browser reports `startAtMs` from its performance time origin plus entry
start, so a delayed batch still identifies when blocking began; browser/server
clock skew remains possible. Session and server ingestion time link the batch.
Monotonic `startMs` alone is not a server timestamp.
Unsupported APIs degrade silently. A permanently hung/killed renderer cannot
run its observer or send logs; missing records do not prove responsiveness.
Offline/backend outages follow existing relay loss policy. Background stalls
are omitted to avoid browser throttling/suspension false positives.

Problem: console/error logs did not reveal recovered main-thread blocking during
programmatic tools. Decision: passive browser timing with bounded canonical
identifier context, reusing the relay. Alternatives: unconditional heartbeat
polling adds idle work and confuses hidden-tab throttling with stalls; copying
tool payloads adds overhead/private content and does not identify blocking code.

The separate `/api/client-error` beacon sends only its declared `message`,
`url`, `conversationCount`, and optional `extra` fields. Its producer omits
browser-only timestamp and user-agent fields: the request boundary rejects
unknown fields, and a rejected beacon loses the original error evidence.

```text
producer
  -> request/principal/task context
  -> original diagnostic values + 16 KiB record bound
  -> WARNING+ duplicate coalescer (burst + power-of-two/heartbeat checkpoints)
  -> bounded non-blocking queue
       -> app/access/error/vendor/frontend evidence families
       -> incident.jsonl (DB-independent structured index)
       -> optional DB fingerprint aggregate (fail-open acceleration only)
```

`error.log` remains human-readable evidence. `incident.jsonl` is the preferred
machine index. The database aggregate and differential digest are caches: when
storage is unavailable, `/api/v4/logs/digest` falls back to the incident index.

Routine request rows and explicitly handled client failures stay in the access
or DEBUG plane. In particular, an incompatible browser extension emits one
structured `browser.protocol_rejected` WARNING; repeated browser-poll 4xx rows
retain only first/power-of-two/five-minute access checkpoints and do not
duplicate that fact into `app.log`, `error.log`, or the incident index. Exact
request totals remain in bounded-cardinality HTTP metrics.
Other client and server failures retain their normal WARNING/ERROR severity.

The managed storage child (the exec'd tofu-db engine, or the Python sidecar)
is spawned with its stderr piped to a dedicated supervisor drainer thread
(`lib/storage/supervisor.py`). The pipe always drains, so the child can never
block on a full stderr buffer; one physical line costs one read, capped at
16 KiB with a `…[truncated]` marker. Every line is also forwarded verbatim
to the inherited stderr, so the external console log keeps its previous
content. The drainer's lifecycle binds to its child (EOF is the only exit);
a respawn detects and reports a still-alive previous drainer, so two
drainers never share one pipe.

The drainer mirrors lines into the app pipeline through
`lib.log.get_logger('tofu.storage.engine')`. Engine operational lines are
exactly one JSON object
`{"source":"tofu-db","level":...,"target":...,"message":...}` so the mirror
routes without parsing free text: `error`/`critical` levels cross at
matching severity, and the `integrity_fence` target crosses at any level
(fence refusals are first-class evidence); other levels stay at DEBUG.
Human-oriented opt-in dumps (`TOFU_DB_FSYNC_STATS` and the `*_stats:`/
`listener_metrics:` lines) mirror at DEBUG, and non-JSON lines forward
as-is at INFO. CLI contract output stays on stdout as key=value/JSON.
Engine observation (health/metrics) goes through the authenticated control
operations, not log scraping.

Storage supervision emits its full lifecycle through
`tofu.storage.supervisor` and `tofu.storage.runtime` — child spawn (pid,
backend), ready handshake, liveness probe failures (count/threshold),
termination escalation steps (owner-channel EOF, SIGTERM, SIGKILL), child
exit (code, decoded signal, uptime), restart scheduling and per-attempt
outcomes with the applied backoff, and the restart circuit breaker's
CRITICAL trip plus heartbeat reminders while the runtime stays in its
terminal `degraded` state. These logger names sit inside the `tofu.`
business prefix admitted by `lib/server_logging._BIZ_PREFIXES`, so every
transition lands in app.log (INFO+) and error.log/incident.jsonl (WARNING+);
a storage authority outage must never be invisible in the standard
pipeline.

## Operator workflow

Start every broad investigation with the bounded report:

```bash
python3 -m lib.log_diagnostics --pretty
python3 -m lib.log_diagnostics --conversation-id <id> --max-bytes 16384
python3 -m lib.log_diagnostics --task-id <id> --window-hours 2
```

The HTTP equivalent is admin-only:

```text
GET /api/v4/logs/diagnostics?window_hours=24&max_items=20&max_bytes=32768
GET /api/v4/logs/diagnostics?request_id=<id>
GET /api/v4/logs/diagnostics?conversation_id=<id>
GET /api/v4/logs/diagnostics?task_id=<id>
GET /api/v4/logs/diagnostics?trace_id=<id>
```

When storage is healthy and the investigation centers on one conversation, read
the durable transcript itself before correlating log slices:

```bash
python3 debug/inspect_conversation.py <conversation_id>
```

The read-only inspector renders turn-native conversations through the same
projection the running sidecar serves, reports which stores reference the ID,
lists compaction receipts, and appends matching `logs/app.log` /
`logs/access.log` lines (`--full`, `--raw`, `--logs N`). It complements, never
replaces, the DB-independent evidence plane above. Per-phase timing and
user-perceived paint evidence for a live or finished run are owned by the
turn-trace contract (`GET /api/v4/tasks/<task_id>/trace`; see
[TURN_TRACE_CONTRACT.md](contracts/TURN_TRACE_CONTRACT.md)).

Inspect retention without mutation, then apply it explicitly if needed:

```bash
python3 -m lib.log_diagnostics --maintenance dry-run --pretty
python3 -m lib.log_diagnostics --maintenance apply --pretty
```

The server and lifecycle manager also run the same policy at startup and every
15 minutes. Core files and registered external descriptors share that one
runtime; standalone desktop/supervisor processes still start it in
external-only mode. Duplicate-tail delivery owns no idle thread: the first
suppressed delta creates one worker, its exact heartbeat checkpoint is queued,
and the worker releases both its generation and retained record payloads. The
database aggregate keeps its 15-second batching window while rows are pending,
but an empty store sleeps directly to the hourly TTL boundary. With an external
console configured, quiet periodic logging workers therefore fall from four to
two and default wakeups from 968 to at most 5 per hour (99.4%+ fewer); the
bounded QueueListener and audit writer are unchanged. The last applied result is stored at
`data/log-maintenance-last.json`; it records active-file rotations, closed-file
compactions, removals, unmanaged files, permission migrations, errors, and
remaining budget pressure. Oversized closed rotations are atomically replaced
with at most one file ceiling of complete-line tail evidence, after age/count
pruning; an over-ceiling single partial record is discarded instead of
retaining a potentially secret-bearing fragment. Append-only streams whose
writers retain descriptors use the same complete-line tail rule with bounded
copy-truncate. Removal targets only regular files in declared directories;
symlinks are never followed. Recent unmanaged files are reported and
protected. Per-process faulthandler files are governed by the same registry:
files owned by live PIDs are protected, while dead-PID files are pruned by age,
count, and family bytes. PostgreSQL uses unique timestamped collector
filenames so a size-triggered rotation can never reopen the same over-limit
file.

## Native crash and stalled-thread evidence

Problem: a GIL-independent faulthandler timer traverses interpreter frames
while other threads execute, return, and destroy generators; the diagnostic
itself can fault while reading stale code metadata. Decision: recoverable
stalls use `lib/server_fault_watchdog.py`, retaining Python frame objects
with `sys._current_frames()` and formatting only filename/function/line
metadata. No locals or source files are read. The loop thread is prioritized;
all other threads fit the existing per-process byte budget, with explicit
truncation and no fixed 100-thread ceiling. Both fault sinks receive the same
snapshot. Measurement with 210 owned worker threads on Python 3.12.13 (five
captures): median 5.9 ms, 160.7 KiB output, peak tracked Python allocation
0.56 MiB. No extra resident worker or periodic artifact family is introduced.

Fatal signals retain early `faulthandler.enable(all_threads=False)` on tmpfs,
falling back to the durable sink/stderr. Boot headers record interpreter,
executable, core limit/pattern/filter and capture scope. The OS core owns
native/all-thread evidence. Alternatives rejected: retaining the unsafe
timer merely to see GIL-held calls; disabling fatal capture; reading FUSE
source files or frame locals during a stall. A Python watcher cannot execute
while a native call continuously holds the GIL: such outages remain visible
to the external manager's heartbeat/liveness policy, but this watcher does
not promise an in-stall Python dump for them.

The manager retains an identity-checked zombie's OS exit evidence in
`lastWorkerProcessExit` before clearing its generation. This observation is
read-only, does not reap the child or identify a signal sender, and remains
unknown if the PID disappeared, was reused or the host lacks Linux `/proc`.
A stale probe error cannot replace a known fatal signal; planned restart,
memory recycle, health-wedge and shared-cgroup OOM accounting retain their
existing authority.

Inspect an explicit saved core offline:

```bash
python3 scripts/inspect_native_crash.py --core /path/to/core --executable /path/to/python3 --output /path/to/new-report.json
```

The tool validates ELF completeness, hashes the supplied executable, disables
GDB auto-loaded scripts/frame arguments, and bounds GDB to 30s and 1 MiB.
Its Linux child uses the kernel one-byte core recursion guard; zero is ignored
by piped collectors. Failure to apply this guard prevents debugger execution.
It never attaches to a running service, reads application storage, changes
host core policy, or treats a supplied binary as a proven match. Core paths,
maps and addresses remain local; the output is a new private file. A piped
host collector can impose its own `file_limit`, independently of the worker's
`RLIMIT_CORE`; increasing `ulimit` alone does not remove that truncation.
Retain a matching interpreter/native library build and the complete core
before host rotation; a truncated dump cannot be repaired afterward.

## Swarm diagnosis

Correlate the execution/session ID and agent ID across `app.log`, the frontend
relay, and access records. A successful status HTTP response establishes route
availability, not successful child execution or a correctly painted panel.
An `await_agents` timeout ends that bounded wait only: children continue, and
an admission heartbeat proves liveness/backpressure rather than productive
model/tool progress. A scheduler stall verdict is separate evidence; inspect
the last real round/tool/dispatch before assigning a cause.

A driver summary with `failed=0` is not a success certificate: cancelled,
stalled, skipped, or unresolved children must remain distinguishable from
completed work. Read the explicit terminal outcome and child counts together.
Expected user cancellation must not be reclassified as an infrastructure
failure, and a late successful child can resolve an earlier stall observation.
Run summaries are bounded diagnostic projections, not a second durable state
machine or an exactly-once model-delivery guarantee. In particular, restart
recovery and provider acknowledgement remain subject to the owning task-engine
and conversation delivery contracts.

Source/unit checks, generated-bundle checks, and the running service are
separate verification layers. A changed Python source does not prove that an
already-running worker has loaded it, and a newly published frontend graph
does not prove that an existing browser tab has adopted it.

## Adding a diagnostic

- Use `lib.log.get_logger`, not a private file handler.
- Use `log_event(logger, level, stable_event_name, message, **fields)` when a
  stable event name or structured fields materially improve diagnosis.
- Bind background correlation once with `bind_log_context` or
  `set_log_context`; do not interpolate entire payloads into messages.
- Do not log request/response bodies, authorization headers, cookies, provider
  keys, full prompts, or base64 data. Log sizes, counts, hashes, status,
  operation names, and safe identifiers instead.
- Message-wire repair warnings follow the same rule: retain role/index and
  content shape or length, never a user/assistant text preview.
- If a subprocess must retain an append-only stdout file, add its stream to
  `lib/log_policy.py` and register the exact path with
  `register_external_log` before opening/spawning it.
- Add tests for original-value preservation, size/count retention, correlation, storage-outage
  behavior, and the model output byte ceiling.

## Failure semantics

- A full async queue sheds records and emits one recovery checkpoint with the
  number shed; it never recursively logs one traceback per drop.
- A diagnostics sink or aggregate failure never fails a business request.
- A maintenance lock collision returns `skipped=maintenance_already_running`.
- If protected active/unmanaged files alone exceed the global budget, the
  maintenance report states `over_budget_bytes`; it does not delete an unknown
  live file to manufacture a green result.
- Logs and raw evidence preserve original content. Support-bundle exports use
  their own separately declared export policy.

## Tool result inspection

Use `python3 debug/inspect_conversation.py CONVERSATION --tool-results --no-logs`
to compare retained model content, frontend renderer inputs, canonical evidence
references and format-audit findings in one read-only pass. Narrow to an
occurrence with `--tool-call-id CALL`; repeated provider IDs retain Turn indices.
The default displays at most 20 observations with 4,096-character diagnostic
previews; `--tool-result-limit 1..256` and `--full` (16,384-character previews)
make expansion explicit. Missing model content never falls back to a frontend
caption. Multimodal replay blocks, deferred frontend previews, partial history
and incomplete async notification XML remain distinguishable. Output follows
the existing bounded JSON-safe diagnostic boundary and configured storage
backend. Raw artifacts are not fetched automatically.

The reusable Python interface is `lib.tools.result_surfaces.inspect_result_document`.
The source/format authority is `contracts/tool_result_surfaces_v1.json`; the
execution contract describes opt-in text reports and preserved structured data.

## Shared Codex sign-in evidence

Shared-source failures emit `codex_shared_login_read_failed` and the single
pre-ingress recovery emits `codex_shared_login_recovery`, both at WARNING.
The existing app/error logs and incident journal retain phase, source kind,
initial/final issue, elapsed milliseconds and recovery outcome with the ambient
request/conversation/task/trace context. Producers include no token, account,
credential path, subprocess output or parser exception text. Error envelopes
also retain the final source issue when login expiry is not established.
Healthy reads add no record; recovery adds no pool or durable store. Existing
log-family retention, size limits and duplicate occurrence accounting apply.
An isolated synthetic recovery-log probe with allocation tracing enabled took
0.102 ms, peaked at 2,721 bytes and emitted one 131-byte fields object. This
measures diagnostic production only; the native-command deadline is unchanged.

## Slow tool executions

Problem: tool time was visible in a requested Turn trace but did not create an
incident when a local edit took 30 seconds. Execution budgets are now shared
from `lib.turn_trace_contract.TOOL_EXECUTION_BUDGETS_MS`; the persisted fold,
normal executor and streaming read prefetch use the same policy. Local write
aliases and actual `find_files` are covered. Model/human waits and arbitrary
`run_command` workloads remain explicitly unbounded.

Completed or failed over-budget execution emits one `tool_execution_slow`
WARNING into the existing app/error/incident pipeline, correlated with task,
conversation and call identity. At most 16 demand-scoped phase rows retain
elapsed time, invocation count, byte and record totals: undo index/payload I/O,
file publication and file history. Phase times are inclusive when nested.
No arguments, file paths, results or exception contents enter this evidence;
healthy calls emit no record. There is no timer, pool or second incident store,
and diagnostic failure cannot change business results or durability.
Alternatives rejected: total elapsed alone cannot locate amplification;
alerting on provider thinking/shell workloads produces misleading incidents;
a private watchdog would duplicate lifecycle and retention owners.
Verification: `tests/tools/test_tool_execution_timing.py` covers the real
executor boundary, shared trace budget, redaction, failure, nested scopes,
phase bounds and unavailable sinks.


## Metric work on storage RPCs

Problem: per-RPC metric recording recreated default histogram containers and normalized every character before truncating bounded labels. Decision: normalize only the length-preserving label prefix, replace ASCII C0 characters using one compiled regular expression, and allocate histogram state only on its first locked observation. Whitespace/fallback rules, Unicode and DEL behavior, label cardinality, first bucket definitions, inclusive counts, sums and fail-open RPC recording remain unchanged. Alternatives considered: disabling or sampling metrics would lose exact observations; label caching would introduce another resident bound and lifecycle; active waiting improved latency but increased CPU and concurrent-read costs.

Measured against frozen native source 5c69659aa11c0f56437decde0c4b40fa10626079f6a7e8193300934ed99622fd: four single-caller runs each checked 610,000 exact reads including the separately profiled sample. Default SDK caller CPU decreases about 12% and mean latency 9–11%; default constructor pool capacity is four, with one caller. The direct-runtime point corpus omits the application client, so its latency must not be described as full SDK latency. Eight original 1000-conversation harness legs preserve exact stream/replay/content and integrity. Stream P99 improves in both transports but remains above one second. One reverse embedded completion P99 regresses about 4.3%; no universal no-regression or SQLite superiority claim follows.

There are no added workers, queues, per-request caches or persistent records. The compiled pattern object measures 160 Python bytes, excluding interpreter regexp-cache bookkeeping and RSS; histogram state has its original structure. Both native/client RSS in embedded observations overlap and must not be summed. Full results and limitations: [metric hot path evidence](evidence/tofudb_metrics_hotpath_admission_20261008.json).

Explicit provider-payload capture
---------------------------------
Problem: valid but incorrect output cannot be diagnosed from redacted metadata.
Decision: `lib.llm.payload_capture.capture_provider_payloads` is an explicit
context-local library scope with caller-supplied owner identity, label and
positive byte/event bounds. It retains consumed event text before JSON parsing;
no headers, request body, shared log, storage record or HTTP capture flag is added.
Callers must derive identity from their authorized principal before exposing or
persisting snapshots. It is not authorization or replay evidence. Threads enter
their own scope; parallel async contexts remain isolated. Close, clipping,
malformed input and observer failures preserve incomplete coverage. Observer
failure does not alter delivery. `coverage_complete` concerns consumed payload
text within the scope, not original network bytes or all HTTP attempts.
Alternatives: global raw logging mixes unrelated requests; post-parse messages
cannot establish which side changed the text. Retention is opt-in memory only:
payload bytes stay within `max_bytes`, and event/stream metadata each stay within
`max_events`; callers dispose the returned scope/snapshot. One Python allocation
probe with 1,000 inputs, 16 KiB/32-event bounds retained exactly 16 KiB/32 events
and peaked at 22,888 traced bytes including a detached snapshot. This measures
that probe, not process RSS or default transport overhead.
