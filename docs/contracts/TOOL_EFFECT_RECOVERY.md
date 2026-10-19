# Tool effect recovery

This contract owns effect evidence at handler entry and its recovery presentation. Approval and ordinary
result settlement remain in [tool execution policy](TOOL_EXECUTION_POLICY.md).

Problem: a timeout or handler exception does not prove that an external write failed; replay after a crash can
duplicate a completed operation. Decision: `ToolContractV2.effectRecovery` declares `read_only`, `idempotent`,
or `reconcile`, checked against permission/idempotency. Missing or contradictory execution documents require
reconciliation. This metadata is server-owned; provider schemas, tool names, equal arguments and exception
text cannot grant safe replay or approval. Overflow `resultRecovery` is a separate contract.

The common executor stamps each occurrence with `executionEffect`: an opaque operation ID, recovery policy,
`attempt`/`process` scope and state. For a durable conversation attempt, non-read-only calls synchronously
commit `tool_effect` through the existing Turn/event transaction **before** entering the handler. Its exact
acknowledgement is required; suppressed, withheld, stale, coalesced, failed or unregistered delivery cannot
authorize entry. The barrier flushes prior accepted events and cannot use the oversized-projection slimming
path. Cancellation is rechecked after the barrier. Transient invocations remain storage-free and explicitly
process-scoped; reads do not pay an intent barrier.

`entered` records possible entry, not success. `returned` means a handler response was captured, including
errors or background handles; it never means that an external operation completed. An exception after entry
records `unknown`; refused entry records `not_started` in the surviving process. A lost storage
acknowledgement can leave `entered` on disk even when this executor never entered. The restored interface
preserves that uncertainty. Tool verdict, Turn completion, and effect outcome remain independent.

Unknown non-idempotent results carry a canonical non-retryable `tool_effect_unknown` error directing the
caller to query the destination. Tool/program cards show the warning without opening results; a terminal Turn
with only an `entered` record gets the same warning. Old rounds without effect metadata retain their existing
presentation. Current operation IDs identify occurrences; they are not yet provider idempotency keys or
cross-attempt replay receipts. This boundary adds no automatic retry or exactly-once promise. The tracked
local file adapter below supplies outcome inspection. Every subsequent execution still uses ordinary approval.

Cost measurement (64 synthetic occurrences per disposable store, 60 warm): intent commit p95 was 1.87 ms on
SQLite and 2.58 ms on native TofuDB; the marker was 109 JSON bytes and the complete carried event 533 bytes
(median). These are local fixture measurements, not a throughput claim. One event is added per non-read-only
durable occurrence, retained under the existing event/Turn policy; there is no extra journal, pool, timer or
cache. Durable effect evidence is not reclaimed as transport data. Read-only calls have no added commit
barrier.

Alternatives rejected: classifying every exception as no effect invites repeated writes; a second Python
journal would split storage authority; inferring acknowledgement from a shared cursor confuses sibling events;
recording intent after entry leaves the crash window unobservable. Evidence: `test_tool_effect_recovery.py`,
real SQLite/TofuDB `test_tool_effect_barrier.py`, and the Chromium effect-recovery surface test.

## Tracked local file publication

Problem: file replacement can succeed before its response or undo acknowledgement. Decision: bind the exact common tool occurrence to the existing write-intent
and undo authorities. After journal preparation and before replacement, tracked local write_file commits a tool_effect reconciliation reference: change ID,
attributed root/path, producing task, root identity, digest and size, without file content. The round projection and event carry the same reference; exact
commit ACK is mandatory and cancellation is rechecked before replacement. This does not prove earlier directory creation or later artifact promotion. Scoped-
dispatch, remote, scratch/untracked and transient writes do not gain the adapter implicitly. Headless tasks retain task-ID journal attribution when there is no
conversation: the guard uses tool_admission.journal_conv_id, matching preparation, and still checks the exact producing task. Comparing an absent conversation
directly with the journal task ID rejected valid Evolution writes. Skipping headless attribution checks was rejected because it admits foreign prepared writes.
No new conversation, durable inspection scope or replay authority is granted.

