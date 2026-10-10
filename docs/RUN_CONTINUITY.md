# Run continuity: interruption recovery and long-running execution

Design authority for making orchestration-backed execution survive process
restarts without human action and remain stable across long horizons, in
support of goal-driven self-evolution ([EVOLUTION_DESIGN.md](EVOLUTION_DESIGN.md)).
Domain behavior owners stay as mapped:
[modules/orchestration_dag.md](modules/orchestration_dag.md),
[modules/task_engine.md](modules/task_engine.md),
[modules/evolution.md](modules/evolution.md).

**Status convention.** Sections marked *running contract* describe behavior
that ships today and name their code owner. Sections marked *target* are
decided direction with a named phase; each phase lands with its own tests and
rewrites its section here from plan into contract in the same change. Git
history retains the superseded plan text.

## Problem

Self-evolution goals run for hours to days. Committed-effect recovery now spans
execution and conversation authority, but restart continuity still has gaps:

1. **Effect recovery needs a complete admission chain.** Retirement, normal
   conversation admission and ready-journal acknowledgement now survive
   preparation crashes. Unproven accepted config, changed human decisions,
   or a new effect without a complete successor seed remain refusal boundaries.
2. **The likeliest interruption point refuses recovery.** A run interrupted
   while a role invocation was in flight leaves an unmatched agent admission
   in the execution journal; `read_journal` raises `RecoveryRefused` because
   the external effects are unknown. Long runs spend most of their wall time
   inside model/tool invocations, so most real crashes land exactly here.
3. **Goal continuity needs complete evidence.** Clean v2 launches and settled
   receipts can rebind to a normal successor Goal. Older launches, an incomplete
   inherited seed, or a changed human/Turn decision remain terminal/refused.
4. **Evolution in-flight effects remain unresolved.** Generic stages now use
   durable inline tasks with confirmed birth records, ordinary event persistence
   and best-effort running checkpoints. A lost executor still needs attention:
   these records preserve diagnostic evidence but cannot prove the outcome of
   an external action that was in flight at interruption.

A secondary continuity gap: the invocation-tree budget account is live-only.
Crash-persistent reservation stays rejected, but a recovered run currently
re-derives consumption from domain counters ad hoc rather than from one rule.

## Decision

### D1 — Continuity policy is launch-stamped run metadata *(running contract, phase 1)*

A durable run's continuity policy is fixed at creation by its launching
domain and persisted with the run row. Phase 1 carries the policy on the
launch-stamped `created_by` provenance field: the composition root passes the
continuity pass one explicit registry of `auto` domains —
`chat_goal_mode` (`lib/goal_runs/contract.py` is its single source of truth)
— and every other run is `manual`. The recovery path never infers policy
from run names, orchestration ids, definition content, or event names. Owner
identity for any successor comes from the durable run row, exactly as the
explicit resume path reads it today — never from a process-global or
hardcoded user. A dedicated per-run policy column is the enterprise seam if a
per-run override is ever needed; the registry deliberately cannot express
one today.

### D2 — Boot continuity pass performs the existing resume protocol *(running contract, phase 1)*

Startup's retire maintenance operation (`orchestration.run.retire_interrupted_all`)
settles lost workers before clients reconnect. In that same transaction it marks
restart-interrupted Chat Goals as `continuity_pending` in private run state and
indexes them. SQL uses the v66 expansion and a partial index; native storage uses
a default-false field and the contract's pending index. The bounded result contains
fresh retirements plus up to 1,000 durably pending descriptors. The process-local
post-ready slot is delivery, while the stored election is authority: a crash
before command admission or a capacity deferral is re-elected on next startup.

Problem: retirement was durable while its only candidate list was process-local.
Decision: keep the pending bit with the run, and close it atomically with a source
recovery claim, `continuity_resumed`, `continuity_refused`, or
`continuity_exhausted`. `continuity_deferred` preserves election. Retryable typed
storage failures, including wrapped repository failures, defer rather than close.
Alternatives: rewriting old terminal runs would change their meaning; scanning
all historical terminal runs on each boot would charge startup for history;
independent queue records would need another ownership and deletion lifecycle.
The pending bit and bounded index avoid those costs and survive storage WAL replay.
A source audit or deletion releases only its own Goal active claim; it cannot
release the successor's claim on the same conversation.

Pending growth is at most one index entry per elected source, reclaimed by the
closing transaction or source deletion. No worker, timer, or queue process is
added. Five isolated sweeps of 100 synthetic pending Goals measured median
1.55 ms (SQLite) and 9.47 ms (native), returning 8,401 JSON bytes. Peak traced
Python allocation was 178,232 / 175,839 bytes respectively; no models ran, and
these are neither process RSS nor end-to-end executor latency. The SQLite query
plan proves use of the partial pending index. Offline migration carries the bit
and binds optional source-column bytes; absent older columns mean false, never
an invented election. Imported closure receipts reclaim the native pending index.
Existing historical terminal runs are not silently reopened by schema expansion.

For each candidate whose stamped domain is `auto`, the pass performs exactly
the explicit-resume protocol (`load_recovery_records` → deterministic
`recovery_run_id` successor → `record_recovery_claim` on the source → spawn
with `recovery_records`), binding the successor to the owner recorded on the
run row. Journal validation is unchanged: a clean seed resumes, a refused
seed leaves the source settled. Every outcome lands as a typed durable audit
event on the source run's stream: `continuity_resumed` (with the successor
id), `continuity_refused` (with reason), `continuity_deferred` (per-boot
capacity), `continuity_exhausted` (crash-loop guard). These are durable
replay-visible facts, not log lines; a generic Studio timeline projection is
deliberately not added by phase 1.

Bounds and guards: candidates are processed sequentially (spawning is cheap;
execution concurrency stays governed by the task runtime) with a per-boot
resume cap of `TOFU_CONTINUITY_AUTO_RESUME_MAX` (positive override, bounded by
the shared task structural ceiling); otherwise the cap follows
`TOFU_AGENT_WORKERS`, the positive inflight limit and the process memory ceiling
from the cached launch resource policy. Missing, malformed
or non-positive boot overrides use that same automatic default. This bounds
recovery admissions and does not change normal worker dispatch. A fixed 16/64 recovery ceiling was
rejected because it strands recoverable tasks on larger machines after restart;
overflow lands `continuity_deferred` and remains pending for next startup. A
candidate already carrying a recovery claim is skipped, so repeated passes
never spawn twice. The crash-loop guard walks the lineage's journal parent
links (bounded at 16) and refuses the resume when 3 or more links retired
within 600 seconds — healthy restarts days apart never trip it, and
exhaustion leaves the run settled with `continuity_exhausted`. Operator kill
switch: `TOFU_CONTINUITY_AUTO_RESUME=off`. The pass is fail-soft per
candidate and overall: it runs after readiness and must never wedge the
process. No new resident daemon, timer, or queue is added.

Capacity verification: synthetic shared probes for 2 CPUs/8 GiB and
128 CPUs/256 GiB (half the memory available) resolve to 2 and 48 boot
admissions respectively under the personal policy. An 18-run storage-backed
recovery test verifies all successors and idempotent replay with a configured
32-slot envelope. A 10,000-call resolution microbenchmark with explicit
128-slot settings averaged 42.22 µs/call; this is policy resolution, not model
execution throughput. The change adds no resident worker or persistent data.

### D3 — Chat Goals rebind through normal conversation commands *(running contract)*

Problem: Studio recovery cannot bind a chat Goal's conversation, attempt and
GoalRun lifecycle. Decision: `lib/goal_runs/recovery.py::resume_chat_goal` is
registered explicitly for `chat_goal_mode` on the post-ready continuity pass.
The production pass now resolves its default owner-bound run-service factory;
missing an injected factory no longer skips every candidate. Other domains
retain their explicit generic recovery protocol.

A source must have the exact restart interruption, a valid v2 launch archive,
and fully settled agent receipts. The source attempt/Turn/task binding remains
unchanged; a later Goal is allowed only when its accepted recovery-command
chain proves an unfinished pre-effect handoff from that source. A
bounded conversation snapshot selects the source lane's current tail. The
ordinary conversation command then atomically checks that exact parent is
still the lane tail, that its settlement permits continuation, that the lane
is idle and that no newer human input is queued. SQL and native creation both
honor `require_parent_not_stopped`; only `interrupted/server_restart` joins
completed/failed parents as eligible. Human Stop and live parents stay rejected.
The cancellation watermark follows the source's accepted command, so Stop
before recovery preparation also wins; the new executor's execution clock
and resources remain fresh. Public commands cannot inject the recovery source
or its cancellation watermark.

