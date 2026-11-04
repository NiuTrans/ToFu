# Logging and incident diagnostics contract

This document is the operating contract for Tofu diagnostics. The policy
registry in `lib/logging_pkg/log_policy.py`, the incident record schema in
`contracts/log_incident_v1.schema.json`, and the OpenAPI metadata on
`GET /api/v4/logs/diagnostics` are the machine-readable authorities.

## Invariants

1. A request thread never performs durable application-log I/O. It stamps
   context, applies flood control, and uses a bounded non-blocking queue.
2. Every core durable stream has a per-file ceiling, backup count, family
   budget, retention age, and priority in `lib/logging_pkg/log_policy.py`. A direct child
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
See `system_audit_rotation_preflight_admission_20261009.json`.

## Task startup timing anchors

`measure_task_startup` retains the existing closed-cardinality histogram and
adds paired `[TaskStartupTiming]` JSON records when its caller supplies explicit
identity. The turn command records `input_build`, `turn_persist`,
`executor_start` (claim, accepted-config re-read and the nested dispatch stages)
and `acknowledgement`. Conversation dispatch records cleanup, context
construction, registration, preparation, binding and submission. Inside
`registration`, a task with a model reference records `registration_route`
(owner authority read and candidate planning) and `registration_mint`
(credential resolution, slot preparation and the single route-group
publication), and a durable task records `registration_checkpoint` (the
durable-at-birth row); they inherit the enclosing identity and are inclusive,
so never add them to `registration`. The physical
worker records worker entry, its nested workspace binding and `input_translation`
(the join of a deferred send translation). Before the first round the root
invocation records `startup_config` (prelude, provider binding, model config),
`startup_resume`, `startup_project`, `startup_tools`, `startup_memory`,
`startup_context` (context injection) and `startup_history`; a stage skipped by an
abort gate emits nothing. Inside `startup_memory` the prefetch records
`memory_list` (the time startup spent obtaining the eligible metadata listing,
including any wait for a shared scan), `memory_rank` (BM25 shortlist and
confidence gate) and `memory_hydrate` (bounded bodies of the selected IDs;
absent when nothing was selected). An accepted attempt starts that listing at
acceptance (`lib/tasks_pkg/memory_listing.py`), so its `memory_list` measures
only the wait for it. The `memory_list` completion carries `listing`
(`early` for that listing, `inline` for one run inside the stage) and
`listingMs`, the listing's own duration: `listingMs` minus `durationMs` is the
share earlier startup hid, and an inline listing hides nothing. A
nested stage inherits the enclosing stage's identity, so the three sub-stages
are paired with the same task without passing identifiers into the memory
package. Each start/complete contains only
bounded conversation/Turn/Attempt/task/owner identifiers, a fixed stage, wall-clock
`observedAtMs`, completion outcome/monotonic `durationMs` and the closed
completion details above (`_TASK_STARTUP_DETAILS` in `lib/observability.py`).
Identity never becomes metric labels. No config, prompt, file list, error text,
polling loop or new durable store is introduced. Existing model/tool/compaction execution clocks
and browser perception receipts cover the subsequent boundaries.

Workspace isolation records `workspace_snapshot` and each `workspace_snapshot_observation`, `workspace_input_ref`, `workspace_metadata`, `workspace_checkout` and `workspace_registration`. Repository/destination admission has separate `*_lock_wait` and `*_lock_hold` stages; wait starts before lockfile filesystem I/O, and hold starts only after admission. Nested stages inherit only the outer caller's explicitly bounded identity through a reset-on-exit context, never config or paths. Startup histograms retain finite buckets through 600 seconds; longer observations remain in the count/sum and infinity bucket. **Problem:** the former inclusive binding/materialization timers exposed latency but could not attribute it to contention, snapshot CPU/I/O or checkout. **Decision:** reuse the paired anchors and closed stage registry rather than another watchdog, lock-owner database or polling thread. Missing completions still do not prove a hang, and these new records certify production coverage only after code reload. Tool `workspace_admission` remains an inclusive total, not pure lock wait; `undo_journal_lock_wait` measures journal admission while `post_commit_wait` and `post_commit_callback` distinguish ordered presentation delay after release.

Workspace tools distinguish registry-lock acquisition (`workspace_registry_wait`), lockfile filesystem setup (`workspace_lock_io`), local condition sleeping (`workspace_local_wait`) and blocked cross-process flock backoff (`workspace_flock_wait`). The sleep timers are measured backoff, not blocker identity or the total kernel-call duration. Lockfile filesystem work runs outside registry ownership; an active-opening counter under the same registry lock fences fork until each FD is registered or closed. Fork waits on the registry condition (releasing it while waiting), then retains the lock across fork. Cancellation cannot interrupt a filesystem syscall, but unrelated edits can now acquire/release their leases while it is blocked. No namespace migration, new thread, queue or durable lock-owner registry is introduced. A call scope keeps its first 16 phase names; a later name is omitted rather than growing state. The owner-defined vocabulary is larger, but only tools with an execution budget open a scope and none of them takes a workspace lease (`run_command` dependency setup can, but `run_command` has no budget), so one timed call records at most 15 names. Slow-tool records are completion-only and over-budget-only, not an all-call latency population. Specs: `tests/observability/test_task_startup_metrics.py` and the real-Git startup evidence case in `test_integration_delivery.py`.

A record carries only identity that exists when it is written. A stage's start
keeps the identity known at entry; identity the stage creates joins its
completion and the stages opened after it (`measure_task_startup` yields
`bind_identity`). `input_build` and the `turn_persist` start precede the Turn and
carry conversation/owner identity only; the `turn_persist` completion carries the
persisted Turn and Attempt, as do `executor_start`, `acknowledgement` and the
dispatch stages. A join anchors a command window on that completion: the earlier
records share its request `rid` and thread, and `observedAtMs - durationMs` is the
persist start. A failed persist names no Turn (a lane-busy conflict's live Turn
belongs to another command). Blank identifiers are omitted, never written as a
placeholder. Attempts started by `create_attempt`, `execute_plan`, queued
activation or recovery emit no command-window records; their first anchor is the
dispatch `cleanup`.