`GET /api/v4/conversations/{conversationId}/turns/{turnId}/tool-effects/{operationId}` uses the ordinary chat
authorization boundary and owner-scoped Turn repository, plus the existing `ownerScope` cache fence. Callers
provide identities, never a path or reconciliation reference. The fresh, `private, no-store` response is
`file_published`, `file_not_published`, or `unknown`, with a schema-defined reason. Only one matching
occurrence is admissible. Its producing task, conversation, change ID, root, path, digest and byte count must
match the file authority.

A matching committed undo receipt proves **historical file publication**, even if a later user edit, undo or
root replacement changed current content. With only a prepared intent, the existing root/inode/content
classifier decides whether publication is proved or an unchanged original plus intact stage proves it has not
happened. Missing, malformed, ambiguous, conflicting or busy evidence remains unknown. Queries hold the
existing journal admission and never execute recovery, acknowledge an intent, repeat an append, or modify user
files. No missing journal directory is created by an inspection. The response does not grant retry or
approval, finish an interrupted agent invocation, or accept a deliverable.

Accepted native Goal recovery has one additional internal proof for a wholly unentered registered-child batch. Native retirement/claim and whole-account handoff must already acknowledge its original pending checkpoint and unchanged uncharged actor prefix. `inspect_child_effect_batch` then checks the original owned birth/parent, absence of every exact operation intent and an unchanged record after inspection. Since handler entry requires that intent's durable ACK, this combination proves `file_not_published/checkpoint_batch_unentered`. Any contradictory receipt refuses. The ordinary inspection API still requires an existing occurrence; absence alone remains insufficient. Reconciliation preserves the original paid model quote, accounts the resolved batch once, and returns the observation to the model; a new write is a separately authorized invocation. This uses the existing record and account authorities, with no extra durable row, pool or background worker. Tests cover missing/contradictory/racing evidence and real writer SIGKILL before intent and after file publication.

Alternatives rejected: comparing content alone cannot distinguish a later equal write; recording the reference
after replacement loses the crash window; a second outcome journal duplicates authority; rebuilding an
operation ID from arguments or provider call IDs can join unrelated writes. The generated query client is
consumed by the mounted tool notice and the read-only `inspect_tool_effect` tool. The admission rule below
covers new local writes after inspection; automatic replay and native in-flight continuation remain separate
work. Validate with `test_file_effect_reconciliation.py`, `test_file_effect_inspection.py` (SQLite and native
TofuDB), and owner-fenced HTTP `test_tool_effect_inspection.py`.

Cost of the added reference barrier (36 actual file writes per disposable store, 32 warm samples): p95 2.00 ms
SQLite / 1.87 ms native TofuDB. The matching owner-scoped outcome query was 1.50 / 1.08 ms p95, with 1–36 undo
records. The reference was 351–352 compact JSON bytes. Paths affect its size. This adds one existing
Turn/event commit per tracked local durable file write, no new journal, background worker, timer, or retained
cache. These small local fixtures are not long-history, end-to-end task, or remote-provider performance
claims.

## User and agent inspection

Problem: a query API alone does not help a person or model act on a lost write acknowledgement, and delegated
cards may display an occurrence from another Turn. Decision: the same pre-publication commit binds
`inspection` to the producing conversation, Turn, operation and authenticated owner cache scope. A displaying
parent never replaces these IDs. The cache scope is not a credential; ordinary chat authorization and
owner-scoped repository lookup still decide access. Legacy effects without this reference keep their
uncertainty warning.

The tool/program notice offers an explicit **Check file publication** read for supported uncertain writes,
including captured write errors while the Turn is still running. Confirmed publication is a historical file
fact, not current-file correctness, full tool success or task acceptance. Unpublished, conflict, busy, missing
evidence and query failure remain distinguishable in the copy. There is no retry-write or accept-task action
in this notice.

Each mounted occurrence retains one optional observation and one in-flight AbortController. Repaint preserves
the control and makes no query. Repeated clicks coalesce while the request is pending; a later explicit check
replaces the observation even when it fails. Identity/owner changes, block-kind changes and disposal cancel
the request. Late or mismatched responses cannot publish. Reload does not restore an old observation as
acceptance; the user checks again. No timer, polling loop or durable browser state is introduced.

`inspect_tool_effect` is eagerly callable for durable conversation tasks and absent from storage-free
assembly. It accepts the exact operation ID and an optional producing Turn (defaulting to the current task's
Turn); conversation and user come only from task context. It is read-only, uncached and uses the same query
service as HTTP. Captured write errors and unknown-effect exceptions carry a callable instruction with the
producing IDs. A failed inspection is a tool error; a successful query returning `unknown` is an honest
observation. String patterns declared by tool contracts are enforced at the common argument validator with
JSON Schema search semantics, rather than copied into this tool.