The source retains its honest `failed/worker_lost` terminal meaning. A stable
source-derived command id elects one normal continuation input/output pair
and one bound executor. Its new GoalRun retains the full objective. Before
model admission the executor revalidates its accepted attempt identity, restores
the archived launch/messages/checklist/child policy and claims a new journal
with inherited committed receipts and a parent link. Replay restores each
committed worker working set without repeating model/tool work. Fresh native
route groups are minted through normal task/child creation, never recovered
from stale handles. Source claims are idempotent and stay with source history.

Problem: acknowledging the source when a task was merely bound closed its
pending election before the successor had a recoverable journal. Decision:
`create_goal_execution_journal` commits the complete header, inherited seed and
ready marker, then the source acknowledgement, before any model/tool admission.
Boot admission without that acknowledgement writes `continuity_deferred` with
`awaiting_successor_journal`; its `resumed` summary counts command admission,
not durable readiness. A live accepted executor is reused or deferred, never
spawned again. A crash after ready but before acknowledgement reconciles the
validated child archive and leaves that child's own election responsible for replay.

Before ready, recovery walks at most 16 ordinary continuation pairs to the
original source. Every abandoned attempt must be `interrupted/server_restart`;
its owner-proved private accepted config must carry the exact server-authored
source/objective/tenant, and its command ID must bind the durable predecessor.
Missing Goals, partial header chunks and interrupted inherited-receipt copies
are pre-effect preparation. A ready marker or new effect admission prevents
rewinding the original source. A fresh normal command can replace only a proven
pre-effect handoff, retaining the original Stop watermark and frozen seed.
Alternatives: trusting public IDs/virtual actors permits forged recovery intent;
reusing an interrupted attempt reopens terminal state; acknowledging task birth
alone loses election on a second crash. The private config getter is opt-in,
owner-filtered and absent from public HTTP reads; SQL and native share its contract.
Abandoned turn pairs and partial journals follow existing conversation/run deletion;
no separate queue, daemon or lifecycle is introduced.
Five isolated recoveries after one pre-Goal handoff crash, each with a 1 MiB
archived message, measured median 93.28 ms on SQLite and 606.90 ms on native
for chain proof, fresh command binding, journal publication and acknowledgement.
Peak traced Python allocation was 20,732,402 / 20,746,943 bytes respectively;
this includes tracing overhead, no model execution and no process-RSS claim.

Alternatives considered: reopening the old terminal Goal changes an already
observed outcome; a standalone Studio worker loses conversation authority;
starting from the visible transcript loses compacted tool context; replaying
an unresolved agent can repeat external effects. Fresh normal commands preserve
one lifecycle and its existing CAS, cancellation, deletion and rollback seams.
No daemon, polling timer or extra execution pool is added. Per-boot capacity
and the crash-loop guard remain D2's authority; chunked launch parent links are
now decoded across page boundaries before lineage counting.

Capacity measurement: five synthetic isolated SQLite recoveries with a 1 MiB
launch message and one 1 MiB completed working-set receipt measured median
145.01 ms for source validation, normal command binding, successor Goal start
and inherited journal publication. The successor used 2,801,552 wire-event
bytes, including the copied launch and receipt. Peak traced Python allocation
across the five samples was 50,959,279 bytes; this is not process RSS. No model
ran in this measurement. Recovery copies committed receipts once per successor;
durable growth is proportional to those receipt sizes plus one normal turn pair,
under the run/conversation's existing deletion lifecycle.

Limits: unresolved model/tool invocations still refuse under D4. An interrupted
inherited-seed copy is replaced only through the proven pre-effect handoff above,
using the original complete seed. Human input, Stop and unproved lane changes
remain admission fences. Preparation chains beyond the structural proof bound refuse.
Branching conversations retain the existing snapshot topology fallback.
Automatic continuity is not a proof of research quality or benchmark dominance.
Tests: `tests/goals/test_goal_handoff_recovery.py` covers pre-bind, pre-Goal,
partial header, inherited copy, pre-ready and ready-before-ack crashes, repeated
preparation, private config ownership and public forgery.
`tests/goals/test_goal_conversation_recovery.py` uses real SQLite/native
stores and a source-matched native engine; PostgreSQL shares the same fixture
and runs when its isolated test binaries are available.

### D3a — Chat Goal launch and effect archives *(running contract)*

Problem: chat Goals previously persisted lifecycle events without execution
journals. A worker result alone cannot restore the model's tool history,
compacted working set or checklist. Decision: `lib/goal_runs/execution_journal.py`
claims a launch on the existing owner-bound orchestration event stream before
model dispatch. The shared journal header carries the immutable launch plus
its digest: original objective, model messages, resolved launch policy/tool
declarations, initial checklist and conversation/Turn/attempt/owner bindings.
Launch format v2 also freezes the ordinary child's inherited model configuration,
owner-authorized model reference/provider preference, execution-tool discovery
policy, contract documents and script permissions. The public projections in
`lib/tasks_pkg/child_invocation.py` own both normal launch and archival; after
the durable claim succeeds, every Goal leaf consumes the frozen policy instead
of the mutable parent config. A conflicting model/project/thinking selection
refuses before child allocation. Credentials, tool environment, principal
handles and native slot groups are not copied: recovery must rebind resources
through their existing owners. Version 1 archives lack this input policy and
cannot reconstruct a Goal; the generic resume guard still recognizes them.
`goal_execution_launch_contract()` in `lib/goal_runs/contract.py` owns its
machine-readable schema. Competing starters use distinct launch witnesses at
the same sequence, so only one may admit effects; terminal/superseded runs
reject subsequent effect admissions through ordinary atomic event projection.

The cross-process lifecycle vocabulary lives in `run_status.py`; the event
registry and storage election consume that same dependency-light contract.
Production leaves now publish `execution_effect_prepared` before child setup.
The child invocation owner awaits `execution_effect_started` only after setup
and execution-capacity waiting, immediately before entering its model/tool
executor. A restart with preparation alone can reconstruct the leaf from the
sealed launch; a started effect without completion remains ambiguous. Admission
publication failure releases child ingress, budget, registry and monitor through
the ordinary teardown and dispatches nothing. Preparation cannot complete a
receipt, change its input hash or admit twice. Injected/custom runners retain
admission before their opaque call, because their setup boundary is unproven.
This resolves setup crashes without treating model/tool timeouts as replayable;
inferring dispatch from a tool-free display preview was rejected. Each new leaf
adds one small durable event on the existing deletion lifecycle; 5 synthetic
1,000-leaf runs measured 175–177 UTF-8 payload bytes per preparation (before
storage framing). No resident worker, queue, cache or timer is added.

Conversation-policy agent receipts include a detached model working set and
checklist snapshot with the completed result in the same durable event.
The existing runner restores this state when replaying a committed result,
without another model/tool call; normal worker/verifier isolation remains.
Missing context, an invalid launch digest, changed ownership/binding, or an
unresolved model invocation refuses replay. Handoff-policy journals cannot
consume conversation-policy receipts. The ordinary Studio resume path refuses
archived chat Goals: D3 owns their conversation/lifecycle rebind, and a
transient standalone worker is not a valid substitute.

