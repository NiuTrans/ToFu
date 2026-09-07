# Logging and incident diagnostics contract

This document is the operating contract for Tofu diagnostics. The policy
registry in `lib/log_policy.py`, the incident record schema in
`contracts/log_incident_v1.schema.json`, and the OpenAPI metadata on
`GET /api/v1/logs/diagnostics` are the machine-readable authorities.

## Invariants

1. A request thread never performs durable application-log I/O. It stamps
   context, applies flood control, and uses a bounded non-blocking queue.
2. Every core durable stream has a per-file ceiling, backup count, family
   budget, retention age, and priority in `lib/log_policy.py`. A direct child
   stdout file outside `logs/` must call `register_external_log`; an
   unregistered append-only log is a defect. Managed files are `0600` and the
   direct log directory is `0700`; maintenance migrates older permissive modes
   without changing unrecognized operator-owned files.
3. WARNING and above produce two views: redacted human evidence and a compact
   JSONL incident index. Repeated records may be physically coalesced, but
   `occurrence_delta` preserves the admitted aggregate count. CRITICAL is never
   coalesced.
4. Durable formatters redact credential-shaped values and bound each physical
   record. Structured audit/event fields are recursively redacted and bounded.
   Raw SSE capture is explicit opt-in, still credential-redacted, and bounded.
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

## Data flow

```text
producer
  -> request/principal/task context
  -> credential redaction + 16 KiB record bound
  -> WARNING+ duplicate coalescer (burst + power-of-two/heartbeat checkpoints)
  -> bounded non-blocking queue
       -> app/access/error/vendor/frontend evidence families
       -> incident.jsonl (DB-independent structured index)
       -> optional DB fingerprint aggregate (fail-open acceleration only)
```

`error.log` remains human-readable evidence. `incident.jsonl` is the preferred
machine index. The database aggregate and differential digest are caches: when
storage is unavailable, `/api/v1/logs/digest` falls back to the incident index.

Routine request rows and explicitly handled client failures stay in the access
or DEBUG plane. In particular, an incompatible browser extension emits one
structured `browser.protocol_rejected` WARNING; repeated browser-poll 4xx rows
retain only first/power-of-two/five-minute access checkpoints and do not
duplicate that fact into `app.log`, `error.log`, or the incident index. Exact
request totals remain in bounded-cardinality HTTP metrics.
Other client and server failures retain their normal WARNING/ERROR severity.

## Operator workflow

Start every broad investigation with the bounded report:

```bash
python3 -m lib.log_diagnostics --pretty
python3 -m lib.log_diagnostics --conversation-id <id> --max-bytes 16384
python3 -m lib.log_diagnostics --task-id <id> --window-hours 2
```

The HTTP equivalent is admin-only:

```text
GET /api/v1/logs/diagnostics?window_hours=24&max_items=20&max_bytes=32768
GET /api/v1/logs/diagnostics?request_id=<id>
GET /api/v1/logs/diagnostics?conversation_id=<id>
GET /api/v1/logs/diagnostics?task_id=<id>
GET /api/v1/logs/diagnostics?trace_id=<id>
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
turn-trace contract (`GET /api/v1/tasks/<task_id>/trace`; see
[TURN_TRACE_CONTRACT.md](TURN_TRACE_CONTRACT.md)).

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
- Add tests for redaction, size/count retention, correlation, storage-outage
  behavior, and the model output byte ceiling.

## Failure semantics

- A full async queue sheds records and emits one recovery checkpoint with the
  number shed; it never recursively logs one traceback per drop.
- A diagnostics sink or aggregate failure never fails a business request.
- A maintenance lock collision returns `skipped=maintenance_already_running`.
- If protected active/unmanaged files alone exceed the global budget, the
  maintenance report states `over_budget_bytes`; it does not delete an unknown
  live file to manufacture a green result.
- Secret redaction is defense in depth, not permission to share logs blindly:
  support bundles and raw evidence can still contain private user content.