Problem: command-window records carried only conversation and owner identity,
so a join attached them to Turns by timing. Over the retained 2026-10-31/11-01
logs the audit's timing join left 9 of 66 settled attempts unattached: 6 Turn
commands submitted as `inputTurn` by background commands, Swarm drivers or
schedules (no `input_build`; 4 had no request `rid`), 2 attempts started outside
Turn creation, and one pseudo-attempt `"unknown"` merging child invocations whose
blank Turn/Attempt fields had been bounded to that placeholder. Decision: a stage
admits identity created inside it, `turn_persist` binds the Turn/Attempt it
persisted, and blank values stay absent, so those 6 command windows join by
Attempt identity. Alternatives rejected: allocating the Turn ID before persistence
invents identity the authority has not committed (an idempotent replay returns
the existing Turn); logging the client `commandId` adds a non-Turn identifier and
still needs a second join; a separate post-persist record duplicates the boundary
the completion already marks; a tighter timing window cannot separate concurrent
commands.

Problem: create-to-run timing conflated dispatch preparation, worker scheduling
and workspace isolation, leaving a bare waiting placeholder during filesystem
work. Decision: publish the committed physical-worker phase before workspace
binding, retaining the binding-before-tools fence and failure settlement; pair
stage starts with completions so a blocked stage leaves an entry witness.
Alternatives rejected: a synthetic heartbeat cannot prove progress, a longer
timeout hides the gap, and logging task dictionaries copies private content.
Nested durations are inclusive, not additive. Missing completion does not alone
prove a hang (log retention/loss still applies); existing `queue_wait` means
create-to-run, not pure executor residence. Problem: on 2026-10-27, 87% of
unattributed task time preceded the first round, split between the unanchored
command window and worker preparation (5–22 s of memory/context work per task).
Decision: extend this one family to both windows instead of adding a second
startup log. The Turn Trace names the same windows from the startup prompts as
`startup` stage spans (`docs/contracts/TURN_TRACE_CONTRACT.md`, 编排窗口与锚点身份),
so a settled task explains them without these log lines. Specs:
`tests/tasks_pkg/test_startup_timing_anchors.py`,
`test_spawn_serving_loop.py` and
`tests/conversation/test_deferred_input_translation.py`.

## Per-task Turn timing summary

Terminal trace settlement (`lib.tasks_pkg.turn_trace.finalize_trace_projection`)
logs exactly one INFO `[TurnTraceSummary]` record per task (one physical line):

```text
[TurnTraceSummary] task=<full task id> conv=<conversation id> status=done totalMs=191521 llmMs=110377 toolMs=21631 waitMs=36194 admissionWaitMs=33000 retryWaitMs=0 compactionMs=0 approvalWaitMs=0 unattributedMs=0 ttftMs=71258 rounds=6 spawnWaitMs=3194 flowStepMs=0 orchestrationMs=23319 startupMs=17200 roundPrepMs=3600 roundSettleMs=2400 finalizeMs=119 coverage=full
```

Every number comes from the fold that is frozen into the attempt's Turn Trace
(`docs/contracts/TURN_TRACE_CONTRACT.md`): the buckets and the `waitMs` split
(`admissionWaitMs + retryWaitMs + spawnWaitMs == waitMs`, and `startupMs +
roundPrepMs + roundSettleMs + finalizeMs == orchestrationMs`, that fold's own
disjoint priority attribution) are read from its persisted `summary`, so the line cannot
disagree with the Request Inspector or the API audit. The durable summary keeps
the split even when the bounded spans list dropped evidence, so API mode of
`debug/turn_trace_audit.py` is exact after these lines rotate away. A rebuilt terminal projection (CAS rebase, retained-receipt
retry) does not log again. The record is INFO — below the WARNING+ duplicate
coalescer — and carries the full task id, so distinct tasks are never merged;
per-module INFO flood insurance (`LogBudgetFilter`) still applies. Keys are
`key=value` tokens; parsers must ignore unknown keys. Aggregate retained lines
without API access with `python3 debug/turn_trace_audit.py --logs logs/`; re-fold
exported event logs with the checkout's fold (startup stages and remaining gaps by
phase) with `--events FILE ...`.

A settlement whose event read failed has no fold to report. Its record carries
only the coverage marker and no number an aggregator could sum, so `--logs`
skips it instead of counting the whole run as unattributed:

```text
[TurnTraceSummary] task=<full task id> conv=<conversation id> status=done coverage=partial coverageReason=event-read-failed
```

If a rebuild of the same terminal projection then reads successfully, that
rebuild logs the normal record once. The read failure itself is the WARNING
`[TurnTrace] Sidecar event read failed task=<full task id> code=... message='...'
retryable=...`, rendered by `describe_storage_failure` and bounded to 400
characters, because a bare storage code is not a diagnosis.
Specs: `tests/turn_lifecycle/test_turn_trace.py` (summary line, coalescing).

## Round preparation timing

Problem: ROUND_START -> logical MODEL_REQUEST_START reached p99 27 s (max
140 s) with no log line; the pre-request durability barrier waited silently on
slow storage writes. Decision: observability only; durability, barrier,
ordering and retry semantics are unchanged, and no task event or phase is
emitted (it would queue behind the durable FIFO being measured).