Alternatives considered: removing the journal's worker-context guard without
storing context loses tool evidence; output-only receipts lose compaction and
checklist state; storing all parent config leaks runtime handles and duplicates
child policy; re-deriving configuration after restart can silently change tool
permissions or provider selection; a separate recovery store introduces another lifecycle and
deletion owner. Archives live with the durable run and are reclaimed by its
existing delete operation. `lib/goal_runs/journal_codec.py` splits large logical
events into digest-bound 512 KiB fragments, encoded below the native 1 MiB
document limit. Only the final reference commits the logical event; partial
completion writes leave the preceding effect unresolved. The Goal publisher
uses the shared bounded storage retry for timeout, busy and unavailable errors:
it resends the identical owner/run/sequence/payload packet and advances the
cursor only on acknowledgement. A lost response after commit reconciles through
the existing idempotent projection; it never re-runs the model/tool effect.
Ready-source acknowledgement likewise reconciles its immutable successor claim.
The default three delays are 1/2/4 seconds, interrupted by owner Stop; conflicts,
false admission and exhausted retries still revoke the journal and preserve
ambiguity. No new receipt store, queue or timer is introduced. Immediate failure
on a temporary authority outage and blind effect re-execution were rejected.
Native and SQL event
pages stop before their 8 MiB byte boundary and continue from the last physical
sequence, retaining every packet without clipping context. PostgreSQL/SQLite
UTF-8 sizing stays behind the SQL adapter boundary. No additional resident
worker, queue or timer starts.
Complete context is retained per agent receipt, so durable growth follows
the sum of recorded working-set sizes, not just output text. A synthetic
1 MiB-message launch and completed-context receipt against isolated SQLite are
covered by the archive tests, including Unicode and history exceeding one page.
The packet regression bounds each wire event below 1 MiB. With allocation
tracing enabled, five synthetic isolated SQLite starts measured median launch
publication 30.16 ms and completed-receipt publication 15.67 ms, maximum wire
packet 699,198 bytes, and peak traced allocation 45,691,734 bytes across all five
starts. Each start plus one receipt used eleven physical events. Traced Python
allocation is not process RSS; this measures archive overhead, not model
quality or native-storage throughput.
Tests: `tests/goals/test_goal_execution_journal.py` and the real child-invocation
replay case in `tests/goals/test_goal_worker_context.py`.

### D4a — Committed handoffs and read-only reviews resume *(running contract)*

`lib/evolution/stage_launch.py` archives the exact next-stage request before
`runs.py` CAS-commits its pointer alongside observed usage and the completed
stage receipt. A `preparing` run resumes this request through the existing
callback, sweep or program advance path. Review evidence and repair feedback
survive loss of the source task; an ambiguous archive acknowledgement reuses
the first immutable input. The run CAS rejects concurrent unstarted losers
before any provider dispatch. Missing or foreign requests refuse dispatch;
owner cancellation and explicit limits remain fences. A missing read-only
reviewer may be re-admitted after its launch-stamped process is confirmed dead
in the same host/boot/PID namespace. Two recoveries per episode are the new-run
default; legacy unstamped owners, living owners and finite token budgets refuse
replay. Lost usage is an explicit lower bound, excluded from measured cost and
efficiency learning. Recovery admission also checks scientific protocol, required outcome, episode and attempt against the sealed executor archive before allocating a replacement; changed scientific acceptance refuses recovery and preserves incomplete usage truth. Terminal JSON syntax, narrowly admitted text-limit refusals, or missing required-original inspection may re-admit only the same frozen read-only review,
under its separate finite format-retry allowance and known-cost receipts.
Valid negative verdicts never authorize a format retry. Executor continuation remains the D4/D5 target below.
Full mechanics, growth bounds and alternatives live in
[EVOLUTION_DESIGN.md](EVOLUTION_DESIGN.md).

Problem: a frozen prompt could resume with regenerated, broader configuration.
Decision: retain the first configuration; reject regeneration and hook expansion
as authorities, while allowing limits to tighten and explicit model successors.
New handoffs also freeze pre-hook task configuration, with a digest witnessed
by the run's launch reference. Structured model references stay fixed; offering
labels resolve through the task engine and must match its task binding. Expired
frozen deadlines refuse allocation. Dispatch preserves project/tool settings and
only tightens token/tool ceilings and the original deadline; hooks cannot
expand declared permissions or those ceilings before submission. Prepared
input restoration validates the same frozen grant. Prompt-only legacy intents
retain their existing protocol, without gaining a configuration-freeze claim.
An explicit owner model change before task allocation prepares a linked
successor under the same run CAS: original prompt/settings/limits remain, the
approved logical model changes, and both requests survive for decision lineage.
Allocated tasks retain their route. Source upgrade alone cannot reselect the
prepared task configuration. This does not admit isolated project execution
or replace path/tool authorization, aggregate quotas or effect publication.

Generic executing/reviewing stages separately commit a `stage_dispatch` binding
and submission admission. Problem: cold local task preparation consumed the same five-second budget as birth persistence and confirmation, refusing dispatch despite a valid birth. Decision: the task registry reports only its birth-checkpoint elapsed time; evolution charges that time and confirmation to one bounded storage budget, while local preparation obeys the original user wall-clock deadline. Missing/non-finite timing, unconfirmed births and expired deadlines still refuse dispatch. Increasing the fixed total timeout or exempting persistence from admission was rejected: both obscure storage stalls or weaken the commit boundary. A deterministic six-second preparation probe now admits an unbounded task; a 5.01-second checkpoint or expired three-second user deadline refuses without spawning (`test_evolution_stage_dispatch.py`), with no added worker, retry loop or resident capacity. A bound resident carrier resumes in its exact live
process with its unchanged birth version. `stage_inputs.py` additionally saves
complete post-hook messages and portable logical configuration before binding:
owner-scoped, digest-checked and immutable. Small inputs retain the inline format;
large inputs use immutable 256 KiB frames and a version-2 manifest. Each frame
has its own SHA-256, index and exact byte length; the manifest and dispatch
reference bind the complete canonical body digest. All frames are acknowledged
before the manifest, and the manifest before the dispatch binding. Same-byte
retries use creation CAS and verify conflicts; changed inputs refuse. Inline
configuration retains its existing bounded credential-free validation.
Problem: a record-size ceiling prevented large post-hook contexts from starting
or restoring. Decision: frame the complete immutable input using existing
owner-scoped record operations, without truncation or a total context-size cap.
Alternatives: prompt clipping loses experiments and provenance; raising the
shared record limit enlarges every storage operation. Neither is accepted.
Storage grows with retained input size (base64 frames cost about 4/3 of raw bytes),
not checkpoint rounds. Unpublished frames remain at deterministic content-bound
addresses for retry; they confer no dispatch authority and are never mistaken
for a complete archive. No worker, timer or second storage backend is added.
Measurement: an isolated native development-engine run archived an 8,001,030-byte
post-hook input in 31 frames and submitted in about 3.01 s, with no model call.
Exact Unicode round-trip, idempotent retry, foreign-owner refusal and preservation
through working-context cleanup passed. This single synthetic launch measures
admission plus storage, not model throughput or a statistical performance claim.
After proven local process death, `pending_recovery.py` requires startup to have
administrator-interrupted precisely that pending birth (version +1), with no
abort, output or execution evidence. Each provably unsubmitted birth can be restored
using the shared task factory without repeating hooks; episode, logical attempt
and absolute deadline stay fixed. A deterministic new physical task identity
lets concurrent reapers and ambiguous binding ACKs share one pending carrier.
Its run CAS records the old carrier as `submission_voided`: zero tokens/tools,
observed waiting time, no outcome or verdict. Restorable-task cancellation
requires an acknowledged durable abort even before its input archive exists;
failed persistence still signals local resources but never acknowledges durability.
One further run CAS admits submission. Admitted/legacy stages, unknown owners,
changed checkpoints, exhausted budgets and incomplete replacement lineage refuse.
Every replacement archives its own complete input before binding, including its
physical task identity and creation time. The original post-hook messages,
configuration, episode and attempt remain unchanged. A trailing chain of distinct
zero-consumption `submission_voided` receipts must match the replacement binding
and original executing episode. A further restart proves the latest birth again;
older retirement evidence never authorizes a new submission. Same-process retries
reuse the same construction input digest and identical immutable archive; conflicts
with changed input refuse. No fixed restart count substitutes for these proofs.
Problem: a second pre-submission restart previously stranded safe pending work.
Decision: independently attest each replacement and retain the chain under the
existing run CAS. Alternatives: reusing the original birth proof or replaying an
admitted carrier would permit duplicate effects and are rejected.
Measurement: the fault-injection test retires four physical carriers across three
replacement crashes, then dispatches one executor with one episode/attempt. Each
replacement adds one immutable input archive and one small voided receipt;
input bytes are bounded by the existing stage-input limit and the run header by
its existing record limit. No worker, timer, cache or unbounded in-memory queue.
This closes a provably unsubmitted gap; physical send ambiguity and interrupted
executors remain the D4/D5 target. Alternatives rejected: interpreting pending
status as no effects, rerunning opaque hooks, or resetting the experiment budget.