Alternatives rejected: deriving IDs from a displaying card queries the wrong occurrence; a global outcome
cache can outlive evidence or owners; local Turn patches turn read observations into a second acceptance
authority; automatic replay after a read discards the original approval and operation identity. Verify
registry/schema/handler contracts, `test_file_effect_agent_read.py` (SQLite/native), browser
inspection/disposal tests, and the built application `test_visual_file_effect_application.py` against real
storage and HTTP.

The inspection reference adds 180 compact JSON bytes for representative IDs (194 bytes including its field in
an existing effect); the provider-facing read-tool schema is 777 bytes. ID lengths affect the reference size.
It shares the already required file-reference commit, so adds no write barrier. A mounted notice issues at
most one query at a time and zero automatic queries; lifecycle browser tests verify those counts and
cancellation. Schema metadata is static, while observations and DOM controls retire with their existing
Surface block.

## Lost-executor interruption receipts

Problem: retirement left a dispatched file call without model-visible feedback; Continue/checkpoint retention
then dropped the occurrence and its inspection ID. Decision: `turn.recover` records a canonical non-retryable
`tool_effect_unknown` observation inside its existing retirement transaction, before deriving resume options.
SQL calls `lib/tools/effect_interruption.py`; native storage implements the same boundary in
`turn_effect_interruption.rs`. Selected-authority tests compare the persisted receipt and subsequent ordinary
Continue behavior.

Only a unique, current task/attempt `write_file` occurrence with attempt-scoped reconcile policy,
entered/unknown state, matching publication/inspection identity, and no captured response qualifies. Existing
responses, foreign/unbound/duplicate identities and discarded provider artifacts remain unchanged. An
inspection call that cannot fit the canonical error advice budget leaves the gap intact. The receipt sets the
occurrence to aborted/unknown and preserves its producer IDs, arguments and publication reference. It states
an interruption observation, never a handler return, successful file publication, task completion or approval.

This lets normal history/Continue carry the exact action and query instruction. It does not resolve the file,
rerun a tool, grant native in-flight checkpoint handoff, complete an unmatched Flow invocation or start a
model. Ordinary query, approval, owner/cancellation and native checkpoint fences remain in force. Alternatives
rejected: silently amputating the call loses evidence; inventing a successful result or replaying the write
can duplicate an already-applied append.

Cost: 1,024 synthetic occurrences added 501 compact JSON bytes each (IDs affect size); 30 warm pure Python
projection samples measured p95 36.15 ms, excluding persistence, model and transport work. The pass and
temporary identity counts are linear in retained rounds. Receipts share the existing Turn lifetime and
recovery transaction; no worker, timer, cache or journal is added. Real SQLite, TofuDB and PostgreSQL tests
kill the file executor after replacement, reopen storage, retire it and Continue to a fresh inspection with
one append; malformed evidence and live exclusions retain their existing boundaries. This short scenario is
not the two-hour demo.

## New-write admission after an unresolved file operation

Problem: a model can ignore recovery advice or plan a repeated append in the same round as its inspection.
Decision: the common decision and handler-entry owners call `executor/_file_recovery.py`. For a new
`write_file`, match the resolved target against retained tracked local occurrences in this task and its
checkpoint. Scope matching identifies a dependency, never an operation identity. Other targets and reads stay
independent. Captured handler errors with publication references are included even when the handler returned.

The producing operation must have an owner/conversation-bound, complete `inspect_tool_effect` result from an
earlier model round of this executor. Read the existing settled `toolContent` after output budgeting;
incomplete, clipped, foreign, discarded and same-round reads do not satisfy admission. No observation cache or
journal is added. Recheck those facts through the ordinary owner-scoped service before approval and handler
entry. Changed facts require a new inspection and replanning; an unavailable service or active writer cannot
be overridden. Restart requires a new read.

Known publication/non-publication allows a newly planned call to reach ordinary authorization, path and
freshness rules. It never re-executes a prior operation. Unknown evidence requires the existing attended human
gate to disclose the producer IDs, uncertainty and exact new arguments. Auto, delegated adjudication and
earlier general approvals cannot supply this takeover. Child/unattended invocations refuse visibly. The
existing exact expiring receipt additionally binds the observed unknown facts; native prepared execution
preserves it and common entry consumes that restriction once. Changed arguments, facts or identity invalidate
it. Rejection/cancellation starts no new effect. The historical operation remains unresolved.

