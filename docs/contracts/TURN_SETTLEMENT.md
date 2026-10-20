# Turn settlement contract

Settlement is a durable fact produced once by the turn authority. It answers
why an attempt ended and which recovery operations are truthful. The browser
projects this document; it does not recompute it from task events.

## Durable state

An output turn is live while its status is `pending` or `running`. Terminal
statuses are:

| Status | Meaning |
|---|---|
| `completed` | provider/agent finished normally |
| `interrupted` | cooperative user/supersession stop or service recovery |
| `truncated` | provider limit or content filter cut output short |
| `failed` | generation, provider stream, or executor startup failed |

An `interrupted/user_abort` verdict is reserved for stops the user (or a
superseding task) actually requested. Internal storage/attempt fences —
`_abort_reason` in `INTERNAL_FENCE_ABORT_REASONS` (`storage_authority_integrity`,
`storage_frame_overflow`, `turn_attempt_stale_fence`,
`conversation_attempt_stale_fence`, planted by `lib/turn_lifecycle` /
`lib/tasks_pkg/manager/_events.py` when the authority deterministically
rejects this turn's writes, e.g. `database_integrity`) — settle as
`failed/internal_fence_abort` with `evidence: external_authority` and a
`storage_fence` error envelope (warning, retryable). Mislabeling these as
`user_abort` once rendered turns as unexplained stops with no recovery path;
the fence aborts the in-flight request, so the request-complete activity row
also reports `failed` + `errorKind: storage_fence` instead of the user-abort
row. The engine (`tofu-db`) carries the exact fence diagnostic in the
response `error_message` while `error_code` stays `database_integrity`, so
the `[TurnLifecycle] conversation authority integrity failure` and
`[ProviderIngress] async delivery failed` log lines name the specific fence
that fired.

Service shutdown stamps `server_shutdown` through the shared task quiesce owner
before closing native clients; their terminal callbacks can settle immediately.
The existing verdict then records `interrupted/server_restart` with
`evidence: system_recovery`. A prior user Stop retains its original cause.
Problem: joining native clients first lost the shutdown source and fabricated
`user_abort`. Decision: declare the cause before physical cancellation, while
still joining native work before its dependencies close. Alternative rejected:
rewriting the verdict after restart would replace an already committed fact.
The full production shutdown test keeps a real native provider request and SSE
consumer active, verifies joined exit and the reopened verdict, and rejects a
new dispatch while the original usage is unknown, even if request config claims
it is complete (`tests/server/test_native_production_shutdown.py`).

Invocation guards stamp `_invocationStopReason` (`stalled` / `deadline`) and an
internal error envelope before signaling physical abort. They settle as
`failed/invocation_stalled` or `failed/invocation_deadline`, with existing
`generation_error` evidence. `client_aborted`, provider finish, partial output
and usage/tool receipts stay truthful; diagnostics expose numeric silence and
threshold only. Explicit Stop is checked first and remains `interrupted/user_abort`
(Swarm `cancelled`), with no cancelled-only automatic wake or new UI taxonomy.

The existing error envelope carries the bounded `invocationStopReason` extension
(`stalled` / `deadline`), minted only by `context: invocation-guard`,
`source: task-engine`. Canonical error serialization preserves it for cold task
consumers even when private carrier fields are gone. Finish settlement records
`error` without erasing physical cancellation. Fatal transport exceptions retain
the guard diagnostic; the shared verdict reads the guarded extension rather than
inferring failure from arbitrary `aborted + error` combinations. Driver stall is
not forwarded through the owner Stop port: the shared child beacon owns its cause.

The terminal turn stores:

```json
{
  "outcome": "completed|interrupted|truncated|failed",
  "cause": "provider_finished|user_abort|generation_error|...",
  "evidence": "provider_finish|provider_stream_failure|...",
  "streamState": "provider_finished|premature_close|malformed_stream|...",
  "providerFinishReason": "stop",
  "error": null,
  "resumeOptions": []
}
```

`error`, when present, is a normalized error envelope. A terminal error event
without an error payload is itself a contract failure and is converted to an
actionable internal envelope at the authority boundary.

`streamState` is the closed parser verdict. `providerFinishReason` remains a
provider/compatibility detail and cannot prove success by itself on the typed
stream path. `evidence` records which positive or negative fact authorized the
turn verdict. A bare `done` event, non-empty content, or a parser default of
`stop` is not completion evidence and settles fail-closed. A named legacy seam
still accepts an explicitly supplied finish reason for non-stream producers;
new model-backed paths must carry `streamState`.

A provider stream that ends without its finish marker is never
`completed/provider_finished`. When partial prose exists, the live executor
first preserves that prose, emits a `phase: retrying` status, and performs a
bounded continuation from the preserved prefix. If those lossless
continuations are exhausted, the turn settles as
`failed/provider_stream_error` with `providerFinishReason: premature_close`;
the partial projection remains durable (and prefill-capable models expose it to
`continue`) and is never cleared by the whole-turn regeneration retry path.

For a tool-bearing stream, parseable argument JSON proves only a complete
prefix. Problem: the legacy recovery classifier admitted that prefix while
the shared execution loop rejected its unverified stream, bypassing recovery.
Decision: the typed stream verdict controls bounded tool-round retry, including
when a legacy salvage hint is present. A verified provider finish without a
`[DONE]` marker remains valid. The accumulator retires unverified announcements
and speculative results before adoption; each discarded announcement receives
an aborted completion receipt with no model-facing tool output. A retry closes
its physical round before the next round opens; exhausted recovery also closes
the rejected round. Alternatives rejected: treating
parseable arguments as completion permits a partial tool batch; relaxing the
common execution gate or dropping event invariants hides the defect. The real
`incomplete_tool_stream` replay proves no unverified write, one verified write,
paid-attempt accounting and paired durable round/tool events.

Cooperative Stop before runner entry, including workspace admission, uses the
existing aborted-task finalizer. Both the task flag and cancellation event are
checked. Problem: workspace admission reported Stop as an executor error and
could ignore an already-signalled event. Decision: preserve typed cancellation
through the shared worker wrapper, keeping prepared workspace preservation and
terminal persistence. Internal authority fences and binding failures retain
their failure lane; converting every admission exception to cancellation would
hide those failures.

For single-agent Responses, an assistant output explicitly marked `commentary`
is intermediate work even when the response closes with `stop`. With no tool
calls, a valid unchanged phase replay triggers at most two continuations per
task, independent of the failed-tool nudge and stream-retry budgets. The
existing `intent_stall_nudge` phase and durable intervention note show the
reason and limit. Exhaustion ends with `abnormal_stop`, never success.
Cancellation, malformed streams, provider errors, token limits, pending human
guidance and explicit end-turn declarations retain precedence. Unphased or
`final_answer` output is never reclassified by matching prose.

Repeated successful verification is not task-completion evidence. The root
loop's repeated-verification stop settles with `providerFinishReason: incomplete`
and the existing incomplete recovery semantics, while its display-only
`system_note` explains why execution stopped. It must not reuse the normal
completion marker for genuine background-task acceptance. Checklist snapshots
are advisory progress records: pending items neither force another model round
nor override terminal evidence, and a completed list is not verification.
Historical checklist-continuation events remain readable, but new execution
has no checklist-only continuation or completion gate.

A short, tool-call-free `stop` body that is a gateway throttle notice rather
than a model answer (some gateways return their hourly-limit text as a normal
HTTP 200 chat completion instead of HTTP 429) closes with
`streamState: gateway_limit_response`. It is unusable: the slot records a soft
failure and cools down, and the round retries so the cooled slot rotates onto
a healthy key rather than surfacing the notice as the answer. The retry shares
the per-phase counter with the other non-empty buckets (cap
`_GATEWAY_LIMIT_RETRY_MAX`); because a throttle notice is never a legitimate
answer, exhaustion settles `failed` with a `premature_close` error envelope
instead of delivering it (conv mtxxaf2l).

A clean blank `stop` response, or a tool-finish response without tool payload,
uses at most two abnormal-terminal recoveries per task across all round/phase
resets. Explicit compatibility-layer evidence of unresolved tool control text
shares that budget. Recovery preserves prior operation receipts and disables
whole-turn regeneration; exhaustion carries a typed `abnormal_stop` error,
not normal completion. Real tool batches and protocol-defined program
continuations do not consume this budget. This executor verdict remains
separate from a benchmark verifier's pass/fail result.

A short final answer after tool work remains byte-identical at settlement,
including JSON, numbers, booleans and concise prose. **Problem:** the former
length-only recovery guard overwrote valid compact answers with inter-round
narration. **Decision:** narration recovery requires a blank terminal body or
an incongruent canned greeting confirmed by the shared stream classifier,
substantial prior narration, a clean `stop`, and no user abort. The classifier
uses the actual conversation tail; missing context cannot establish greeting
incongruence. **Alternative rejected:** special-casing JSON would still corrupt
valid short text answers, while disabling recovery would reopen the upstream
greeting incident. Shortness remains diagnostic evidence, not overwrite
permission. The public task result and durable turn share this delivered body.

## Event-delivery cancellation

**Problem:** shutdown cancels event admission while a producer is still draining;
wrapping that cancellation as a generic delivery failure misreports capacity
pressure. **Decision:** the event admission fence preserves cancellation and
revocation types across later emissions. Closing delivery is cancellation, not
capacity rejection. The fatal sink uses the shared verdict matrix for typed
cancellation: interrupted work publishes an aborted terminal with no failure
envelope, preserving `server_shutdown` as `server_restart`. Invocation guards
and internal authority fences retain their failed verdicts and diagnostics.
Accepted event debt still drains before terminal persistence; cancellation does
not replay model work or discard accepted evidence. **Alternatives rejected:**
relabeling `server_busy` in the browser hides genuine durability failures;
treating every physically aborted task as cancellation hides guard/fence faults.
Specs: `tests/tasks_pkg/test_internal_delivery_error_attribution.py`.

## Delivery dependencies

A completed provider Turn is not a user-objective completion receipt. Existing
`waitingOn` metadata exposes unresolved required Swarm work on clean `stop`,
`end_turn` or `incomplete` boundaries: required children may be running or have
unread terminal evidence awaiting parent integration. Optional children do not
block this dependency observation. Provider stream status and finish reason are
preserved. The Swarm owner derives dependencies from durable specs, current
terminal receipts and unread inbox identities; advisory checklists/prose cannot
create or settle them. Existing continuation resumes after required receipts
settle, subject to cancellation, identity and budget fences. See
[delivery dependencies](../RETAINED_AGENTS.md#delivery-dependencies).

## Single producer

`lib/turn_lifecycle.py::_settlement` derives the document from the terminal
executor evidence through the pure `lib/turn_verdict/__init__.py` state matrix.
`turn.event.record` applies a revision-checked projection patch and commits the
resulting projection, attempt event, turn status, settlement, conversation
revision, and any carried task event in one Sidecar transaction.

Every terminal exporter uses the same matrix. In-process results and the
OpenAI/Anthropic compatibility surfaces call `derive_task_verdict`; a failed
verdict travels on the vendor error channel and is never normalized into
`stop` or `end_turn`.

Consequences:

- a client-visible terminal frame never precedes durable settlement;
- duplicate terminal frames are rejected by attempt state/CAS;
- a stale or superseded executor cannot overwrite the current attempt;
- reconnect and cold reload read the same settlement;
- queue draining happens only after the terminal transaction frees the lane.

## Visible Flow phase settlement

Problem: native `turn.visible.sync` previously returned an existing live child
without applying its terminal evidence, leaving completed work running until
restart recovery incorrectly interrupted it. Imported terminal phases were also
unconditionally completed. Decision: native and SQL authorities use the same
verdict precedence; missing finish evidence fails closed, provider limits remain
truncated, errors remain failed and user cancellation remains interrupted.
Native settlement uses the regular atomic event-record authority, retaining
projection block identities, owner/root/current-attempt/parent fences and index
updates. Exact terminal replay is read-only; changed output/outcome or reopening
a terminal phase fails. A conflicting batch rolls back its earlier mutations.
Alternatives rejected: completing all phases, fixing projections only at restart,
or adding a separate caller-supplied verdict beside message evidence.
Specs: `packages/tofu-db/src/turn_visible_settlement_tests.rs` and
`tests/storage_authority_sql/test_turns_delta_sync.py`.

Visible tool segments use the public constructor's metadata rules on both
authorities: retain exact nonnegative JavaScript-safe `roundNum`, preserve
`toolModelContent` as result `modelContent`, and omit a missing/non-string/empty
verdict instead of emitting `status: null`. The problem was a native constructor
that lost chronology/model evidence and produced malformed early tool results.
The decision is constructor parity at the durable projection boundary; ignoring
metadata during differential comparison or repairing it only in the browser
would leave replayed data inconsistent. Public sync and receipt replay verify
these fields; the differential suite also checks the resulting projection.

## Explicit user cancellation

A Stop acknowledgement means the owned attempt accepted a cancellation signal,
not that its workers have exited or its Turn has settled. All chat Stop entry
points delegate to the same owner-checked cancellation authority; a terminal or
terminalizing attempt cannot be rewritten by a racing Stop.


Post-Stop event admission raises typed `EventDeliveryCancelled`, never a new
persistence failure. Unaccepted late frames stay invisible without poisoning
admission or planting a delivery-wedge warning; accepted FIFO evidence still
drains before terminal settlement. Typed cooperative cancellation at a model or
persistence boundary uses the existing terminal owner with `aborted` and no
fatal error payload. Internal fences, invocation guards and genuine delivery
failures retain their failed verdicts. **Problem:** a cancelled child-panel write
poisoned parent admission, replacing cancellation with generic `server_busy`.
**Decision:** preserve cancellation identity at admission and settlement.
**Alternative rejected:** suppressing all failures on an aborted task hides
real storage faults. Specs: task-event-delivery and internal-delivery-attribution
regressions.

Explicit cancellation reaches the active child-agent session and tool resources
without waiting for the parent model/tool loop to finalize. Ordinary parent
completion and observer disconnect remain distinct: they do not cancel an
accepted detached swarm. Conversation-scoped reuse must not bind a new active
turn to a stale spawning-turn cancellation predicate.

`await_agents` treats cancellation as an independent exit condition, not as a
completion timeout and not as a requirement to wait for scheduler drain. Tool
batch collection must observe cancellation even when no future completes;
a blocking iterator or unconditional pool join cannot be its signal-delivery
mechanism. Queued work is prevented from starting; running work receives
cooperative cancellation. Resource callbacks send bounded signals only, outside
the task-authority lock; they must not join workers or perform network cleanup.
Already-running Python threads cannot be forcibly terminated: outstanding work
must retain bounded resource ownership and late-result fences rather than be
reported as physically stopped. Each visible tool occurrence settles once with
its actual completed receipt or an explicit interruption, never an invented
`Unknown tool` failure. Child cancellation is not a retryable provider failure
and must preserve accumulated usage and committed child evidence.

A normal parent settlement detaches its swarm even if the last child finished
between the final inbox drain and the parent's `done` commit. Natural completion
must neither call child cancellation nor delete unread result/inbox evidence.
After a successful terminal commit, `lib/swarm/turn_handoff.py` rechecks the same
session settle hook; the continuation policy, rather than the finalizer, owns
batch eligibility, Stop, deduplication and retry limits. A failed terminal commit
cannot authorize a successor. Explicit Stop signals the workers and clears only
the swarm notification lane, preserving unrelated peer/background-command
receipts and child results for inspection. Startup recovery checks owner-scoped
Turn authority and fails closed on a committed Stop or unavailable cancellation
authority. A terminated driver may replay terminal results, but never respawns
missing/nonterminal checkpoints from a user cancellation on its own. An exact
accepted Continue may reopen safe ordinary children carrying parent-Stop
provenance after the old workers drain; completed results, individual kills,
retained assignment CAS and unresolved tool effects are not replayed. See
[run continuity](../RUN_CONTINUITY.md).

Server shutdown is distinct: `abort_reason=server_shutdown` derives
`status=interrupted`, `outcome=interrupted`, `reason=server_restart`, with
`system_recovery` termination evidence. Shutdown cannot overwrite an earlier
user Stop. The session is durably restart-paused rather than terminated, and
cooperative child cancellation does not publish a terminal cancellation or end
a retained assignment. Startup alone does not authorize execution.

Accepted resume config carries authority-written `_resumeSource` (exact prior
attempt/task, status and settlement cause). SQL/native acceptance overwrite any
caller-supplied value in the same CAS transaction; execution reads this receipt,
not local operation flags or the successor's now-cleared Turn settlement.
An accepted Continue can resume only the same owner's saved roster bound to the
source Turn and active Attempt and its exact, currently running Continue
successor. Recheck durable Stop/supersession after the binding write and registry
publication, then at actual child admission before and after start observers;
local notification delay grants no execution. Admission checks run outside
registry/scheduler locks and are required steps, not best-effort observers.
Natural parent completion still permits its admitted detached children.
Use current principal and executable tools intersected with the saved scope. Completed children and acknowledged deliveries remain settled; pending
children and safe unfinished checkpoints reconstruct new execution instances.
Paid token/cost receipts, elapsed work and tool counters carry forward once.
Write-ahead model/tool intent and settled receipts define recovery. Unknown tool
outcomes fail visibly, never replay blindly; their transitive unfinished dependents
fail with `restart_dependency_reconciliation_required` before scheduler admission.
Finished evidence and unrelated children remain intact. Dependency input includes
canonical status/error, never partial prose presented as ordinary success.
In-flight model usage is incomplete;
finite budgets refuse unknown consumption, while safe ready receipts retain
completeness across shutdown. A prior request's retained usage cannot certify a
later failed/usage-free request; completeness uses that request's own receipt. Request-local costs and active time accumulate once;
restart never renews a child timeout. Explicit deliveries checkpoint their final
answer before teardown. Unactivated recovery retains an exact failed-activation
receipt; only its direct failed successor may retry, never Stop/regeneration.
Registry admission reserves bounded capacity before durable rebinding; active
and reserved membership of one key consumes one slot. Registration/timer-start
failure restores exactly its prior session, timestamp and alias state, retiring
the failed timer generation. Failed activation drains submitted workers before
unpublication, retaining exact retry provenance. Stop during publication removes aliases and cannot leave a dead driver.
Current subtree access intersects the original policy, including readonly subroots.
Legacy cancellation rows without restart provenance remain non-resumable; do
not retrospectively turn user Stop into Continue authorization.

Continue rebinds the current principal, ModelRef and narrowed tool catalog, not
expired provider slots. Isolated children retain their original checkout and
acknowledged root labels; current removed roots refuse recovery, and readonly
downgrades veto writes. New parent checkout/delivery ownership is not inherited.
Specs: `tests/swarm/test_swarm_turn_handoff.py`,
`tests/swarm/test_swarm_rehydrate_cancellation.py`,
`tests/swarm/test_swarm_restart_continue.py`, `tests/swarm/test_swarm_invocation.py`,
`tests/swarm/test_swarm_restart_authority_storage.py` (real SQL/native acceptance),
`tests/swarm/test_swarm_restart_dependencies.py`,
`tests/swarm/test_swarm_recovery_activation_fence.py`,
`tests/swarm/test_swarm_recovery_registry.py`.

A background command completion first commits one durable passive receipt row;
the inbox twin only accelerates delivery to a still-running turn. One receipt
does not itself authorize another model turn. When all known commands for the
owner and conversation have finished and published, one collective workflow
wake starts an idle conversation. Existing queued work takes precedence and
consumes the receipts instead. The next turn rehydrates up to 32 receipts into
one inbox batch, including receipts left by a server restart; provider-
consumption acknowledgement removes their rows.
Problem: the live tool config can acquire process-private cache views that the storage JSON
codec cannot encode, losing the completion before queue commit. Decision: the
queued config retains public turn settings and strips every leading-underscore
runtime key. Alternative rejected: encoding private runtime objects into the
queue would persist stale state and cross the turn authority boundary.
Problem (idle continuation): dispatching every finished command as a workflow
turn made late test completions produce vacuous assistant replies after the
parent task was done. Decision: wait for every known command to publish, then
enqueue one idempotent collective wake; startup and maintenance reconstruct
missed wakes from durable receipts. A failed publication remains in the
command registry and retries on the existing maintenance tick before its batch
may wake. Alternative rejected: a fixed debounce alone, which can fire while
a slower sibling is still running.

Regression tests must separately observe signal acceptance, wait release, child
exit, and authoritative terminal settlement. A fast HTTP response or a released
composer button alone is not a cancellation-latency test.

## Swarm failure visibility and continuation

Each child failure is surfaced immediately, independently of main-agent wakeup:
the panel retains its reason and warns that failed work may have partial edits.
Success, failure, cancellation, running and waiting counts are distinct. Transient
notifications are deduplicated per conversation/run/agent with a 256-key bound;
hydrated terminal snapshots do not replay old alerts.

Routine automatic integration waits for the whole swarm to settle. Unread stamped
successes authorize integration; a wave containing failures (including all-failed)
may authorize one diagnostic/recovery/closure turn per human-authorized chain,
not blind retries. Durable continuation receipts enforce that failure allowance
and the total unattended-chain ceiling across restart. Cancelled/unknown evidence
alone, Stop/interruption, unavailable authority, or exhausted budgets do not
authorize continuation; results remain inspectable. Failed dispatch does not spend
the recovery allowance. Specs: `tests/swarm/test_swarm_failure_continuation.py`,
`frontend/unit/swarm-failure.test.cjs`.

## Resume options

Recovery is explicit. Each entry has an `operation` and a durable `anchor`.

- `continue`: lossless assistant prefill is available for a model that
  supports it and a non-empty partial projection. The executor config pairs
  the prefill with the retained checkpoint tool rounds — the same pre-gap
  retention a `checkpoint_resume` anchor computes — so the resumed model
  keeps every tool fact it produced before the interruption instead of
  continuing blind to the history the user still sees. Replay inside the
  executor still uses the causal prefix.
- `checkpoint_resume`: completed tool rounds provide a stable checkpoint.
  The anchor retains every pre-gap round, display/progress carriers
  included, and amputates the result-less in-flight tail; only explicitly
  superseded provider-attempt artifacts are filtered.
- `regenerate`: restart from turn input; always available for a non-successful
  terminal turn.

All resume operations, including `answer_guidance`, supersede the later lane
tail and its rooted branches atomically, just like regeneration. Resume keeps
the target's checkpoint, progress and paid usage, advancing logical rounds
without a visible attempt split. Live descendants refuse the whole command
before mutation. `deletedTurnIds` and `turn.deleted` replay converge clients;
rejected commands leave the tail intact. External effects are not undone.

The command service validates the selected operation against the stored
options and expected projection revision. The frontend displays exactly these
operations through `frontend/src/core/turn-presentation.ts`; it does not label
regeneration as continuation, and the rendered action label reflects the
granted operation, so a regenerate-only settlement reads as regeneration.

## Attempt startup failure

Creating a turn and starting its executor are separate durability phases:

1. create the input/output pair and pending attempt;
2. atomically claim the pending attempt;
3. register the executor task and bind its id before spawn returns;
4. if registration/start fails, settle the attempt with
   `task_start_failed`.

There is no post-spawn compatibility bind. Accepting an unbound worker would
recreate an orphan window in which two executors can own one attempt.

## Orchestration turns

Planner, worker, critic, virtual-user, scheduler, and swarm continuation rows
are explicit turns with stable identities and actors. They do not append
synthetic entries to an archived message array. A parent attempt may announce
related turn ids so its live stream exposes the complete orchestration shape.

## Invariants to test

- terminal state is monotonic;
- settlement and terminal projection commit together;
- stale attempts cannot mutate current turns;
- error envelopes survive replay;
- resume options correspond to actually executable anchors;
- startup failure leaves no pending/running orphan;
- terminal queue drain dispatches at most one successor.

Primary suites: `tests/turn_lifecycle/test_turn_lifecycle.py`,
`tests/turn_lifecycle/test_turn_event_carried_task_event.py`,
`tests/translate/test_translation_turn_authority.py`, and
`tests/frontend/test_frontend_conversation_surface_vite.py`.

Usage aggregation uses `lib.cost.accumulate_usage_into`: public numeric counters
and fixed-depth reasoning/cache detail counters sum across authoring and discarded
billed rounds. Private dispatch/wire evidence and nested discarded-round carriers
are never summed. Each discarded attempt retains its own model/provider ledger
record and is added exactly once; booleans and nonfinite values are not counters.

Terminal observers register once through `manager._terminal` (at most 16) and
receive only successful owner-fenced durable writes. Running checkpoints,
transient tasks, failed writes and stale finalizers do not notify. The per-task
execution receipt suppresses duplicate notifications; observer errors never undo
committed state. Callbacks must only enqueue bounded follow-up work.

## Whole-turn automatic retry receipts

Problem: the settlement producer retained discarded retry evidence, but the
strict wire schema did not declare `retryChain`, so both live attempt events
and cold snapshots could reject otherwise valid retried turns.

Decision: `TurnSettlement.retryChain` is optional and contains at most eight
typed `TurnRetryReceipt` entries (`attempt`, `kind`, `backoff_s`, `at`, optional
bounded `detail` and `failureAttribution`). The producer in
`lib/tasks_pkg/failure_attribution.py` remains the authority; snapshot and
attempt-event decoders accept the same durable receipts. Strict settlement
and receipt field validation remains enabled.


Failure attribution projects the loop's zero-based internal dispatch index into
one-based `roundNum`, including the first dispatch as round 1. The internal task
index stays zero-based for context composition. Invalid/negative indices do not
invent a round, and task-start failures never blame replayed rounds or tools.
Normalizing the internal carrier was rejected because it shifts context-policy
consumers; only the public diagnostic boundary performs the conversion.

Alternatives rejected: dropping retry evidence hides why another attempt ran;
allowing undeclared settlement fields removes the protocol safety boundary.
`tests/turn_lifecycle/test_failure_attribution.py` covers produced receipts in
both transport envelopes, the bound, and rejection of undeclared fields.

## Retrying a failed terminal commit

Problem: a terminal Turn event can fail while the diagnostic `task_results`
write later succeeds. Retrying only diagnostics clears the task eviction fence
but leaves the browser's authoritative Turn running indefinitely.

Decision: the Turn facade retains one task-local terminal event and its exact
replay carrier when settlement raises. The existing bounded terminal retry lane
replays this receipt before writing diagnostics or releasing projection state.
An ambiguous timeout invalidates the local projection baseline; retry reads the
current attempt and respects its terminal/supersession fence. No model/tool work
is repeated and no terminal push precedes the authoritative commit. A process
crash continues to use durable orphan-attempt recovery.

Alternatives: synthesizing browser completion would invent a second authority;
retrying only `task_results` cannot repair a Turn. A new retry worker or queue is
unnecessary because the existing task registry already bounds and owns this debt.
Fault-injection tests cover rejection before commit, lost acknowledgement after
commit, retained partial output, and the diagnostic-write ordering.


## Registered workspace delivery is not Turn completion

Problem: terminal model/task success and candidate integration can precede
canonical publication. Decision: `lib/tasks_pkg/workspace_delivery.py` derives
publication eligibility from the canonical verdict, while
`lib/project_mod/delivery.py` owns the separate durable `origin.delivery` receipt.
Only terminal success plus a completed verdict authorizes publication. Missing
finish evidence, truncation and internal fences fail closed; failed/cancelled/
stopped outcomes preserve checkpoints without automatic submission or adoption.
A capture error leaves explicit retry debt, not an invented preservation receipt.

Before tools run, writable configured roots bind only existing owner/task
registrations; read-only roots, cwd visits and task-stamped file receipts cannot
claim a checkout. Evidence is bounded (200 file/cwd facts each), diagnostic only.
Terminal intent must be acknowledged before heavy evidence release/eviction;
failure retains the existing terminal-persistence fence and retry lane. Git
checkpointing, gates and delivery run in the lifecycle-owned Integration worker,
not another task queue. Isolated roots never fall back to shared-root publication.
Startup leaves an orphaned Project Work active when intent cannot be persisted.

Candidate `merged`, Project Work `completed` and Turn `completed` are distinct
from delivery `delivered`. Busy/dirty/changed canonical targets retain blocked
receipts and checkpoints; gates/conflicts cannot be repaired by declaring task
success. Restart scans bounded receipt pages and uses checkpoint ancestry to
recognize an adoption whose acknowledgement was lost, without replaying model
work. See [`project_integration.md`](../modules/project_integration.md) for leases,
adoption guards and cooperative-writer limits. A second settlement authority,
magic success token or destructive cleanup was rejected; none proves delivery.

## Known external blockers and unattended recovery

Problem: a retryable refusal or completed child result does not prove that the
main task's external condition or request protocol has changed. Replaying it
can spend repeated model rounds without executing the blocked operation.

Decision: `lib.turn_lifecycle.automatic_recovery_blocker` reads typed failure
metadata. `git_guard` refusals carry `condition` and
`retryPolicy: {mode: after_condition_change, automatic: false}`; their Turn
remains **failed**, with the rejection preserved in the error envelope.
`retryable: true` permits retry after the condition changes, not immediate
unattended replay. `autoRetryExhausted` also fences the existing whole-turn
replay path. Request-prepare `protocol_conversion:*`, explicitly non-retryable
failures, and exhausted automatic budgets likewise cannot be revived by a
successful child. Swarm updates stay in the inbox for the next authorized turn;
Stop, successful result integration, and the single ordinary failure-recovery
allowance retain their existing authority.

Intent-stall and commentary continuation read the last execution batch's
structured Git guard evidence without requiring an `END_TURN` token. A
successful sibling is not evidence that a refused command ran; a subsequent
execution batch may supersede the old blocker. Alternatives rejected: treating
`retryable` as permission to replay immediately, asking the model to invent a
magic stopping token, or converting a tool refusal into successful settlement.

Whole-turn regeneration uses that same `automatic_recovery_blocker` before
spending an attempt or clearing presentation. Problem: the error classifier
marked provider 5xx and temporary gateway failures as retryable
`upstream_error`, while the outer retry policy omitted that kind; it also
ignored explicit manual/condition-change metadata on otherwise transient kinds.
Decision: eligible `upstream_error` failures join the existing bounded
whole-turn policy, and all eligible kinds obey the common typed blocker.
The configured inner gateway wait still defaults to waiting for recovery or
owner cancellation. If it exits, the existing whole-turn allowance, opt-out,
Stop, partial-reply protection and before-tool-entry guard govern regeneration.
Paid usage is carried forward, pristine input is restored, and ordinary
invocation owners run each admitted attempt with one terminal settlement.
Alternatives rejected:
changing error taxonomy hides the provider cause; raising inner budgets does
not repair outer-policy drift; a separate retry loop forks lifecycle ownership.
Verify `test_upstream_auto_recovery.py` and the real invocation/dispatch/storage
scenarios in `tests/llm/test_upstream_turn_replay.py`.

Announced model rounds must close before outer recovery or terminal failure.
Problem: dispatch exceptions bypassed normal round settlement, leaving an open
round across the retry reset or final error. Decision: the root adapter closes
the announced round when model dispatch/preparation raises, after dispatch
cleanup, then propagates the original exception. A failed close propagates as
an event-delivery failure, retaining the dispatch cause and blocking provider
replay. Successful dispatch keeps normal settlement ownership. Alternatives
rejected: treating `retry_reset` as an implicit close weakens durable event
pairing and leaves non-retried failures open; a second retry loop or persistent
round-state flag duplicates lifecycle ownership. Verify
`tests/tasks_pkg/test_model_round_failure_boundary.py` and the replay scenarios
above, including explicit retry opt-out.

Build-specific verification: `tofudb_native_parity_20261001.json`.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->