### D4b — Committed terminal outcomes resume *(running contract)*

Before run receipt progression, `terminal_checkpoint.py` prepares full originals
and commits their frozen manifest with the owned task result. `stage_outcomes.py`
materializes terminal facts, consumption and exact review input from that commit.
Recovery consumes verified committed outcomes without replaying the executor;
all acknowledged parts can seal an interrupted archive. The headless runtime gate injects a handoff interruption after a real task terminal archive, restarts isolated SQLite/TofuDB storage, opens the same authority in an independent Python process with no original task registry to reconstruct the frozen task from that archive, and then invokes the authenticated reconcile endpoint to finish independent review with one executor invocation. This exercises storage cold readback; the application process remains alive and does not prove full process restart continuity. A terminal archive binding refusal passes through ordinary settlement: the episode becomes aborted without a verdict, usage remains incomplete, and the run needs attention without replay. Missing parts require
attention after owner loss, while known consumption remains accounted. Unknown
in-flight effects retain the D4/D5 target below. Owner cancellation, canonical
terminal verdicts, partial usage, and run CAS remain fences. Full mechanics,
resource bounds, and verification live in [STAGE_OUTCOMES.md](STAGE_OUTCOMES.md).

### D4c — Terminal review format recovery *(running contract)*

Terminal review format refusals share a distinct `max_review_format_retries` allowance:
new runs default to two retries per execution episode, 0 disables, and legacy
runs without the field stay disabled. Programs inherit explicit overrides.
**Problem.** A provider may prepend prose or exceed a schema text limit even when
the physical request carries `response_format=json_object`, losing a completed execution's audit.
**Decision.** With format recovery enabled, one complete JSON verdict object
can be projected from a prose/fenced wrapper; multiple JSON values, duplicate
keys, incomplete objects and trailing prose are refused. The sealed original
review must match its digest, task and frozen execution intent. Original IDs,
outcomes and all other schema constraints stay fixed. Only overlong declared
evidence references/consumer notes become short digest-bound JSON pointers.
A `review_format_normalization` receipt binds the original source, exact JSON
range and replaced field paths; complete text remains in the outcome archive.
This representation costs no model request and creates no new source coverage.
It preserves negative/unknown judgments and the existing requirement aggregation.
Unread sources still make acceptance unknown; zero/legacy policy stays disabled.
Refusals retain the existing finite `review_format_retry` fallback: known costs,
original deadline and fresh source reads through an ordinary read-only judge.
Negative pure-JSON judgments and other schema/audit failures never authorize
that fallback. No additional worker, cache or timer is introduced. **Alternatives.**
Clipping evidence loses qualifications; a fresh judge for formatting rereads the
same sources and consumes subsequent experiment budgets. Lossless references
keep the original judgment available without manufacturing a corrected verdict.

### D4d — Durable stage evidence before terminal output *(running contract)*

**Problem.** Storage-free transient stages omitted executing checkpoints and
request events, leaving progress resident until terminal archival. **Decision.**
Generic execution and review use existing durable inline task composition:
`runs._launch` confirms one compact `task_results.replay_get` pending record
before episode creation, binding and dispatch. Task identity, owner, empty
conversation and a positive integer version must agree; missing, mismatched,
terminal or failed reads reject admission. The extra confirmation uses one
bounded 500 ms point read. Unstarted rejection closes the ordinary task owners;
its unbound callback cannot advance a committed preparing request.

The task keeps inline model context while existing event/checkpoint owners
persist progress. Running checkpoints retain their existing best-effort 500 ms
policy; terminal original-output archives remain mandatory progression evidence.
A controlled native 16-round fixture serialized its retained task-result row
to 52,446 bytes; complete cold stage admission took 0.85 seconds including
planning/setup, with no provider invocation. This is not a total heap/WAL
measurement or a bound on arbitrary tool content.
This is durable diagnostic progress, not a promise to replay unresolved external
effects or recover precompaction tool text. A checkpoint never authorizes review,
spend or a success verdict. Research keeps its own durable pipeline. Embedded
storage-free tasks outside evolution retain their existing composition.

The required `TerminalDelivery` port owns managed-stage acknowledgement/retries;
the durable observer delegates to that same idempotent port. An archive failure
after task-result commit keeps terminal retry debt and heavy evidence until the
required receiver acknowledges; a durable result row alone is insufficient. No second consumer,
worker, timer, queue or store is added. **Alternatives.** A parallel evolution
checkpoint store duplicates task lifecycle; treating partial state as terminal
output fabricates a verdict; blindly repeating an interrupted executor duplicates
unknown effects. Focused guards: `test_evolution_durable_stages.py` plus stage
launch, callback-race, review-recovery and ordinary task persistence tests.

### Observed consumption survives interrupted stages

Problem: running checkpoints retained presentation state but omitted observed
consumption, so confirmed process loss discarded known model/tool costs.
Decision: an admitted Evolution carrier attaches its server-owned run, goal,
plan, stage, task, launch intent and birth version to the existing task checkpoint.
`bridge.execution_checkpoint` uses the canonical root consumption snapshot;
physical tool admissions count even before their presentation rounds finish.
The wire authority is `execution_checkpoint` in `evolution_run_v1.schema.json`.
After confirmed process loss, an owner-scoped compact task-result metadata read
must match every binding field and a version newer than birth. Valid partial costs
become one `checkpoint_lower_bound` receipt with `actuals_complete=false`, including
cancellation. Run and episode totals include it once; recovered read-only reviews
charge their own usage separately. Missing, corrupt, foreign or stale snapshots
contribute no invented cost. These receipts grant no verdict, training,
source coverage, tool outcome or execution replay authority. The existing UI
already labels incomplete totals with ≥. Ordinary checkpoints are unchanged.
Alternatives: fabricating zero consumption understates spending; treating partial
cost as complete permits unjustified budget admission. Neither is accepted.
No new store, worker or extra checkpoint write is introduced. One compact metadata
read per reconciliation visit, with a 500ms deadline, occurs only on confirmed
loss. The snapshot adds a
fixed binding and three integer counters to the existing metadata; checkpoint
frequency and record lifecycle remain unchanged. A warm synthetic 1,000-snapshot
measurement used 399 serialized bytes, 369.61µs per snapshot with allocation
tracing enabled, and 10,212 bytes peak traced allocation; it excludes storage I/O
and does not measure real model throughput. `test_evolution_checkpoint_actuals`
verifies real owned writes, lower-bound settlement, corruption refusal and no
executor replay. A lost review with 80 observed tokens, after a 15-token executor
and before a 15-token successor, settles at ≥110 tokens, never 30 or a complete 110.

### Durable working contexts at the shared execution boundary

Problem: a diagnostic checkpoint retained output but not the complete model
prefix or the boundary at which execution stopped. Repeating the original prompt
could repeat an already-issued command or discard experiment constraints.
Decision: Evolution installs one sink on the existing root invocation checkpoint
port. It publishes the exact JSON message working set, checklist and reported
usage before model/tool execution and at tool-round settlement. The root owns
`model_in_flight`, `tools_in_flight`, `ready` and `terminal_delivery`; this module
creates no alternate execution loop. Failed publication admits no subsequent
model/tool effect. Existing checkpoint observers run after durable publication.

`stage_inputs.py` owns this reconstructible transport in
`evolution.stage_checkpoints.v1` and `evolution.stage_checkpoint_parts.v1`, separate
from immutable birth inputs and verified outcomes. Working sets have no one-record
size ceiling: 256KiB binary frames with base64 envelopes stay below the existing
record limit. Two reusable banks prevent growth with round count. A bank's
high-water manifest precedes writes so failed large publications do not lose
cleanup knowledge. Unchanged bodies reuse frames only when the live writer has the acknowledged
current pointer version and digest; every new phase still publishes its pointer.
Complete parts precede the pointer CAS; an interrupted write
leaves the previous published bank intact. Shrinking sets reclaim unused addresses
at settlement through owner/task-prefixed indexed pages. Verified terminal capture
releases both banks; failed cleanup retains explicit addresses for later cleanup.
Terminal memory release detaches the sink closure only after persistence is
acknowledged, preserving earlier observers and freeing its lock/carrier references.