Alternatives rejected: checking only the prompt leaves a bypass; accepting a same-round query lets a
preplanned append proceed; blanket task suspension blocks independent work; inventing success or equating
equal arguments with an operation breaks causality. This covers normal tracked local `write_file` targets, not
shell/remote/provider mutation, native pending invocation settlement or general exactly-once execution. Verify
unit/actual native gateway cases in `tests/tasks_pkg/test_file_recovery_admission.py`, real selected-store
tests in `tests/turn_lifecycle/test_file_recovery_admission.py`, and actual application approval in
`test_visual_file_recovery_approval.py`.

## Registered child file receipts

Problem: Flow, Goal and Swarm children already have immutable registered birth
identities, but process-scoped tool evidence disappeared on executor exit. A
parent Turn cannot identify or prove those child publications.

Decision: `tasks_pkg/invocation_effects.py` uses the existing registered `task_results` record and guarded `task_results.checkpoint` CAS. The common tool boundary commits each non-read-only occurrence before entry and commits the prepared file reference before publication. `scope=invocation` and `invocationInspection` identify the actual child, conversation, operation and owner scope. The recorded birth identity, active owned conversation, exact producer coordinates and checkpoint version authorize the update. Parallel tool receipts serialize only within their child; conflicting external versions refuse without rebasing a frozen candidate. Lost acknowledgements may replay only that same checkpoint, never a file handler. Explicit cancellation still prevents entry. Descendants inherit their parent's explicit invocation storage policy; the transient runtime carrier flag cannot downgrade a registered lineage to process-only evidence. Every generation retains its own registered producer.

The record's `tool_rounds` field retains compact producer/reference rows, without
arguments, file bodies or duplicated model context. It remains with registered
child ownership and conversation lifecycle; no event TTL or new retention timer
can erase its evidence. The write-ahead state is intentionally not an assertion
that a handler completed. The existing file journal determines publication.

`inspect_tool_effect` accepts an explicit `invocation_id`, mutually exclusive with
`turn_id`, and returns canonical `InvocationToolEffectInspection`. It reads the
exact registered child under the requesting owner and conversation, then uses the
same file-domain inspection as a Turn. Active writers remain unknown; source paths
and journal proof never come from tool arguments. Further writes in the same child
require an earlier complete inspection and fresh evidence at approval and entry,
just like root writes. Unknown outcomes still require an exact attended human
decision; unattended children cannot inherit that permission.

Alternatives rejected: forcing children into parent Turns changes ownership;
using prunable Inspector events loses restart evidence; a second file journal or
replay loop duplicates existing authority. These receipts do not restore a native
in-flight model frontier, settle fees, or authorize an unmatched Flow invocation
to rerun. Those remain separate recovery requirements.

Verification: `test_child_file_effects.py` exercises real SQLite/TofuDB publication,
restart, lost intent/reference/undo ACKs, parallel tools, cancellation, owner
isolation, observed-read admission and SIGKILL after replacement. It verifies an
active writer reports unknown and the reopened store proves historical publication
without appending the file again.

Cost: 2,048 synthetic retained rows (1,024 affected operations) measured one admission pass at p95 18.55 ms
over 30 warm samples, including path metadata checks but excluding publication queries and file-content I/O.
Sources and model-visible receipts are indexed once per pass. Each affected producer adds one fresh query at
decision and again at handler entry; no worker, timer or observation cache is created. The existing expiring
receipt adds a 64-character evidence digest. For one unknown producer and 512-byte content, approval metadata
grew from 565 to 1592 JSON bytes; it stays in the existing approval event/Turn lifecycle and result limits. No
exactly-once or end-to-end speed claim follows.