- INFO `[RoundPrepTiming] {json}` (`lib/tasks_pkg/round_prep_timing.py`): at
  most one per root round, logged by the logical request start when the window
  is >= 1 s; faster rounds and rounds whose request never starts log nothing.
  Keys: `task_id` (full), `conversation_id`, `turn_id`, `attempt_id`,
  `user_id` when known; `roundNum` (1-based, = MODEL_REQUEST_START.roundNum;
  ROUND_START.roundNum is this minus one), `requestTag`, `spanId`,
  `observedAtMs`, `totalMs`, `stepsMs` (always `round_open`, `hygiene`,
  `compaction`, `inputs`, `admission`, `build_body`, `accumulator`, `barrier`,
  `dispatch`, `receipts`, `request`, `other`; self time, so nested steps never
  double count and the values sum to `totalMs`), `inputsMs` (per-input
  breakdown when inputs ran, including `remember`), `build_bodyMs.snapshot`
  (Request Inspector snapshot construction and hand-off; a managed root lane
  and a child invocation's evidence lane only enqueue it),
  `barrierMs` (`sink`: the domain checkpoint write-ahead, e.g. the Swarm agent
  checkpoint; `events`: present only when a failed FIFO head forced a repair
  wait), `barrierPendingEvents` (accepted task events still undurable when the
  request was dispatched; for a child, its Inspector snapshots) and
  `barrierAwaitedEvents` (how many the pre-request checkpoint waited for: 0
  unless it repaired a failed head).
  `round_open` is the ROUND_START append plus the round phase; `accumulator`
  the streaming tool accumulator; `dispatch` the fallback/credential wrappers
  between the barrier and the receipt check; `request` the stream entry through
  the MODEL_REQUEST_START and waiting-phase appends (the logical request start
  names this tail). `other` is only time no boundary named (a path that
  bypasses one). The native context policy and swarm sinks carry no record.
- WARNING `[EventBarrier] slow task=<full id> conv=<id> checkpoint=<phase>
  checkpointRound=<n> elapsedMs=<ms> pendingEvents=<n>` (event
  `event_barrier_slow`): once per awaited `checkpoint_invocation` barrier
  (`tools_in_flight`, `ready`, `terminal_delivery`; `model_in_flight`, and a
  child invocation's `ready`, only while repairing a failed head) still waiting
  after 1 s (`checkpointRound` is the
  0-based round index); the barrier keeps waiting. Only then is it followed by
  INFO `[EventBarrier] released ... elapsedMs=<total> awaitedEvents=<n|->
  persistCalls=<n|-> persistMs=<ms|-> queuedMs=<ms|-> outcome=<ok|ExceptionClass>`.
  The three cost fields cover the batches the barrier awaited (`-` without
  a lane): `persistCalls` counts their storage persist calls, transient
  retries included (merged progress frames share one call, and so does a run
  of queued events persisted together); `persistMs` sums
  each batch's first storage dispatch to its acknowledgement, retries and
  backoff included (storage latency); `queuedMs` sums each batch's wait at
  its FIFO head for a delivery pump, from acceptance or the previous batch's
  settlement or repair (local queuing: pump scheduling, lock and GIL waits).
  The remainder of `elapsedMs` is publication callbacks, barrier wake-up and
  any head already in flight before the barrier began. A refused barrier
  reports the cost it observed.
- WARNING `storage.v2 slow <query|command> <operation> succeeded
  elapsedMs=<ms> attempts=<n> task=<id|-> conv=<id|->` (event
  `storage_rpc_slow`, `lib/storage/v2_client.py`): a successful call >= 2 s,
  at most once per kind+operation per 30 s (`RetryLogThrottle`); identity
  comes from the ambient log context. `tofu_storage_rpc_seconds` keeps exact
  counts.
- `[Timing] TTFT=<s> (context-ready→first-token), request=<s>
  (stream-entry→first-token, after build_body+barrier)`: the request clock
  starts after body building, the barrier and receipt checks. This line, the
  `[Timing:<tid>] prep=` line and `[Timing:<tid>] queue_wait=` (from the
  create-time `_t_created_monotonic`) subtract monotonic readings only; a
  wall-clock step cannot produce a negative or inflated duration.

Alternatives rejected: a user-visible storage-wait phase cannot be seen while
the FIFO it rides is stalled; a barrier timeout would change durability.

Problem: agents waited 10-69 s per round at `[EventBarrier]` on a busy task,
and `elapsedMs` could not say whether storage calls or local queuing (pump
churn, lock and GIL convoys) spent that time. Decision: every queued batch
keeps a small timing record (persist calls, dispatch-to-acknowledgement time,
head-of-FIFO wait) that the coordinator sums for the batches a barrier awaited
([task event delivery](TASK_EVENT_DELIVERY.md#pump-scheduling-and-coordinator-lock-scope));
the barrier holds only those records, never the batches' callbacks or inputs.
Alternatives rejected: summing acceptance-to-dispatch per batch counts the
wait behind earlier batches' storage calls as queuing, growing with backlog
depth; one log line per persist attempt floods the log; an aggregate metric
loses the attribution to one task's barrier.

Problem: once the pre-request checkpoint stopped awaiting request diagnostics
([task event delivery](TASK_EVENT_DELIVERY.md#pre-request-checkpoint-never-awaits-diagnostics)),
one `barrier` number could no longer separate a domain write-ahead from event
durability, and `barrierPendingEvents` stopped meaning "events awaited".
Decision: `barrier` keeps the whole checkpoint wall time (near zero for root
tasks without a sink); `barrierMs.sink`/`barrierMs.events` name what remains,
the pending and awaited counts are separate facts, and the snapshot hand-off
is named inside `build_body`. Alternatives rejected: dropping the
`barrier` key breaks existing parsers; reporting only the event wait would
hide the Swarm/Evolution write-ahead that still precedes every paid call.

Problem: on 2026-10-27, `other` was 13% of root round preparation (p50
193 ms, max 2.3 s) — unwrapped calls had no name. Decision: a
`round_prep_lap(task, name)` boundary after each unwrapped call names the
not-yet-charged time since the previous boundary (a no-op inside an open step),
and `finish_round_prep(tail=...)` names the final stretch, so instrumentation
adds one line instead of re-indenting the measured code. On the LLM replay
suite (18 rounds) `other` fell from 52.1% to 0.0%; the ROUND_START and
MODEL_REQUEST_START event appends (`round_open`, `request`) were each about a
quarter of preparation. Alternative rejected: wrapping each call in
`round_prep_step` re-indents code that other latency work edits and names
nothing more. Each lap costs one monotonic read and a sum over at most a dozen
steps.
Specs: `tests/tasks_pkg/test_round_prep_timing.py`,
`tests/storage/test_storage_v2_client.py` (slow RPC warning).

## Repeated tool-call evidence

Problem: content-identical calls can represent independent effects. Signature-based
rejection/collapse previously stopped streams before terminal usage and could lose
operations. Decision: new provider attempts preserve each response position and
never emit signature-flood rejection or salvage markers. Stable-slot retransmission
and slot-splitting diagnostics remain with the stream assembly owner; logging does
not decide execution identity. See the [execution policy](contracts/TOOL_EXECUTION_POLICY.md).

Historical `ProviderToolCallFloodError` remains `tool_loop` with manual retry only:
it does not trigger fallback, automatic turn replay or route-health penalties.
Existing `toolCallEvidence` and `rawArchiveRef` records remain readable. Their
position counts and redacted parser counters cannot establish whether a model or
an upstream gateway generated the repetitions. An archive reference identifies a
capture attempt, not proof of successful storage or continued retention; the
archive catalog remains the availability/integrity/truncation authority.
Alternatives rejected: continuing to log each identical input as an incident
mislabels legitimate actions; dumping arguments or IDs creates sensitive-data noise.

The conversation inspector's `--request-evidence` flag follows the selected
authenticated application API to show references for the last four referenced
tasks and up to four archives of each latest request. Reads share its existing
time/request/byte budget; errors and expiration are explicit. It never prints
request or response bodies. Offline SQLite inspection does not switch authority.
Use this metadata to fetch a bounded archive chunk through Request Inspector
when raw-frame verification is needed. Terminal fatal paths also run the normal
tool-round sweep before DONE/persistence, so unadopted announcements stop showing
as running; retries and external/flow-owned lifecycle retain their owners.

## Raw archive persistence stays off the provider path

Problem: transports finished a Request Inspector capture on the provider
call's thread, and the async relay finished it on the event loop, before the
model result returned. Compression, hashing and `raw_archive.put` (whose
storage client replays refusals with backoff, then replayed again by the
archive writer) therefore sat on every round's critical path. Measured
2026-10-23 after a restart: 3,251 refused archive commits against 2,443
provider rounds; a refused commit held its provider call p50 47 s, p90 290 s
(27,349 s in total), and a capacity refusal cannot succeed on replay.
Decision: `RawArchiveCapture.commit` is a non-blocking handoff to one
background writer in `lib/raw_archive.py` that persists captures in
submission order. One writer, because the authority serializes commits;
parallel writers would contend with the event and turn writes sharing it.
Pending captures are bounded by resident bytes (request document plus the
response spool's in-memory part, 64 MiB); an empty queue always admits one
capture, and beyond the bound a capture is dropped and counted. A capacity
refusal (`database_resource_exhausted`) is never replayed by the writer and
pauses capture process-wide for 30 s, doubling per refused probe up to
15 min; exactly one commit probes when a window ends, and success resumes
capture. Exit drains for at most 5 s. Dropped, skipped and resumed counts are
logged once per episode, so missing evidence stays visible; an archive
reference still proves only a capture attempt. Alternatives rejected:
retrying in place keeps the stall on the user's path; a per-call thread or
the demand executor would fan out against an authority that is already
refusing; silently disabling capture hides missing evidence. Specs:
`tests/storage/test_raw_archive.py`.

## Swarm sub-agent rounds are not raw-archived

Problem: child invocations (swarm agents) run the same round loop, but their
task carries no Turn/Attempt identity, so `RawArchiveCapture.create` declines
and the Request Inspector's composite id (`<parent>#agent:<child>`) lists no
`rawArchives`; their wire evidence exists only as reconstructed round evidence.
Decision (2026-10-28): keep it that way for now. Swarm lanes completed 4,777
model rounds against 1,279 root rounds in the retained 2026-10-27/28 logs, so
archiving them would multiply writes through the single serialized archive
writer about 4.7x and, under the oldest-first reclamation below, retire the
parent conversation's own archives within its 1,792-archive ceiling. Revisit
with a measured budget and an explicit capture scope. Alternatives rejected:
reusing the parent's Turn/Attempt with the composite id for every child round
(the volume above, unmeasured retention impact); a per-run opt-in flag (a
second capture policy without an owner decision). A failure-only capture for
child rounds is the smallest follow-up.

## Raw archives are reclaimed oldest-first

Problem: raw archives left the native authority only through conversation
deletion and superseded-Attempt cleanup. Every put counted against the
65,536-archive owner ceiling, including the body-less `quota_exhausted`
placeholder written once the tenant byte budget is full, and at the ceiling
every put was refused (`database_resource_exhausted`). In production the
4 GiB budget (`TOFU_RAW_ARCHIVE_BUDGET_MIB=4096`) was full by 2026-10-20, so
every later archive was a placeholder, and the owner ceiling refused from
2026-10-22 11:57 (engine counter `raw_archive_owner_capacity=25425`,
conversation ceiling 0). With the writer pause above, the Request Inspector
recorded nothing.

Decision: raw archives are reconstructible diagnostic evidence, not durable
user state. Before a live `raw_archive.put` would meet the conversation
ceiling (1,792), it retires that conversation's oldest archives. Before it
would meet the owner ceiling, or scrub a body that fits the budget once older
bodies go, it retires the owner's oldest archives in
`(created_at_ms, archive_id)` order from the owner age index
`raw_archives_by_owner_created`. Reclamation, the new archive and every
counter commit in the put's transaction, at most 64 archives per put; what is
still exceeded keeps the previous refusal or scrub, and the next put continues.
Bodies scrubbed regardless (free-space floor, 16 MiB per-Attempt budget,
parts above the budget) never reclaim for bytes; replays and offline restores
never reclaim. Archives written before the age index existed are indexed from
the conversation removal index by a persistent owner cursor, 512 rows per live
put (128 puts cover 65,536), before that put reclaims. An owner whose first
archive arrives with this index needs no backfill. Reading a reclaimed archive
through `GET /api/v4/tasks/{taskId}/raw-archives/{archiveId}/{part}` returns an
empty chunk with `state: "expired"`; retained chunks carry
`state: "retained"`. The SQL sidecar keeps its scrub-only budget; it has no
count ceiling, so it never refuses. The byte budget is tenant-wide while an
owner reclaims only its own archives (TODO(enterprise): per-owner budgets).

Measured with `raw_archive::tests::lifecycle::measure_reclaiming_put_cost`
(debug build, in-memory deterministic VFS, loaded 4-CPU host): 9.5 ms per put
below the bounds, 13.7 ms per put that reclaims one archive, both one WAL write
per commit; the first backfill put (512 rows) took 45 ms and 2 writes.

Alternatives rejected: raising the ceilings moves the cliff and lets the store
grow without a lifecycle; a TTL or maintenance-driven prune operation adds an
operation, a schedule and a second policy, and still refuses between passes;
ordering owner reclamation by the existing conversation index would retire
evidence in conversation-ID order rather than by age; refusing until the
backfill completes would never complete it, because a refused put rolls back
its backfill. Specs: `raw_archive::tests::lifecycle`,
`raw_archive::migration::tests`, `tests/storage/test_raw_archive.py`.

## 搜索诊断与统计

**Problem:** 零候选在工具执行统计中仍是成功，无法统一区分目录缺失、可见性过滤和检索故障。**Decision:** 本地 `search_tools` 与程序内 `catalog.search` 每次实际检索向现有有界异步 `logs/audit.log` 写一条 `tool_search_outcome`（schema_version=1），成功也记录以提供分母。`outcome` 区分 matched / empty / error；`reason` 记录精确名称缺失、已可见、歧义、namespace 缺失/过滤精确名、翻页耗尽、空目录、全部已披露、无匹配、能力不可用的既有 reason_code、参数错误或 retrieval_exception。fail-open 即使给出恢复目录仍计为检索错误。

记录保留至多 512 字符查询（超限显式标记）、128 字符 namespace/身份、模型、owner、会话/task/attempt、call_id、llm_round、目录名称指纹、可见/已披露/隐藏数量、返回数量和耗时，额外保留搜索器返回的 resolved_name 与 repeated_query。可见/已披露集合按搜索器的大小写无关规则投影且互斥，目录中的单词名称也识别为精确名查询。名称形状和当前目录成员事实仅作证据；不得由此直接认定上下文污染或幻觉。使用 task/attempt/round 回查既有 Request Inspector 请求，再核对上下文来源；不复制提示词、schema、工具结果或异常消息。审计失败不改变工具结果、异常传播、审批或 fail-open 行为，不新增缓存、数据库或保留周期。**Alternatives:** 只记精确 miss 漏掉自然语言和程序入口；把零结果全当污染则混淆正常过滤，均不采用。

`python3 scripts/report_tool_search_outcomes.py logs/audit.log [旧日志...] --query steer_agent_run` 汇总保留记录的 matched/empty/error、按模型/入口/原因分组、每模型命中/空结果/检索错误比例、高频非命中查询和最近 20 条非命中记录。already_visible / all_tools_disclosed / page_exhausted 单列为 redundant_lookups，不等同检索故障；repeated_searches 只计有搜索器重复证据的调用。最近记录按日志时间排序，重复文件参数及同文件 symlink 不重复计数；无有效时间的行按读取顺序排在有效时间之前。脚本流式读取，单行 32 Ki 字符；原因、模型、查询各保留至多 1,024 身份加 overflow，超限由 aggregation_overflow 声明，总样本计数仍完整。原因/模型输出前 50 组，查询前 20 组；异常/过深 JSON 行单独计数。统计范围仅为本地入口、现存日志和成功入队的记录，不包含 hosted provider 搜索、不保证完整历史。既有异步审计队列与轮转限制仍生效。

256 个合成目录名称、1,000 次诊断构造（含 JSON 编码）本机测量：中位 0.129 ms、p95 0.168 ms；单条 645 bytes（不含 sink 时间/请求字段），Python 临时分配峰值 40,834 bytes。测量不含检索与磁盘写入；队列、文件和日志家族仍使用既有资源上限。 互斥计数以单次目录遍历替代多份临时集合；同机 256 名称/1,000 次 JSON 编码对比，优化前/后中位均为 0.074 ms，p95 为 0.080/0.079 ms，单次 Python 临时分配峰值为 40,378/18,083 bytes。新增汇总状态只存在于离线命令，随命令退出释放。

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
and endpoint recursion guard still apply. Urgent flushes arriving during a flight
coalesce into one immediate follow-up after settlement, without waiting for the
normal 15/60-second relay clock or introducing retries. Each observer batch selects its longest
entry before limiting. Each type emits one record per five-second window, plus
one immediate promotion if it first crosses one second. Suppressed entries retain
their longest complete evidence (including its original context), drained by one
demand-only timeout at the window deadline even if no further stalls occur.
`suppressed` counts entries folded since the prior report, including the retained
representative when it is drained. Hiding, pagehide and composition disposal first
take completed observer records and drain pending evidence, then disconnect and
cancel the timeout. Resume/pageshow reinstalls observers without historical entries.
At most two observers/checkpoints, one timeout and one semantic interaction are
retained; idle creates no timeout or request and copies no transcript/payload.
Resource measurement and verification live below; no polling heartbeat is added.

Evidence includes page-monotonic start/duration/delivery delay, current canonical
main-lane conversation/turn/attempt/task and latest tool-call identifiers,
transport status, and, where the browser supports it, the slowest long-frame
script path/function/character position/invoker type, script duration, blocking
time and forced layout time. Character position identifies the script entry,
not necessarily its slowest nested function (Chrome's
[LoAF attribution contract](https://developer.chrome.com/docs/web-platform/long-animation-frames)).
Script URLs omit query/fragment. Optional tool/attempt context is omitted before
script attribution to preserve intact JSON within the 800-character relay budget.
Identifier labels are capped at 64 characters. A capturing click/activation-key
listener retains only a semantic interaction label (conversation-switch, link,
disclosure or control), monotonic time and optional destination conversation ID.
Interactions preceding entry start within 30 seconds, or captured inside the
measured frame/task interval, are attached. `interactionAgeMs` is signed relative
to entry start; a negative value means the frame began before the interaction.
Activations delivered after entry end, raw key values, element text, link URLs
and DOM references are never retained.
Context is sampled **after** the stall, can
already have advanced or settled, and does not establish that the latest tool
caused it. The browser reports `startAtMs` from its performance time origin plus entry
start, so a delayed batch still identifies when blocking began; browser/server
clock skew/suspension remains possible. `observedAtMs` separately records wall time
at observer delivery; session and server ingestion time link the batch.
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

Problem: first-entry limiting hid a later serious stall, and visibility changes
discarded suppressed maxima. Decision: select each batch maximum, allow one severity
promotion and drain retained evidence on a demand deadline and lifecycle edges.
Alternatives rejected: waiting for another stall can lose the final hang; unbounded
severity bypass can amplify a stalled page's logging; polling adds idle work.
A Node 20.19 synthetic 10,000-callback probe measured 24.97 ms wall / 56.96 ms CPU,
one immediate record, one demand timer and one final drain record. This measures
diagnostic callbacks, not application latency or browser rendering. The timeout
is reused without rescheduling for same-deadline bursts. Focused tests cover batch
maxima, promotion, deadline/visibility/disposal drains, stale deliveries, bounded
attribution and content-free interactions; the visual gate exercises real Chrome
observer recovery and successful relay delivery through an isolated local app.

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
`{"source":"tofu-db","timestamp":...,"level":...,"target":...,"message":...}`
so the mirror routes without parsing free text: `error`/`critical` levels cross at
matching severity, and the `integrity_fence` target crosses at any level
(fence refusals are first-class evidence); other levels stay at DEBUG.
`timestamp` is the engine's wall-clock emit instant in RFC 3339 UTC with
milliseconds and a `+00:00` offset, the incident index form (for example
`2026-11-01T09:27:30.172+00:00`). A duration record (`authority_hold`, a
lifecycle `state=done`) is emitted when its interval ends, `elapsed_ms` after it
began; an `authority_hold` message also splits that time into exclusive phases
and stages ([format and phases](RESOURCE_CONTROL.md#demand-driven-root-acceptance-and-workers)). Mirrored records repeat it as `at=` (`at=-` from an engine without the
field) because their own record time is when the drainer read the line.
Human-oriented opt-in dumps (`TOFU_DB_FSYNC_STATS` and the `*_stats:`/
`listener_metrics:` lines) mirror at DEBUG, and non-JSON lines forward
as-is at INFO. CLI contract output stays on stdout as key=value/JSON.
Engine observation (health/metrics) goes through the authenticated control
operations, not log scraping.

Problem: raw engine lines reached `server-console.log` without a time, and the
mirror persists only error/fence records, so an `authority_hold` could not be
aligned with the client-side `storage.v2 slow` warnings in `error.log`.
Decision: the engine stamps every record when it emits it. Alternatives
rejected: stamping in the Python drainer dates the read rather than the emit (the
drainer lags under GIL pressure), breaks the verbatim raw forward and cannot cover
the embedded transport, which has no drainer; local time with an offset needs
per-platform calendar calls while `app.log` local times carry no offset anyway;
epoch milliseconds are unreadable in a console; a calendar crate would be a new
engine dependency for one formatter.

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
python3 -m lib.logging_pkg.log_diagnostics --pretty
python3 -m lib.logging_pkg.log_diagnostics --conversation-id <id> --max-bytes 16384
python3 -m lib.logging_pkg.log_diagnostics --task-id <id> --window-hours 2
```

Problem: traversing current then rotated files retained the last 200 scanned
checkpoints, hiding newer failures; a byte-limited scan also looked like a complete
requested time window. Decision: retain the newest 200 timestamps in a bounded
heap and expose `summary.scan_truncated`, `scan_read_errors`, and scoped
`correlation_occurrences`. Alternatives: sorting every checkpoint retains
unbounded timeline state; increasing the default scan hides the coverage limit.
`scan_truncated` means retained source bytes were omitted, not that producers lost
events. `truncated` still describes the output JSON ceiling. Read failures remain
visible even when legacy error-log fallback succeeds. Correlation counts use
admitted occurrence deltas after all owner/selector filters; legacy text reports
null because it cannot prove structured attribution. No field's absence alone
establishes a defect: a process-wide engine event can have no request identity.
For a larger offline evidence window, use `--scan-bytes 67108864` (64 MiB maximum);
retention and scan limits still prevent certifying an arbitrary historical window.
Conversation storage failures record the operation, declared route conversation,
storage code/operation ID, private diagnostic and original exception tuple in the
existing incident pipeline; HTTP 500 disclosure stays at the stable public code.
HTTP 500 cause records from `api_internal_error` and the shared conversation
storage/contract adapter use the existing `tofu_no_coalesce` bypass, so equal
failures on different requests retain individual correlation. Aggregate counts
alone were rejected because they retain only a representative request ID.
The bounded queue, record ceiling and rotating retention remain authoritative.
Root invocation retires both its copied execution context and outer worker
correlation, including rejected admission and failed kickoff; child invocation
restores its parent's context. Clearing only inside `copy_context` was rejected
because it leaves stale caller fields available to the next pooled task.

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
python3 -m lib.logging_pkg.log_diagnostics --maintenance dry-run --pretty
python3 -m lib.logging_pkg.log_diagnostics --maintenance apply --pretty
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
stalls use `lib/server/server_fault_watchdog.py`, retaining Python frame objects
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
`lastWorkerProcessExit` when it retires a generation, planned or failed. The
observation itself is read-only and identifies no signal sender; only after
the record is saved does the manager reap that exact child (see
[infra runtime](modules/infra_runtime.md)). It remains unknown if the PID
disappeared, was reused or the host lacks Linux `/proc`.
A stale probe error cannot replace a known fatal signal; planned restart,
health-wedge and shared-cgroup OOM accounting retain their existing authority.

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
  `lib/logging_pkg/log_policy.py` and register the exact path with
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
file publication, file history, and the durable acknowledgements that bracket a
side effect (`effect_intent_ack` before the handler runs,
`effect_reconciliation_ack` before a file publication is reported). Phase times
are inclusive when nested.
No arguments, file paths, results or exception contents enter this evidence;
healthy calls emit no record. There is no timer, pool or second incident store,
and diagnostic failure cannot change business results or durability.
Alternatives rejected: total elapsed alone cannot locate amplification;
alerting on provider thinking/shell workloads produces misleading incidents;
a private watchdog would duplicate lifecycle and retention owners.
Verification: `tests/tools/test_tool_execution_timing.py` covers the real
executor boundary, shared trace budget, redaction, failure, nested scopes,
phase bounds and unavailable sinks.

## Workspace lease wait evidence

Model tool calls take no workspace lease since 2026-10-28
(`docs/runbooks/GIT_WORKSPACE_GUARD.md`, "Tool calls take no workspace
leases"), so the `[CommandAdmission] fence` line and command claim replay are
gone. The `[WorkspaceLease] wait` line remains for Tofu's own operations
(delivery, integration snapshots, checkpoints, dependency setup) and names
both sides of a stalled acquisition:

```text
[WorkspaceLease] wait 12s mode=write root=tree:/…/tofu waiter(pid=… thread=…
  thread_name=tofu-integration tool=- task=- conv=- claims=file:/…/a.py,…
  cmd="…") [holder: pid=…, thread=…, held_seconds=12.0, mode=file-write,
  thread_name=…, claims=tree:/…/worktrees/x]
```

(one line in the log). `claims=` lists at most four `scope:path` labels
(`+N more` beyond). `mode=` is the blocker's mode on the contested key:
`file-write` on an ancestor `tree:` key means the holder's claim lies beneath
it. A blocker that is an earlier queued request the waiter may not overtake
reports `queued_seconds=` and `mode=queued-<mode>` instead of
`held_seconds=`. `cmd=` is the caller's purpose summary, collapsed to one line
of at most 160 characters. The `workspace_admission_wait` event carries the
same facts. The decision record is "Both sides named" in
`docs/runbooks/GIT_WORKSPACE_GUARD.md`.

`run_command: $ <cmd> (timeout=…, cwd=…)` logs one line per execution: the
redundant leading `cd <cwd> && ` is elided (the line records cwd), newlines are
escaped, and text beyond 600 characters ends in `…[+N chars]`. Verification:
`tests/project_mod/test_run_command_log_line.py` and
`tests/project_mod/test_workspace_admission_wait_both_sides.py`.


## Multi-agent load evidence

Read the [architecture scaling map](ARCHITECTURE.md#multi-agent-execution-and-scaling-boundaries)
before interpreting Agent counts. Existing `/metrics` exposition is protected
by the admin scope. Save two complete authorized scrapes from the same serving
process and record the elapsed monotonic time between capture boundaries.
Capture the process incarnation and source/build identity separately; file
modification times and a shared URL do not prove either. Keep raw observations
outside tracked source and do not include credentials or conversation contents.

Compare the saved observations without importing or starting the application:

```sh
python3 scripts/analyze_agent_load.py before.prom after.prom --elapsed-seconds 60
```

The elapsed value is the measured observation interval, not a polling setting.
The script reads local files only, identifies them by SHA-256, preserves gauge
endpoints, and calculates interval counter deltas and storage-RPC mean wall
durations. It selects task, executor, event-delivery and storage-RPC series;
unrelated metric labels are not copied. Missing endpoints, decreased counters
and inconsistent histogram pairs never become zero work or successful latency
measurements. Malformed or duplicate selected series fail with a nonzero exit.
The report is diagnostic evidence, not a performance pass/fail gate.

Interpret the populations separately:

- Task counts are not a census of logical graph actors. Executor jobs, resident
  carriers, monitoring threads and operating-system threads are different sets.
- Delivery `pending_bytes` charges queued/in-flight events and retained
  structural sources, including shared immutable rows. It is not unique heap
  memory or RSS, and dividing it by pending-event count is not a frame size.
- Event-loop lag is a point observation. Delivery `max_lag_ms` and `peak_*`
  are process-lifetime high-water marks. Neither gives an interval percentile.
- RPC sum/count deltas give the mean duration of completed calls in that series.
  Concurrent durations overlap; their sum is not elapsed wall time, database
  CPU or storage-service time alone. Successful and failed outcomes stay
  separate. Long polls and streams must not be judged as short HTTP requests.
- A counter decrease proves the interval cannot be differenced as one monotonic
  series. Counters that grow do not exclude a restart followed by enough new
  work. Confirm process incarnation externally before making rate comparisons.

Pair this with process CPU/RSS/thread observations, the existing bounded log
diagnostic and workload shape. Record truncation and read errors. Stack sampling
locates candidate work; missed samples and nonblocking reads limit precision,
and profiled runs cannot serve as uninstrumented performance comparisons.
Use the application workload and failure matrix in the architecture map before
claiming improved multi-agent throughput or capacity.

Verification: `tests/meta/test_analyze_agent_load.py` covers interval arithmetic,
quiet/missing/reset series, malformed evidence and a standalone offline CLI.

## Metric work on storage RPCs

Metric test resets clear both admitted label values and their normalization
memo. Resetting only the admission set leaves previously folded labels stuck
on `other` in later tests; retaining that memo across a reset was rejected
because it makes label admission depend on test order.

Problem: every observation updated every matching cumulative bucket, while
exposition repeatedly sorted and rescanned complete series for each metric
name and re-escaped the same labels for every bucket.
Decision: the process-local store retains one disjoint count per distinct
non-NaN boundary and uses binary search to increment at most one count.
Scraping derives inclusive counts from a consistent locked snapshot, groups
sorted series once, and escapes each histogram's labels once. The first bucket
definition, its original order and duplicate boundaries remain authoritative;
negative/NaN observations retain their existing zero clamp, NaN boundaries
never match, and infinite observations remain counted. Counter names retain
precedence over gauges of the same name. No sample, label, public metric or
observation is dropped.

Alternatives considered: sampling or disabling metrics loses exact evidence;
adding a scrape cache introduces freshness policy; assuming sorted unique
inputs changes the existing store contract. Input conversion still occurs on
every observation, preserving failure behavior for invalid bucket iterables.
Existing prefix-only label normalization and cardinality limits are unchanged.

A seven-pair alternating component benchmark against `7d0f5ff8a` checked
byte-identical exposition and exact snapshots on each pair. For 100,000 calls
to `record_storage_rpc`, median wall time fell from 0.556 s to 0.471 s (15%).
Ten scrapes with 8 series per name and 1/16/64 names per metric kind fell from
2.22/47.7/354 ms to 1.17/17.2/70.1 ms. An unchanged-source control varied by
approximately 2% or less. This measures metric work in one Python process,
not storage RPC latency, application startup or overall request throughput.
An eight-writer fixture with 16,000 observations and 100 concurrent snapshots
preserved exact counts: median wall/CPU time changed from 257/331 ms to
194/253 ms. First observation of a new series pays boundary indexing once:
a separate nine-pair measurement changed from 5.33 to 6.34 microseconds.
For the 16-name fixture (128 histograms), traced retained Python allocations
changed from 155,880 to 156,200 bytes; the peak including a detached snapshot
changed from 215,024 to 218,120 bytes. These are Python allocations, not RSS;
no worker, queue, persistent record or tuning parameter was introduced.
Verify `tests/observability/test_metric_store.py`,
`tests/storage/test_storage_rpc_metrics.py` and the metrics route suite.

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

## Correlated translation phase timing

**Problem:** one `Translate:trace elapsed` and its outer attempt count could not
separate local routing, backpressure, nested HTTP retries, provider response wait,
body transfer, content validation, or storage contention. **Decision:**
`lib/llm/diagnostic_timing.py` owns monotonic, inclusive phase evidence;
`lib/translate/observability.py` admits scopes and explicitly transfers only this
diagnostic context to translation workers. `lib/http_transport/client.py` and LLM dispatch
consume the generic observer and never import translation policy. No model,
retry, admission, request format, cancellation, timeout, or durable DTO changes.
**Alternatives rejected:** timing the whole POST as inference hides storage;
switching to streaming changes provider behavior; estimating DNS/TLS or first
content from response completion manufactures measurements.

The normal `app.log` receives JSON records prefixed `[Translation:timing]`:
`phase_start`, `phase_observation`, `phase_complete`, and one `summary`. A
`timing_id` links the root command, worker and physical request records; safe
`conversation_id`, `task_id`/`job_id` and actual `M-TraceId` provide additional
correlation. The engine's private `_translate_trace.timing` snapshot carries the
same ID. No new database, endpoint, logger file, background thread or timer is
introduced. These are diagnostic facts, never task/Turn settlement authority.

| Phase / field | Actual observation |
|---|---|
| `turn_command_total` | command application; an accepted turn's input translation is not part of it |
| `input_translation`, `language_detection` | send translation gate/work: the attempt-start job of an accepted turn (`send` operation; the worker logs `[Task …] joined deferred input translation after Ns` when it had to wait) or a synchronous steer |
| `lane_queue`, `lane_worker` | submission → worker entry, and worker entry → return |
| `route_resolution`, `route_selection`, `dispatcher_wait` | dispatcher preparation, slot selection, and actual backpressure sleep; every pick on any thread (agent rounds included) also records `tofu_llm_route_selection_seconds{scope=pinned\|shared,outcome=selected\|none}`, dispatcher-lock wait included; chat dispatch reports its summed pick time as described below |
| `cache_lookup`, `identity_check`, `machine_translation`, `cache_store` | explicit fast paths and cache writes |
| `engine_attempt`, `engine_backoff`, `quality_validation`, `postprocess` | outer translation attempt, actual backoff, guards/status publication, dedup |
| `status_delivery` | status callback publication; nested in validation when appropriate |
| `request_build`, `input_admission`, `request_seal` | canonical body building, input accounting and final sealing |
| `egress_route`, `network_route`, `http_pool_acquire` | desktop/proxy path resolution and thread-local session acquisition |
| `provider_call`, `chat_total`, `provider_send` | selected-slot invocation, nonstream chat lifecycle, and send/budget boundary |
| `http_request.response_headers_ms` | request start → last actual response-header hook; includes connect/upload/upstream wait |
| `http_request.response_body_ms` | last response-header hook → nonstream request return; includes caller hooks and body transfer, not pure generation |
| `response_parse`, `response_normalize`, `response_usage` | JSON parsing, wire normalization and content/token count disclosure |
| `transport_backoff` | actual physical transport retry sleep |
| `provider_stream.first_content_ms` | stream attempt start → first actual nonempty text delta; reasoning/heartbeat is not content |
| `provider_stream.after_first_content_ms` | first content → attempt end, including remaining events/usage; errors retain failed outcome |
| `stream_headers_wait` | direct/relay stream-header wait, with physical trace and transport label |
| `storage_commit`, `storage_attempt`, `storage_backoff` | translated Turn overlay CAS lifecycle, storage attempts and actual retry sleeps |
| `turn_persist`, `executor_start`, `acknowledgement_read` | accepted send command persistence, executor start and final receipt reads |

**Chat dispatch pick latency (decision 2026-11-02).** Problem: outside an admitted translation scope `timed_call('route_selection', …)` records nothing, so agent and swarm rounds had no log-visible pick latency, while the per-logger `LogBudgetFilter` (300 INFO/DEBUG lines per minute) dropped `lib.llm.diagnostic_timing` lines in 33 one-minute windows of the 2026-11-02 live profile. Decision: no per-pick log line is added. Every pick on every thread records `tofu_llm_route_selection_seconds{scope,outcome}`, which no log budget can drop. Each chat dispatch (sync stream, async stream, non-stream) also sums the wall time of its picks, empty picks and retried picks included, into the internal dispatch evidence `usage['_dispatch']['route_selection_ms']` beside `queue_wait_ms`, so the round's provider evidence carries it, and the existing per-round INFO line `stream_llm_response complete: … route_selection_ms=<ms>` prints it (`-` when a transport reports no dispatch evidence) without adding a line. Translation keeps its `[Translation:timing]` phases; when the budget drops them, the metric still holds every pick. Alternatives considered: binding a `DiagnosticTiming` scope to every chat dispatch emits start and complete records for every phase of every round into the same budgeted logger; a per-pick INFO line from the dispatcher adds load to a budgeted logger and is dropped first under the contention it should explain; WARNING records bypass the budget but misfile routine timing as incidents. Specs: `test_chat_dispatch_pick_emits_the_latency_record` and `test_round_completion_line_reports_the_dispatch_pick_time` in `tests/llm_dispatch/test_slot_pool_scopes.py`.

Spans are **inclusive**, not additive: `parent_span_id` names nesting, and worker
execution overlaps the synchronous caller wait. Sum neither `provider_call` and
`http_request` nor `engine_total` and its children. The Request lifecycle's HTTP
log remains the full route wall clock; `turn_command_total` starts at command
service entry. A send fallback reports `translation_outcome` separately from a
successfully accepted command. Translation-enabled create commands keep one
scope through persistence; unrelated commands do not enable these diagnostics.

Requests hooks observe headers *before* automatic nonstream body consumption.
Caller/session hooks and replacement responses preserve their original authority;
the metric status code retains the real header observation, not a replacement.
Redirect header observations retain first/last offsets and an exact count.
`stream=True` return and failed/aborted reads never claim `body_complete`.
Connection failures retain no invented header time. Adapter/desktop nonstream
calls have outer send coverage but no fabricated Requests header/body split.
DNS, TCP/TLS and provider-internal queueing are explicitly unobserved; nonstream
first-token timing is unobserved, never the old completion upper bound.

Per scope, constants bound 48 stage aggregates, 48 span samples/active records,
and 96 phase events plus one compact summary. Aggregated stage counts/durations,
real HTTP response-header/429 counts and HTTP-boundary errors continue beyond
sample clipping; `coverage`, `events_suppressed` and uncounted-stage occurrences
report gaps. Metadata uses fixed scalar fields and 128-character labels; phase
names cap at 64. Snapshots are detached. A caller timeout freezes its summary
with outstanding spans; worker records arriving afterward are explicitly
`late_phase_*` and cannot change its verdict. Background summaries say
`worker_completed`, not durable translation success. Deferred storage retries
have separate worker timing IDs and correlate by their existing task/job ID;
scheduled delay is not claimed as active worker or model time.

Observation faults cannot replace output or exceptions. New evidence contains
no prompts, response/reasoning bodies, URLs/query strings, header maps, key names,
key values or exception text. Exact local tests:
`test_diagnostic_timing.py`, `test_http_translation_timing.py`, and
`test_translation_phase_metrics.py`, plus existing deadline/budget/HTTP suites.
A Python 3.12 component probe measured 20,000 trivial calls at 0.043 µs baseline
versus 0.120 µs through an inactive observer; 1,000 active phase scopes with
tracemalloc and a no-op sink took 19.615 ms, retaining 48 samples and all 1,000
aggregate occurrences in a 5,659-byte snapshot. This excludes logger delivery,
network, storage and inference and is not an application-speed claim. Existing
log rotation, queue and family-retention limits remain the only durable-log
resource policy.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->