Reads bind owner, task, run, goal, plan, launch, birth version and immutable input
digest, verify every frame and the full SHA, then recheck pointer/version. Changed,
missing or corrupt data refuses. Message objects retain their key order. Inspection
also works after `needs_attention` using the frozen stage; it writes no verdict or
admission. A stored `ready` prefix alone does not prove child/background quiescence
or authorize executor replay. Those are separate continuation admission obligations.

Working metadata can contain newer observed costs than the throttled diagnostic
row. Confirmed loss takes component-wise maxima for the same identity, never sums
of duplicated snapshots, and records source versions with incomplete cost truth.
Alternatives: unbounded immutable context copies would grow with every model round;
one mutable large record would reject large contexts; treating model prefix or
phase labels as completion/replay proof would permit unjustified effects.
Verification: `test_evolution_working_checkpoint.py` covers multi-megabyte Unicode
prefixes, bounded banks across repeated rounds, shrink/terminal reclamation,
corruption and pointer changes, write-ahead refusal before tool dispatch, loss
inspection and cost reconciliation. On a private native daemon built in the dev
profile, four distinct 4,003,969-byte synthetic contexts used 16 frames per bank
and averaged 758.89ms per publication. Four unchanged-body publications averaged
22.29ms. One unchanged-body publication peaked at 18,746,549 traced bytes,
including encoding and client copies; these are synthetic storage measurements,
not real model throughput. Shrink and terminal release preserved immutable input.
No worker, timer or startup capability loads.

### Outstanding work accompanies the model prefix

Problem: `ready` describes a loop boundary, but a delegated child, reserved model
request or explicitly backgrounded command may still exist. A model prefix alone
cannot distinguish these facts after process loss.
Problem: a pending request count loses the distinction between admission and
physical dispatch, and a child may close while its model request is unfinished.
Decision: account snapshots additionally retain each unfinished model admission's
account-local ordinal, original task ID, dispatch flag and reserved tokens under
the same account lock. Old snapshots may omit `model_attempts`; omission means
unrecorded details. Settled attempts disappear, and ordinals never recycle within
an account. Reservation amounts remain authorization holds, not consumed tokens
or guaranteed provider usage bounds. No request body, credentials or task object
is retained in the snapshot. Alternatives: deriving dispatch from task status or
assuming zero pending requests from an empty active-child list loses late work.
The existing working-context publication retains this observation without extra
writes; it does not make an earlier checkpoint current or authorize recovery.
Measurement: a synthetic one-child/one-lease account (five-character child ID)
added 99 serialized JSON bytes; 10,000 lock-only snapshots averaged about 1.14 µs
on this host. Snapshot space scales with existing unfinished admissions; no new
resident queue, worker or persistence cycle is introduced. This is a local
microbenchmark with no model calls, not a throughput or scientific-quality claim.
Decision: new working contexts include optional `continuity_effects` from the wire
contract. The shared execution account snapshots its root, live child identities,
pending model-request count and unknown-usage flag under its existing lock. The
background registry snapshots command IDs/statuses for the exact owner and launch
task, with its recovery-incomplete flag under its own existing lock. No task object,
result body, command text, credentials or cancellation closure enters the inventory.
An absent execution account is explicit null. Legacy contexts without the inventory
remain readable but cannot certify absence of outstanding work.

The observations do not form a new execution journal or a globally atomic cut:
account and command registry have separate lifecycles. Neither empty arrays,
completed command labels, `ready`, nor zero pending requests grant replay authority.
Unknown consumption remains explicit after its request closes. A future admission
must independently resolve these facts against owned producers and frozen policy.
Alternatives: infer quiescence from phase labels; scan task registries without the
canonical account; retain live resource objects in the model prefix. All lose
ownership/completeness or prolong resource lifetimes. Reads change no lease, task
or command state; new inventory changes participate in the existing body digest,
so an unchanged message list cannot reuse stale effect metadata.
Verification: working-context and execution-budget suites cover simultaneous live
children/requests/background commands, later closure, unknown usage, task-isolated
completed identities and registry recovery. The added work uses existing locks,
no I/O, worker, timer or startup account creation; payload growth follows the
existing bounded command registry and resource-sized child/model admissions.
A synthetic root with one child and one registered command produced 278 serialized
inventory bytes; 1,000 snapshots averaged 2.21µs each on this host. This excludes
working-context serialization/storage and does not measure model throughput.

### Working checkpoint progress cannot regress

Problem: pointer CAS prevented concurrent overwrite but did not reject an older
round or a backward phase submitted after a newer checkpoint. A stale callback
could otherwise overwrite unresolved tool intent with an earlier `ready` label.
Decision: the phase constants and explicit progress order are defined once in the
`working_checkpoint` contract. Publication rejects lower rounds, lower phase order
within the same round, and every transition out of `terminal_delivery`. Repeated
same-phase observations remain permitted; tool settlement advances the round,
so the ordinary loop can progress from tool intent to next-round readiness.
The guard runs before bank/manifests/parts/pointer writes. Versions and all earlier
context bytes remain unchanged on refusal. No label grants recovery admission.
Alternatives: CAS alone accepts sequential stale writers; silently keeping the old
pointer while returning success would let the caller execute without its declared
new intent. Both are rejected. Working-context, explicit artifact/review delivery
and shared-loop tests verify refusal and normal progression. No extra query, lock,
worker or stored field; the phase-order map comes from the cached schema authority.

### Reuse unchanged working-context frames

Problem: a small append to a multi-megabyte model prefix rewrote every frame,
because legacy frame envelopes repeated the complete-body digest.
Decision: optional v2 frame envelopes bind their own decoded bytes. Readers still
validate index, exact length, each frame SHA and the complete header SHA before
acceptance, then recheck pointer/version. Unversioned legacy frames retain their
original complete-body binding. New writers remember only acknowledged frame
hashes in each reusable bank, guarded by that bank's manifest version. Unchanged
frames can be skipped; changed or unacknowledged addresses must be written.
Before a changed write, its old remembered hash is removed so a lost acknowledgement
cannot authorize reuse of stale bytes. Shrink/cleanup invalidates affected bank
hashes; deletion verifies absence and retries typed transients at most twice, preserving command identity. Lost ACKs require absence; response-only ACKs were rejected. Terminal memory release drops the tiny map with the checkpoint sink.
No full prefix copy remains in this optimization cache; its size follows framed
context size and the same two-bank lifecycle. No additional query, worker or timer.
Alternatives: skip the final body digest (permits incorrect frame mixtures); trust
hashes across manifest changes or failed writes (forgets publication uncertainty).
Both are rejected. Tests cover unchanged frame versions, ambiguous write retries,
legacy reads, independently valid but wrong frame mixtures, shrink and release.

Measurement: matched scripts in separate original/modified checkouts used the same
native dev binary and 4,004,109-byte synthetic context (16 frames). Four small-append
publications after both banks warmed averaged 603.01ms before and 35.85ms after;
first four distinct publications, including cold banks, averaged 807.70ms and
479.32ms. Unchanged-body publications averaged 22.63ms and 20.67ms. These small
sequential storage measurements are not statistical significance, model throughput
or real research outcome evidence. Native shrink/release preserved immutable input.

### Model send intent and settlement witnesses

Problem: children may admit provider requests after a parent working checkpoint,
so its pending-request inventory can be stale before process death.
Decision: Evolution composition binds an injected observer to the shared account
before model admission. The physical-send port acknowledges an immutable
`dispatch_intent` under the frozen stage/owner/birth identity and account ordinal
before allowing transport, then rechecks cancellation, deadline and allowances
after the acknowledgement. Each attempt subsequently records one `settled` or
provably unsent `voided` observation, with reported token totals and explicit
usage completeness. Detached and late children retain the same original account
port. The core imports no repository; transient invocations remain unchanged.
An intent proves authorization to begin sending, not provider acceptance.
Reservations are not observed costs or guaranteed usage bounds.