An isolated 20-append measurement on 2026-10-14 used real SQLite and native TofuDB
daemon authorities. At 256 starting receipts, one checkpoint measured 6.51 / 9.56
ms p95; at 2048 it measured 41.70 / 65.89 ms p95. The latter ended with 2068
reference-only receipts occupying 1,415,322 JSON bytes, with no arguments or file
bodies. A fresh child measured 1.76 / 1.50 ms p95. Each checkpoint uses two reads
and one CAS command. Removing redundant birth resolution avoided rereading the
cumulative record; the earlier 2048-receipt measurements were 43.96 / 72.44 ms.
This is storage overhead, not model latency or task-success evidence. Cumulative
checkpoint serialization remains linear in retained receipt count; this change
does not claim constant-cost long-task storage. The file-effect recovery contract
and `test_child_file_effects.py` specify fault, ownership and restart semantics.

## Flow agent identity

Problem: a child can finish before its Flow completion event is acknowledged.
Decision: registered conversation children bind their exact selected-store birth,
`invocation_id` and a fresh admission nonce to `execution_effect_started` before
model/tool dispatch. The ordinary journal captures the normalized full result,
original accounting, a cumulative `execution_account` snapshot and Goal worker context. Before publishing completion,
`invocation_recovery.py` commits one immutable `flow_invocation_completion` in the
existing child task-result record through the guarded checkpoint CAS. Lost ACKs
retry the frozen candidate; conflicting completions and later tool effects refuse.

An owner-bound recovery may resolve an unmatched admission only when the stored
completion matches its entire admission, including nonce, input hash and exact
birth lineage and account root. Goal also requires its archived conversation/parent and accepted source execution limits.
Before new work, the Goal successor restores one complete settled shared-account frontier dominating all receipt counters;
it never adds cumulative samples or calls/reprices a replayed child. Unknown usage and missing bounded evidence refuse.
Missing results, unknown effects, inaccessible/deleted parents and changed lineage still refuse.
Explicit registered native actor capture preserves paid local frontiers; takeover remains refused (see `RUN_CONTINUITY.md`). Headless children remain storage-free.
A completed leaf is not artifact acceptance or provider-level exactly-once I/O.

Alternative rejected: UI previews, successful tool returns or inferred node IDs
do not prove a complete leaf result. A separate recovery store would split child
ownership and deletion. Resetting consumed authorization or renewing the deadline on recovery is rejected. One extra full result follows the task-result lifecycle. This adds one lookup and one guarded CAS per
completed registered leaf, and duplicate result bytes alongside the Flow event.
`test_flow_invocation_completion.py` verifies SIGKILL/reopen, ACK loss, ownership,
context and accounting; `test_flow_invocation_binding.py` covers admission.
For 32/256 KiB output fixtures, the extra receipt occupied 68,974/527,727 bytes;
native single-CAS samples were 22.53/156.25 ms, with 32 warm full-recovery reads
median 5.67/22.32 ms (SQLite 2.56/3.24 ms). Both appended once after SIGKILL.
The account adds a 308–309-byte snapshot (32-byte root ID), no transaction/worker; capture medians over 1,000 calls were 12.57/21.33/78.16 µs with 0/32/256 active children. Local costs, not task-performance gains.

## Native paid tool frontier

Problem: a completed-round checkpoint omits a paid provider response when the
process exits during its tools. Retrying that provider or the whole batch can
repeat charges and effects; treating every inflight checkpoint as completed
would invent observations and reset progress protection.

Decision: the existing native Agent/Journal/OwnedPolicy adds an explicit
`tools_pending` frontier after provider settlement and complete-batch validation,
before approval or tool entry. It stores the complete assistant message, usage,
original limits, progress ledger and optional account/deadline authority in the
same current snapshot. `pending` is also a generated checkpoint C ABI operation.
The new header phase remains leased, cannot begin another round or release, and
only explicit recovery with the store's kernel-backed dead-writer proof may take
it over. Uncaptured `inflight`, terminal and legacy writer fences still refuse.

The supplied financial/time recovery owner restores authority first. A separate,
default-deny `Recovery.reconcile` callback must query domain evidence and return
one observation per exact saved call, in batch order. Missing, reordered, changed
or oversized observations cannot enter a new model round. It grants no execution
permission and must never dispatch the old tools. Agent reuses its ordinary
post-tool progress and completion boundary; it preserves the paid usage prefix,
tool/round counts and existing breaker streaks. Journal completion checks that
only tool observations, progress and bounded spent tool quota changed: the paid
message prefix, usage, account origins, limits and deadline cannot be rewritten. Cancellation remains joined.