The existing owner-scoped record operation provides creation CAS and exact-value
conflict checks. Lost intent acknowledgement blocks sending; a subsequent
unsent observation can resolve an intent that committed without acknowledgement.
Failed settlement observation releases leases and retains paid lower bounds,
marks account completeness false and preserves existing finite-budget refusal.
It never refunds paid usage or blocks teardown. Owner reads recheck the complete
frozen binding, not only the record address. Context-cache cleanup preserves
these witnesses, whose lifecycle follows retained stage evidence.
Alternatives: trust a parent checkpoint's empty inventory; infer dispatch from
missing task rows; require successful storage during teardown. These respectively
miss later child requests, guess effects, or leak live reservations.
The witnesses add no worker or replay permission. Automatic executor continuity
still needs tool/child lifecycle resolution and an authoritative quiescence fence.
Measurement: 201 synthetic native development-engine admissions (no model
calls) averaged 0.96 ms for the intent and 0.87 ms for settlement. The two events
serialized to about 1,092 JSON bytes per attempt, excluding storage/index overhead.
A three-page recovery read took about 119 ms: an old parent checkpoint of
37 tokens was raised to 2,449 by acknowledged account observations, with 2,412
reported request tokens and one additional unfinished attempt. Memory retains
one bounded page plus scalar counters; no total-history list or attempt set is
built. These local sequential measurements are not throughput guarantees.
Recovery streams the exact owner/task key range in bounded pages, validates
stage, intent, birth, account identity and canonical addresses. It counts distinct
observed attempts (including unfinished ones), sums terminal reported-token
observations and retains the largest observed shared-account token snapshot.
New send/settlement witnesses take that snapshot under the account lock; earlier
witnesses without it remain valid. Send intents add no request consumption and
reservation holds never enter observed totals.
It combines those observations with task/working-checkpoint token lower bounds by maximum,
never duplicate addition. A journal-only receipt records its observed count and
token sum and optional account snapshot without inventing a checkpoint version. Consumed task receipts retain
the existing run CAS fence. No journal count, complete individual usage report or
empty page proves total task consumption, a verdict or replay eligibility.
Problem: acknowledged late-child consumption was ignored when the parent
checkpoint was older or absent. Decision: retain it as another authenticated
lower bound. Alternatives: add overlapping totals, scan all owner history, or
promote settled requests into task completeness. These respectively double-count,
scale with unrelated goals, or ignore unknown tools/children.
Retained witness growth is linear in provider attempts and includes no request
body or output. Account locking serializes each witness with admission; this
small sequential storage measurement is not a provider-throughput benchmark.

### Shared child membership witnesses

Problem: child creation may happen after a parent checkpoint, and failed budget
attachment previously left an allocated child registered. Decision: Evolution
composition also injects a child observer into the shared account. Before granting
membership it acknowledges `attach_intent`, then rechecks the live control parent,
cancellation and deadline. Failed attachment emits `attachment_voided` when
possible and grants no membership. Account ordinals do not recycle after failures.
Detached Swarm carriers explicitly borrow their original live control identity;
joined children become their own control parents, so a closed child cannot admit
new descendants through an old proxy. Non-observed transient compositions keep
their existing admission behavior and add no storage writes.

Detachment records the original control parent/child identities and outstanding
model-request count and pending tool-executor count. New tool observations retain
the original control identity separately from the carrier's own identity, so a
Swarm proxy's unfinished tool remains attributed to the child that granted control.
Legacy records may omit these fields; absence never certifies zero pending tools.
Failed detachment observation still releases membership and
retains paid usage, but marks accounting incomplete. Detachment is not terminal
result, background-command completion, or a global quiescence certificate.
The observer retains frozen binding data, not task objects; active child ordinal
metadata is removed at detachment. Construction attaches budget before registering
conversation lineage; failure closes ingress, rejects the unstarted execution
session/any durable row and unregisters it through the existing manager port.
Alternatives: infer child absence from a stale checkpoint; discard only registry
state on admission failure; treat detachment as completed effects. These lose
later children, strand sessions/rows, or ignore unfinished external work.
The existing owner-scoped immutable record CAS carries witnesses outside the run
header; reads validate frozen stage, intent, birth, owner and ordinal identity.
Measurement: the native development-engine proof retained one attachment and one
detachment in 1,077 serialized JSON bytes (excluding storage/index overhead),
accurately reporting one pending model request at detachment. No model calls
were made. Active ordinal metadata contains only control IDs and ordinals, grows
with currently attached children, and is removed on close; historical witnesses
remain proportional to retained child attachments. No new worker, timer, model loop, or replay authorization is introduced.

### Tool admission follows live account ownership

Problem: tool reservation checked shared quotas without checking active ownership,
and a caller with a shared account could bypass it by omitting local limits.
Decision: atomic tool reservation validates the caller's declared live control
identity under the same account lock as quota charging. Closed children, proxies
of closed owners and cancelled owners receive `execution_owner_cancelled`; no
shared or local slot is charged. Live detached Swarm control carriers preserve
the original account identity. Missing local limits do not remove shared policy.
Unaccounted unbounded transient calls retain their existing fast path.
Alternatives: check only `task.aborted`; rely on a previous membership snapshot;
check ownership outside the reservation lock. These miss detached identities or
allow a close/reserve race. Existing quota failure reasons retain precedence.
Measurement: 10,000 synthetic in-memory admissions averaged about 12.07 µs
on this host, with no storage write or model call; this is not a handler-throughput
benchmark. No additional record, worker, pool or resident task is introduced. This admission
boundary does not certify handler result, hook effects or background completion;
tool-effect journaling and executor continuity remain separate unfinished work.

### Tool executor intent and return witnesses

Problem: a finished model round alone cannot identify which handler entered or
whether its original return was retained. Decision: Evolution composition injects
one tool observer into the shared account before any tool slot has been charged.
Bundle binding validates every port before changing any observer. Existing tool
charges, including restored charges with no journal ordinal, refuse late binding.
Problem: ordinal zero alone did not prove that tools had never run. Alternatives:
retroactively label old work as observed or install a partial bundle on refusal;
both lose the boundary between recorded and unrecorded execution. The common executor acknowledges an
immutable intent after quota admission and before resolver/handler entry, then
rechecks ownership, cancellation and deadlines. Intent failure refuses entry and
records `not_entered` when possible. Tool slots retain their existing no-refund
semantics. Return records bind a SHA-256 of the original text or portable JSON
before presentation clipping; unsupported values omit the digest, never invent it.
An escaping executor exception is observed separately. A returned error tuple or
background handle is still an executor return, not successful external work.

Pending tool observations participate in account snapshots and completeness.
Terminal observation failure releases pending state but marks completeness false
while preserving the handler's original result/error. These ports add no store
imports to the core and no writes to ordinary transient invocations. Payloads
retain identities/digests, not tool arguments, credentials or duplicate results.
Owner reads recheck frozen stage/intent/birth identity and ordinal. Text digests
encode bounded character slices, avoiding a complete extra byte copy; JSON is
incrementally encoded. Measurement: the native development-engine proof retained
two tool intent/terminal pairs in 2,370 JSON bytes, including original control
identity and an exact original-return digest,
excluding storage/index overhead, with no model calls.
Alternatives: infer success from displayed status; replay an unmatched intent;
keep pending leases when terminal persistence fails. These respectively confuse
background/error returns, guess effects, or leak execution state.
Registered pre/post-tool hook batches now enter the same optional account port
with an explicit observation scope and a distinct batch identity. Empty hook
registries allocate no account or record. Pre-hook intent refusal blocks entry;
post-hook refusal skips callbacks and retains the original result. Existing
callback exception handling, argument modification and result modification stay
owned by the hook layer. Terminal hook observations retain the count of caught
callback exceptions; a returned batch can contain failed callbacks. Legacy records
without this count remain unknown. One integer per terminal batch adds no writes
or workers. Alternatives: infer success from batch return or rely on ephemeral
logs; both lose durable evidence of swallowed callback errors. Hook observations charge no tool slot and never imply
that callback-spawned external work completed. Legacy scope-less records remain
executor-only observations. Native development-engine verification retained two
registered batches, including one caught error each, in 2,564 JSON bytes (excluding storage/index overhead), charged
zero extra tool slots and made no model calls. Additional growth is proportional
to registered batch executions; empty registries retain the existing zero-write
path. The witnesses grant no global quiescence or replay authority. Original-result
inspection and independently verified effect settlement remain required.

### Tool quota continuity after process loss

Problem: parent checkpoints could omit charged tool slots from later executions.
Decision: new tool executor witnesses retain the shared account's observed charged
slot counter at intent and terminal observation. Recovery streams only the exact
owner/task key range, validates frozen binding and canonical ordinal addresses,
and takes the maximum charged counter across witnesses and older checkpoints.
A journal-only lower-bound receipt records observed attempt count and charged
slots without inventing a checkpoint version. Existing run CAS consumes it once.
Tool attempts, charged slots and completed effects remain separate facts; unknown
or older witnesses without counters cannot manufacture a slot count.
Alternatives: count terminal replies; add overlapping checkpoint and journal
counts; infer unrecorded slots from historical ordinals. These omit interrupted
or nested work, double-charge, or guess missing facts. No verdict, completeness
or replay permission follows. Reads retain one bounded page and scalar counters;
there is no new worker or total-history allocation.
Verification: the native development-engine proof recovered two charged slots
from witnesses while the older parent checkpoint recorded zero. It retained
incomplete consumption and granted no replay/verdict. No model calls were made.

### Terminal actors cannot issue new execution

Problem: a completed root can remain in its account for late child/request cost
settlement; membership alone therefore did not prove that it could still execute.
Decision: model reservation/send, tool reservation/intent and new-child attachment
reject the shared terminal statuses from the existing terminal-delivery contract.
A live detached carrier cannot resurrect authority through a terminal control
owner. No cancellation flag or account removal is required for this refusal.
Already dispatched model requests retain their settlement path and reported
usage; an unsent reservation can still void after its owner becomes terminal.
Alternatives: remove the root immediately (loses late settlement); inspect only
`aborted` (misses successful completion/error/interruption); let stale carriers
continue (creates effects after terminal delivery). No new state, worker or
storage write is introduced. This lifecycle fence is not a replay permission.

### Detached carriers share their control owner's local tool slice

Problem: independent proxy-local counters could multiply a child's explicit tool
slice while remaining below the larger tree quota. Decision: the shared account
tracks charged slots per active control identity under the reservation lock. Every
carrier of that identity intersects the same owner's current local cap and the
tree cap. Restored charges initialize the owner counter once; removing a carrier's
local configuration cannot erase the owner slice. Allowance snapshots include
charges made through proxies, and detachment removes the active owner counter
while global spent slots remain retained.
Alternatives: charge only each proxy dictionary; copy the counter when cloning;
charge outside the account lock. These respectively multiply slices, retain stale
copies, or race overlapping waves. Counters retain only active IDs/integers and
scale with existing account membership; no worker or persistence operation is added.
Measurement: a root plus one active child used 240 bytes for the counter dictionary
and integer shallow sizes on Python 3.12 (excluding already-shared identity strings).
Closing the child returned the map to one root entry. This is representation size,
not whole-process RSS, and counter lifetime follows existing active membership.

### Model carriers intersect the active owner's slice

Problem: a detached same-identity carrier could expand its local token cap or
retain an older deadline/usage view while borrowing a live owner's account.
Decision: reservation, physical-send recheck and allowance projection intersect
the carrier and active owner's current limits with the tree limit. Local usage
uses the maximum of the owner, carrier and account-reported tokens; overlapping
observations are never added. A stricter carrier remains stricter, while wider
or stale carrier configuration never weakens its owner's authorization.
Alternatives: trust only the copied configuration or usage dictionary; rely only
on the larger tree cap. These lose the owner's narrower slice. Late reported
settlement remains unaffected. No new state, store operation or worker is added.

### Carrier copies cannot clear unknown owner consumption

Problem: a globally unbounded account with a locally finite owner could admit a
fresh same-identity carrier whose dictionary omitted the owner's incomplete-usage
flag. Decision: local finite model authorization checks both carrier and active
owner completeness at reservation and physical send. A new carrier never restores
finite headroom after missing usage; unsent refusal still releases its reservation.
An unrelated child's missing usage in an unbounded tree does not erase an
independent known child's local slice. Alternatives: trust the copied flag or
refuse every bounded child whenever any unbounded tree member is unknown. These
respectively permit unknown spending or unnecessarily halt independent work.
No state, worker or persistence write is added; incomplete aggregate accounting
remains explicit and never trains a complete cost prior.

### D4 — Evolution stages re-admit as bounded new attempts *(target, phase 3)*

Evolution runs already decompose into stages, each stage an ordinary task
with a durable binding and CAS stage receipts. The existing
`sweep_stale_runs` reconciliation gains one bounded behavior: when a
non-terminal run's current stage has no settled receipt and its task is gone
(process death, confirmed by the same liveness evidence the sweep uses
today), the sweep re-admits that stage as a **new attempt task** under the
same episode:

- the new task identity and a `stage_retry` receipt (cause `worker_lost`)
  are persisted before dispatch, so a second crash during re-admission is
  itself recoverable;
- re-execution is attempt semantics: the new attempt observes the workspace
  as-is, the episode accumulates consumption across attempts, and verdict
  gates (checkers, holdout, human) are unchanged — done is still not success;
- a per-goal `stage_retry_budget` (small default, owner-overridable) and a
  per-stage cooldown bound the loop; exhaustion lands in `needs_attention`
  exactly as today, never a fabricated verdict.

This deliberately does **not** migrate evolution execution onto the durable
Flow engine: stage receipts already supply the idempotent seam, and a
mid-stage crash inside a Flow would hit the same D5 ambiguity anyway. The
rejected migration alternative is recorded below.

### D5 — In-flight attempt outcome resolution *(target, phase 4)*

The journal's refusal of unmatched agent admissions stays correct for truly
unknown effects, but today it cannot distinguish three cases that durable
evidence does distinguish. The journal admission gains a bound attempt
identity from the shared invocation pipeline, and recovery resolves the
pending admission against durable evidence before refusing:

1. **Attempt committed.** The invocation's durable terminal record exists →
   the pending admission converts into a completed receipt carrying that
   recorded result. No re-execution, no new effect.
2. **Attempt provably never dispatched.** Crash landed between journal
   admission and spawn, evidenced by a durable unchanged birth plus an explicit
   unsubmitted binding (never merely an absent row) →
   recovery journals an explicit `execution_effect_voided` receipt and
   re-executes. The retry is an audited decision, never a silent replay.
3. **Attempt genuinely unknown.** An attempt row exists without a terminal
   record (provider/tool call in flight at crash) → refusal stands; the run
   surfaces a typed `recovery_ambiguous` state with explicit owner choices
   (retry-with-void, or abandon as `worker_lost`). Tool side-effect
   ambiguity resolves through the same rule keyed on tool-settlement
   receipts. Automatic recovery never guesses.

`read_journal` gains a resolution seam; the replay interpreter and the
receipt taxonomy are unchanged. New event kinds are additive — old journals
remain valid, so `JOURNAL_VERSION` does not change for this phase; an
incompatible interpreter change would require a version bump per the existing
rule.

### D6 — One budget-continuity rule *(running contract, phase 1)*

The shared invocation account now reserves the final locally admitted input
estimate and output ceiling before each provider attempt. The transport's required
settlement port records reported usage once, including retries and bridge handoffs.
Only a proven unsent request voids its lease; each transport marks dispatch at
the physical send boundary. Preflight failures refund without inventing usage.
Live siblings wait for reservations to settle under the existing deadline and cancellation controls.
Unobserved consumption blocks finite-token authorization and marks stage, episode
and program usage as a lower bound; unbounded evolution can continue but excludes
that cost from measured learning. Provider counts can exceed local estimates, so
this remains an admission bound, not an exact invoice or crash-persistent escrow.
The counters/active leases use existing task/model concurrency and add no worker.
An owned benchmark measured 12.91 µs median reserve/dispatch/settle over 1,000 calls,
18.4 µs p99 and 640 retained bytes per live lease; settlement leaves no lease row.
The same limits apply to model fallback and native sync/async/desktop transports.
Problem: round-end checks admitted an already-unaffordable next prompt. Decision:
reserve at sealed request ingress. Alternatives: a root-only check misses children
and retries; treating missing usage as zero refunds unknown provider work.