Alternative rejected: inferring completion from a file's current bytes, a UI
preview or a provider call ID alone cannot prove a particular publication.
The explicit stdio/SDK policy can publish and reconcile this frontier, including
priced host restoration. The explicit file gateway binding below covers proven
ROOT file batches and accepted wallet handoff; Flow continuation and general
application-wide interrupted-tool resume remain separate admission work.

Verification: native Agent tests interrupt after actual tool dispatch and resume
without repeating tools or the paid model round, including duplicate arguments
with distinct call IDs, refused/partial batches, Stop and retained no-progress
protection. A real TofuDB writer is SIGKILLed after file append; reopening retains
the paid pending snapshot, proves writer death, and settles without appending
again. C ABI tests enforce the same state/fee-prefix boundary.

Cost: the existing header/current-snapshot lifecycle remains; no worker, cache
or separate journal is added. Each enabled tool round adds one snapshot Replace
and one version-pinned snapshot read at completion; configured WAL/outbox retains
those writes under its existing GC/ACK rules. Recovery additionally reads its
exact snapshot before invoking the reconciler and briefly holds a bounded saved
copy while restoring messages. A local native-store component measurement (8
warmups, 32 samples) found completed-only versus pending+completed p95 2.53/6.49 ms
for a 32 KiB source and 16.12/39.96 ms for 256 KiB. Mean logical snapshot bytes
written per round were 33,956/67,872 and 263,332/526,624 respectively. The ignored
`measure_pending_checkpoint_cost` test reproduces this measurement. These are
durability costs, not full-task performance or physical WAL byte measurements.

### Application checkpoint callback boundary

Problem: a native pending snapshot alone cannot restore host accounting or locate
an application effect. Decision: a policy explicitly declaring
`supports_pending_tools=True` supplies `pending` and `reconcile` callbacks over
the existing stdio checkpoint channel. Legacy policies retain their old protocol;
an archive without the new configure field refuses before dispatch. Pending
capture follows paid settlement and precedes tool approval/entry. Restored
accounting and original fees precede reconciliation; ordered exact observations
then enter the existing progress and checkpoint completion path before another
model reservation. The old round is never represented as a new provider request.
Malformed, changed, incomplete or oversized callback results refuse continuation;
host callback failure remains sticky and cancellation joins entered work.

`NativeCheckpointPolicy` delegates to the selected embedded authority. Its optional
`PendingToolRecovery` port captures domain metadata without changing paid fields,
receives `committed` only after the pending snapshot ACK, and reconciles the exact
loaded snapshot. Optional `tool_recovery` metadata belongs to that domain port,
is frozen across completion by the native journal, and is held only for the
current batch. It is not execution permission. Unknown ACK, cancellation or a
failed domain query never arms or replays a tool. The port must validate owner,
producer and exact operation identity; it cannot infer publication from current
file bytes alone. Paid child execution changes financial state and needs an
explicit admissible batch/account policy before using this immutable frontier.

Alternative rejected: clearing the inflight marker or issuing another model
reservation would invent settlement and could replay paid work. Adding a Python
journal or a second post-tool state machine would split the existing authority.

Verification: `test_native_fee_checkpoint_installed.py` uses an actual priced
native process, interrupts after a fixture tool publishes, and restores the paid
prefix before querying its supplied domain port, observing progress and issuing
the next HTTP request. It rejects missing or altered fee/account evidence.
`test_native_pending_policy.py` SIGKILLs a Python writer using the real selected
C ABI and verifies exact domain-metadata recovery with one fixture publication.
These fixture domain callbacks do not implement the production file gateway.
Unit tests cover ACK loss, ownership, mutation, cancellation and callback order.

Application cost: a local Python-policy/selected-C-ABI measurement (8 warmups,
32 samples per size and mode) measured completed-only versus pending+completed
p95 3.07/8.27 ms at 32 KiB and 20.29/52.11 ms at 256 KiB. Logical snapshot
bytes were 33,448/66,991 and 262,824/525,743. This excludes stdio, tools and
provider latency. A simultaneous-build sample reached 92.92 ms at 256 KiB,
so these are host-local component observations, not a task-speed claim. The host
retains a digest and current domain metadata; capture briefly detaches one
snapshot under existing byte bounds. Stdio serializes the live pending view by
borrowing history. No new worker, history cache or durable store is added.

### Exact ROOT file batch binding