A recovered run never re-spends completed work: the replayed receipt is the
durable consumption record. The run-tree agent-start budget counts only
genuine dispatches — a replayed receipt reaches the claim site before the
journal returns its record, so the engine wires
`OrchestrationAgentBudget.release` as the journal's replay hook and the
phantom reservation is refunded (`engine.py` →
`ExecutionJournal.runner(on_replay=...)`). Reservation stays live-only:
crash-persistent budget reservation remains rejected, and in-flight
over-admission at crash is absorbed by the recorded-usage lower bound, not
pretended away. Evolution episodes keep accumulating actuals across stage
attempts.

### D7 — Long-horizon stability posture (running contract, retained)

These mechanisms already ship and are the stability floor this program builds
on; they are restated here so the continuity target names one place:

- Goal budget is an evidence-extended checkpoint window, never a stop reason;
  a zero-progress window ends as `stalled` → `blocked/no_verified_progress`.
- Durable human gates park worker-free across restarts with wall-clock
  approval deadlines; expiry is settled lazily through the same CAS as
  answers.
- Terminal result delivery requires acknowledgement; failed handoffs retry
  behind the maintenance fence; reconciliation sweeps re-drive lost
  transitions idempotently.
- ProgressBeacon liveness and the stuck-task reaper bound wedged execution.
- Event logs stay bounded: recovery replays pages, validates journal segments
  by hash/count, and never copies result bodies across generations.
- The launch probe sizes every pool/queue introduced above; probe failure
  falls back lean, and explicit operator overrides remain.

## Alternatives considered

- **Blindly auto-replay ambiguous admissions.** Rejected: replaying an
  interrupted provider/tool call cannot prove whether an external effect
  committed; duplicates are worse than a visible refusal. D5 resolves the
  provable cases instead.
- **Serialize interpreter control state (checkpoint the program counter).**
  Rejected when receipt journaling shipped and rejected again here: a second,
  brittle control-state schema that drifts from the interpreter. Receipt
  replay through the existing graph interpreter remains the only recovery
  authority.
- **Migrate evolution execution onto the durable Flow engine.** Rejected for
  this program: stage tasks with CAS receipts already form an idempotent
  seam, and a mid-stage Flow crash reduces to the same D5 ambiguity.
  Re-evaluate only if stage granularity proves too coarse in operation.
- **One durable interpreter for every workflow (chat, Swarm, evolution,
  goals).** Rejected by the execution-unification decisions in
  TASK_EXECUTION_MODES.md; this program reuses the shared journal and
  invocation boundaries instead of replacing domain lifecycles.
- **Process-supervisor restart as the recovery story.** Orthogonal: a
  supervisor restarts a process, not runs, and conflicts with zero-config
  desktop deployment. Continuity must be a property of the runtime.
- **Exactly-once external I/O via provider idempotency keys.** Not generally
  supported across providers and tools; D5's attempt-outcome resolution
  covers the realistic crash window without it.

## Phasing and verification

| Phase | Scope | Focused verification |
|---|---|---|
| 1 *(landed)* | D1 launch-stamped policy, D2 boot pass, D6 replay refund, continuity events | `tests/orchestration/test_orchestration_continuity.py` (election, owner-bound successor, refusal audit, idempotency, crash-loop guard, capacity, kill switch, replay refund); `test_continuity_durable_election`, `test_orchestration_startup_recovery`, `test_server_startup_recovery`, `test_orchestration_continuation`, `test_orchestration_run_service`, `test_storage_application_contract` |
| 2 | D3 GoalRun continuation | Goal run survives restart with working set restored from the lane checkpoint; terminal truth preserved on spawn failure/refusal; `test_goal_worker_context`, goal invocation integration |
| 3 | D4 evolution stage re-admission | Sweep re-admits a lost stage as a new attempt with prior receipts intact; retry budget exhaustion → `needs_attention`; no verdict fabrication; `test_evolution_automation`, `test_evolution_runs`, end-to-end |
| 4 | D5 attempt resolution | Committed-attempt conversion, void-receipt retry, ambiguous operator flow, journal additive compatibility; journal/recovery suites |

Each landing phase updates its domain map and rewrites its section above from
*target* into *running contract* in the same change, and extends
[RELIABILITY_RUNBOOK.md](runbooks/RELIABILITY_RUNBOOK.md) with the corresponding
continuity incident playbook.


## Native completed-round checkpoint authority

Agent.checkpoints borrows an explicit trusted load/begin/complete port. The host
holds an exclusive eligible completed frontier and restores financial/time
budgets before load returns. begin must CAS to inflight before effects; complete
commits joined model/tool/progress state before a Suspend can stop the loop.
Inflight or terminal frontiers cannot load an older snapshot. Failure stops,
never reexecutes effects. The native_agent_checkpoint_v1 schema binds identity,
opaque model/tool/account-policy binding, cumulative counts, history/usage and
native progress digests. Round/tool limits must match; memory is readmitted and
past usage never becomes a second settlement. No monotonic deadline is persisted.
Native stdio/application checkpoint storage and default-root recovery remain
unbound; the core port grants no automatic storage, recovery or financial credit.
## Shared loop completed-round state

Problem: restoring messages/usage alone reset round identity, premature-close
retry credit, timeout and progress/failure streaks, allowing restart to evade
semantic breakers. Decision: run_agent_loop exposes optional on_checkpoint and
resume_checkpoint ports. State is emitted only after all tools, breaker checks,
after_tools and on_round_end settle successfully. Sink refusal propagates before
another dispatch. Terminal, aborted or halted rounds emit no resumable state.

The versioned JSON shape contains next_round, retry_bonus_used,
consecutive_tool_timeouts, claimed_tool_call_ids, progress and unchanged limits.
ProgressLedgerV2.snapshot/from_snapshot retains full call/world/evidence and
terminal-failure signatures, streaks and bounded evidence IDs. Restoration
validates version, complete fields, counter types, correlation-ID uniqueness,
signature shape and breaker/retry limits before provider dispatch. Changing
limits or restoring a state that already exhausted a breaker is refused.
Serial tool ID claims survive; batch adopters still own their ID normalization.

Replaying completed tools or restoring only the next round was rejected because
it duplicates effects or resets semantic safety. These ports do not restore
caller messages, budget meters, world state, batch-owned IDs, abort state or
execution ownership. Workflow repositories must bind those materials to a task,
acknowledge them atomically with the chassis state, and fence stale attempts.
Research automatic continuation is not enabled by this chassis interface alone.
`validate_agent_loop_checkpoint` owns complete shape, integer counters, admitted
limits, tool identities and progress-ledger validation for both persistence and
execution. Valid halted state can be inspected; execution additionally checks
the caller's exact limits and refuses exhausted breakers. Problem: partial
durable validation admitted hash-correct state that the execution chassis later
refused. Decision: share the validator; rejected: separate persistence checks or
resetting missing counters/ledger state. Validation never invokes tools/models
or grants execution ownership.
Tests interrupt at the checkpoint port and resume the real shared loop to verify
no-progress/terminal-failure/timeout streaks, retry credit and serial ID history.

Measurement: 1,000 synthetic serial tool rounds with changing result evidence
produced a 78,545-byte final checkpoint (1,000 ID claims and evidence IDs) in
269.1 ms on this host, with no provider calls. Evidence history remains bounded
at 2,048 IDs; correlation claims retain existing per-loop growth. Checkpoint
serialization therefore scales with ID claims plus bounded evidence. Durable
workflow adapters must choose checkpoint cadence and page long histories rather
than claim constant-space persistence from this interface.

The shared budget's `restore_model_account` port restores cumulative model
consumption and the last physical attempt ordinal into a pristine model-only
account exactly once, before admission. Problem: a reconstructed account lost
consumed authorization and restarted attempt numbering. Decision: carry both
values forward atomically; incomplete usage remains incomplete and prevents
finite model admission. Alternative rejected: initialize recovery at zero or
reuse old attempt keys. The caller must prove old-writer quiescence, validate
receipts and bind the journal observer before restoration. The port grants no
ownership, settlement or replay and is not yet activated by discovery recovery.
Verify `tests/agent_core/test_model_account_restore.py` and model budget suites.