Problem: restoring a paid response without the server's operation IDs cannot
identify actual file publications, and restoring only pre-tool counters would
refund spent tool quota. Decision: explicit `NativeFileToolRecovery` binds the
existing gateway's prepared occurrences to server-minted UUIDs. The existing
pending snapshot carries canonical inspection references and producer identity;
only its durable ACK arms those exact rows for ordinary approved handler entry.
The common handler still owns intent, publication and outcome commit barriers.

Reconciliation validates owner, conversation, Turn, producer and ordered whole
batch, then queries the canonical file-effect service without executing tools.
Only proven published/not-published outcomes become observations. Historical
publication remains proven after a later user edit when the committed undo
receipt exists; the recovery never overwrites that edit. Missing, foreign or
unknown proof refuses continuation and marks tool accounting incomplete. A fully
proven batch adds its spent calls to the restored shared account exactly once.
Native completion permits equal nonnegative tool/control deltas bounded by the
saved batch and original cap; all other account fields remain identical.
Observer-enabled tool accounting and paid child batches remain unsupported.

Default admission permits whole `write_file` batches only; this is explicit policy, not default application-wide recovery.
Problem: that rule also rejected ordinary tools during uninterrupted execution.
Decision: trusted `allow_unreconciled_batches=True` permits live ordinary/mixed batches under the existing gateway.
It commits the unchanged paid frontier without file recovery metadata; ACK binds its exact digest once.
Malformed/duplicate call identities refuse before effects. After process loss, unknown batches refuse replay
and mark consumption incomplete. A known file within a mixed batch cannot certify its other operations.
Alternatives rejected: guessing success from file existence, replaying the batch, or disabling the journal after recovery.
The original paid usage/files remain visible; this adds no record, cache, worker or writer.
Verify production SIGKILL/public Continue for a recovered file followed by a live read and for mixed-batch loss,
plus selected-store digest, ownership and accounting tests. Controlled providers prove boundaries, not model quality.
Asynchronous children can remain active when spawn returns; shared-root checkpoints still require quiescence.
This opt-in does not grant actor checkpoint/handoff support or relax unknown-usage checks.
Complete recovery history uses the existing exclusive large-frame stdio owner, original
frame bound/deadline and joined cancellation, including histories over PIPE_BUF.

Alternatives rejected: argument hashes merge distinct writes; replaying an append
can duplicate effects; accepting pre-tool quota grants extra execution; a second
Python journal splits ownership. No new store, worker or history cache is added.
Only the current prepared row references, UUIDs and domain metadata are retained,
within the gateway pending-byte bound, and gateway close releases the carrier.

Verification: `test_native_file_recovery.py` exercises the real gateway, file
handler and inspection owner over SQLite and native storage, including identical
arguments, foreign/missing proof and an uncommitted write followed by a user edit.
`test_native_file_root_recovery.py` SIGKILLs a real ROOT after publication using
selected C ABI storage, reopens under dead-writer proof, and verifies one append,
paid-prefix restoration, spent quota, continued HTTP, wallet settlement and later
user edits. These use a controlled provider and the same executor identity.

Measured ROOT carrier: 32,969 bytes of paid state, including 345 bytes of exact
operation metadata. Recovery queried the original publication without repeating
the tool; these are representation measurements, not WAL or latency claims.

Fresh-executor handoff follows accepted Continue lineage. Journal and the active
transaction prove the old pending writer dead before source retirement, target
creation and snapshot transfer commit together with one composite outbox record.
Passive replay applies the authenticated decision without granting promotion.
Original operations, fee origins, limits and deadline stay fixed while task and
account identities change. Live, unknown, inflight and foreign sources refuse.
Pending wallet handoff freezes the original invoice in its retired journal header.
The existing wallet sweeper reads that durable evidence and settles by original
task ID. See [billing](../DEVELOPER_RUNTIME.md#native-journal-store-boundary).

Repeated crashes retain the original producer. `validate_resume_producer` follows
accepted owner-scoped receipts with cycle/byte bounds and cancellation; caller
config grants no ancestry. Temporary IDs are bounded, reads scale with depth,
and no store/cache/worker is added. Rewriting producers loses evidence; arbitrary
older Turn rows bypass lineage; discarding original debt can refund paid work.

Verify actual v4 Continue, repeated SIGKILL and wallet recovery in
`test_native_file_root_recovery.py`, plus native live-writer/passive replay and
transaction faults. Controlled providers do not qualify task speed or deployment.
