# Tool execution and settlement policy

This guide owns side-effect and result-settlement policy referenced by the
[tools domain map](../modules/tools_execution.md).

Verified skill installation accepts the task's existing Auto write grant,
including unattended execution. Manual mode requires an attended decision.
`ToolSpec.auto_approval_tools` is the declaration of this exception; other
receipt-requiring tools retain human confirmation by default. Both approval
paths mint an exact, expiring, single-use gateway receipt before the handler
can install. Scope, origin verification, bounded attempts, and immediate
instruction disclosure are owned by [the skills contract](../modules/skills.md).

## Concurrent tool rounds (owner decision 2026-10-28)

**Problem:** every write-partition call — `run_command`, file writes,
non-read-only MCP tools, `kill` — ran one at a time on the dispatcher thread,
and `ask_human`/`await_task(wait)` plus blocking approvals ran inline in the
admission loop before any sibling started. A round of N slow commands cost the
sum of their durations, and one pending human answer or approval held back
every unrelated call of the round.

**Decision:** `execute_tool_pipeline` admits a round without blocking (parse,
authority and reusable-result checks), then `tool_dispatch/_execution.py`
starts every admitted call at once on the shared demand executor; its
idle-reuse hint never caps concurrency. Approval, the post-approval Stop check,
human-wait argument injection and pre-hooks run on the call's own worker, so a
pending human decision delays only that call; one invocation-local lock keeps
settlement exactly-once while refusals settle from workers. Order survives
only where concurrent starts would silently change what sequential calls meant
and keeping it is cheap (`tool_dispatch/_ordering.py`):

- mutations of the same resolved file (`write_file`, `edit_file`,
  `apply_diff(s)`, `insert_content(s)`, compared after canonical rebinding and
  symlink resolution), `desktop_write_file` calls on the same device path,
  memory mutations of the same `memory_id`, and browser page actions on the
  same `tab_id` (an action without one conflicts with every tab) apply in call
  order; a successor waits for its predecessor's approval and handler before
  its own approval starts;
- `todo_write` revisions apply in call order;
- every `run_command` working directory is frozen at admission in call order,
  so a sticky `working_dir` set earlier in the round is honoured, and after the
  round the sticky directory is re-asserted to the call-order result. A command
  that inherits an earlier sibling's directory waits for that sibling's
  admission verdict (never its completion) before its own approval: a sibling
  refused at approval, by a pre-hook or by Stop never moved the sticky
  directory, so the command falls back to the previous candidate.

Tool messages still replay in provider call order. Each call keeps its former
lane's contract: writes, ordered state and ratified human waits have no pool
ceiling and no declared timer, run admission and handler in a copy of the
dispatcher's context (for example the swarm progress sink that `run_command`
output feeds), and after Stop are joined and keep their own receipt and effect
evidence. Every other call keeps the bounded contract: the pool ceiling and its
declared timer, measured from handler entry, and the Stop placeholder. A call
not yet in its handler when Stop arrives settles `aborted` without entering it.
A round that writes still never caches its write-sensitive reads, and
approval receipts mutate under the task receipt lock. Several foreground
commands can now run in one task on every execution path (see "Per-command
slots on every execution path" below): the task's single-command slots (the
`_subprocess_pid` mirror read by the interrupt route, abort signal and stall
reaper, and `_cmd_handoff_control` read by the background route) name the
newest live command and fall back to the next one when it exits or detaches;
a background request reaches the exact call it names, every command can hand
off on its own elapsed time or a steer, and an interrupt addressed to one live
command is never consumed or voided by its sibling's read loop. The streaming
pre-executor still starts only read-only calls while a response streams; side
effects start only once the response is final.

**Alternatives rejected:** keeping a serial write lane, a global write lock or
a per-tool-name lock (each reintroduces the serial lane); ordering every
mutation of a round (loses most of the gain for unrelated files); deciding
approvals on the dispatcher before starting siblings (a pending approval still
delays their settlement); freezing only explicit `working_dir` values (a later
call without one races the earlier sticky update); serializing foreground
commands to keep the single-command slots (forfeits command overlap). Writes
without a declared target (`run_command`, MCP tools) are not target-ordered:
their targets are unknowable, and the owner asked for them to overlap.

Known limits: native-application rounds follow these rules through the
settled call order ("Native application rounds" below), where a sibling counts
as admitted once it is approved, because Rust decides a round's approvals
before any call starts. The interrupt route and the durable interrupt sweep
still name the newest live command, not a chosen one.

Measurement (`/tmp` harness through the real pipeline, executor replaced by a
0.5 s sleep, median of three rounds): 4 writes 2.005 s → 0.506 s, 8 writes
4.009 s → 0.514 s. `tests/tools/test_parallel_dispatch_overlap.py` pins overlap,
same-target order, waits, approvals, Stop, replay order, working directories
and the max-not-sum round time; `tests/tasks_pkg/test_run_command_background_handoff.py`
pins concurrent foreground commands.

### Per-command slots on every execution path

**Problem:** the per-command view lived only in the conversation handoff
proxy. Swarm, Goal and Flow workers, headless requests, Evolution and timers
ran `run_command` on the task itself: the first command to finish erased the
live sibling's PID mirror, a sibling's read loop voided an interrupt addressed
to another live command, and the stall watchdog judged task-level silence and
interrupted the newest command. A stalled command beside a finished sibling
therefore failed the whole worker task, and a chatty newest command could be
interrupted in place of the silent one.

**Decision:** `lib/project_mod/foreground_command_slots.py` owns the per-task
foreground-command registry. Both `run_command` runners always run against a
`CommandTaskView` (the conversation `DetachableCommandTask` extends it with
handoff), so every path publishes each command's PID into the registry, keeps
the mirror on the newest live command, hides an interrupt addressed to a live
sibling and voids it only once that pid no longer runs. Each command keeps its
own output clock. The stall watchdog (`reap_stuck_running_tasks`, judging with
`lib/tasks_pkg/liveness.py`) interrupts the live command whose own output has
been silent longest past `TOFU_STUCK_TASK_MAX_SILENT_SECS`, while the dispatch
heartbeat is stale; a sibling's output no longer hides it, and siblings keep
running. Only an interrupt that a live command leaves unconsumed past its
grace, with both task clocks stale, fails the task. The same ladder applies to
Swarm, Goal and Flow workers: a worker's silent command is interrupted and the
worker continues; a wedged worker settles with its own typed stall. Stop still ends every command, and a stdin prompt
keeps only its own command in the foreground. A child invocation hands a
command still running after `AUTO_BACKGROUND_AFTER_S` off and reads its handle
with `poll`; it owns no conversation delivery, so a steer never moves it, and
each concurrent call can also choose `background=true`.
A Flow parent blocked on a child invocation advances its clocks only through
child events, so it publishes the wait (`explained_wait.child_invocation_wait`)
and the reaper treats its silence as explained while that child is live: the
child's silent command is interrupted, never the whole Flow turn. Once the
child is stopped or settled, the parent's own clocks decide again, counting
from the end of that wait (`liveness.silence_origin`).

**Alternatives rejected:** clearing the mirror only when it names the
command's own pid (a live sibling still disappears when the newest command
exits first); keeping the task-level silence gate for the interrupt (a chatty
sibling hides a stalled command until it exits, and the newest command is
interrupted instead); a second registry inside `run_command.py` beside the
proxy's (two owners of one mirror); wrapping only `run_with_steer_handoff`'s
unattended branch (misses the Evolution and other direct runner callers).
Specs: section 7 of `tests/tasks_pkg/test_run_command_interrupt.py` (real
subprocesses on an unattended worker task: interrupt one, watchdog after a
sibling exits, watchdog beside a chatty sibling, Stop, background beside a
foreground command) and the stdin case in
`tests/tasks_pkg/test_run_command_background_handoff.py`; the interrupt,
watchdog and stdin specs fail on the previous code.

### Task counters written by concurrent calls

**Problem:** with every call of a round running at once, handler-owned
read-modify-write updates on the task lose increments: the three-attempt skill
install budget admitted a fourth install, two identical `search_tools` calls
both counted as the first, two searches dropped each other's disclosed
schemas, and MCP search telemetry under-counted. **Decision:** each owner
makes its update one step under a module-level leaf lock
(`handlers/skills.py`, `handlers/tool_gateway.py` `_note_search_query`,
`tools/disclosure_state.py`, `context_telemetry.record_mcp_search`), matching
the existing task-local locks for execution budgets, receipts and media bytes.
**Alternatives rejected:** serializing those tools in `_ordering` (loses their
overlap for a microsecond update); a per-task lock dictionary on the task (state
copied onto the task for no gain). Specs force every reader to read before any
writer and fail on the previous code.

### Sibling gateway calls run concurrently (owner decision 2026-11-04)

**Problem:** `handle_execute_tools` held one per-task gateway lock across
normalization, a whole program or child batch, and settlement. Sibling
`execute_program` / `execute_tools` calls of one response therefore ran one at
a time while the round showed every one of them as running, and a slow program
held back unrelated siblings for its full duration.

**Decision:** exactly-once is scoped to the receipt key, never the task: one
occurrence executes under `_receipt_claim(task, receipt_key)`, so only replayed
copies of that occurrence wait for it. The task-shared gateway state (receipt
table, child round numbers, program ledger IDs, authoring-failure latches and
the direct-sibling delegation FIFO) is published under `_gateway_lock`, which
is never held across normalization, execution or settlement. Child row
allocation also announces those rows, so the lock is taken before the task's
event-lane locks and never while holding one; that keeps one sibling's rows
and events adjacent in both streams. Program parent rows and their adoption
evidence use a per-task leaf lock in `orchestrator/_programmatic.py`. Call
order survives through `_ordering`: the top-level pipeline (and native
`prepare_response`, through `publish_prepared_response_siblings`) publishes
the response's gateway occurrences in provider order (`GatewaySiblingOrder`)
before any of them runs. A child that `_ordering` orders (the same-target
mutations and `todo_write` above) waits until every earlier-issued sibling
has settled: a program picks its next child from data, so a later sibling
cannot know whether an earlier one will still mutate its target. Waits point
only at earlier calls, so they cannot cycle, and Stop releases every waiter.

An occurrence is its published call ID and round row, so a nested call that
reuses a call ID never takes or settles a sibling's place. The worker
submitted for an occurrence adopts it and settles it when that worker
returns, raises or is cancelled unstarted. A collection deadline only stops
waiting for the receipt: the expired worker keeps its place, because its
program may still mutate, and a write is unbounded, so a program returns only
after its own mutations ended. An occurrence no worker adopted never runs and
is released by whoever learns that first: the dispatch invocation's exit
(refused, failed before submission, Stop), a native callback joined without
a worker, the native gateway's close, or the next response's publication. A
nested `execute_tools` / `execute_program` call keeps its parent's place, so
its ordered grandchildren follow the same earlier siblings. Native callbacks
run `execute_tool_pipeline` with `publish_direct_gateway_siblings=False`, so
a callback never replaces the published order. Native `prepare_response`
publishes only the gateway calls the native runtime will dispatch without a
host decision: parsed without refusal (the argument contract and caller
envelope native admission checks) under a `read` permission. A call under a
write or approval permission runs only after an approval and never when it is
denied, so it takes no place.

**Alternatives rejected:** keeping the task-wide lock (the serial lane this
removes); a per-tool-name lock (two programs writing one file lose their call
order, and the concurrent-rounds decision above already rejects it); ordering
children across siblings by target only (an earlier program's future targets
are unknown until it runs); ordering the gateway calls themselves at the top
level (restores the serial lane for every program).

Alternatives rejected for settlement: settling every published occurrence
when its dispatch invocation exits (releases an expired earlier program that
is still running, so a later write can overtake it); publishing every parsed
native gateway call (a call the runtime never dispatches holds a later
sibling until Stop); reporting the native dispatched set from Rust before
callbacks (exact, but a native stdio contract change; the publication rule
already mirrors every per-call refusal for read gateway calls).

Known limits: an ordered child of a later sibling waits for every earlier
sibling, including ones that never touch its target. Direct (non-gateway)
siblings of the response are not part of this order; their relation to
gateway children is unchanged. Unordered children (reads, `run_command`, MCP
tools) never wait. A gateway call under a write or approval permission is not
ordered against its siblings. Native publication relies on Python's parse and
argument contract matching native admission (their differential oracles); a
read gateway call native admission refused without that refusal in Python
would hold a later sibling's ordered child until Stop or the native deadline.
Siblings settle concurrently, so the program authoring-failure latch counts
failures in settlement order. Specs:
`tests/tasks_pkg/test_gateway_sibling_concurrency.py` runs real JavaScript
programs through the real handler and pipeline: sibling programs meet on a
barrier (a task-wide lock anywhere on the path breaks it), a duplicate receipt
key executes once while other keys overlap, an ordered child of a later
sibling waits for the earlier sibling whether it returned or failed, an
expired earlier program still running keeps its place, Stop releases that
wait, a child re-entering the gateway from its worker does not deadlock, a
nested gateway call keeps its parent's place, a reused call ID never takes a
sibling's place, native sibling program callbacks overlap and keep the order,
and a native sibling that awaits an approval or whose callback fails first
never holds back a later write.

## Native application rounds (owner decision 2026-10-28)

**Problem:** the native application path kept the serial lane the Python
dispatch removed. The Rust scheduler (`packages/tofu-runtime/src/tool_scheduler.rs`)
started only explicitly qualified reads on their own workers and ran every
write, `run_command` and `todo_write` one at a time on its dispatcher thread,
and `NativeGateway.execute` serialized the same callbacks again behind one
lock. Four 0.5 s writes took 2.02 s and eight took 4.05 s (peak overlap 1).

**Decision:** application composition (`run_native_task`) selects
`concurrent_tool_dispatch` in the stdio contract. Rust starts every admitted
call of a round on its own joined worker (`Scheduler::concurrent`); a call
waits only for the earlier calls the host names. The host names them on round
settlement: `NativeGateway.prepare_response` runs the `_ordering` authority of
`execute_tool_pipeline` (`tool_dispatch.api.plan_concurrent_round`) over the
settled declarations and binds the result to the settlement's
`NativeToolOrder`; the `round_ack` carries it as `tool_order` (model call ID
to earlier call IDs). The same step freezes every `run_command` `working_dir`
into its prepared occurrence in call order, and each finishing command
re-asserts the call-order sticky directory. A command that inherits a
sibling's directory settles it at its own decision
(`CommandDirectoryPlan.resolve`): Rust decides a round's approvals one at a
time in call order, so every earlier sticky-setting sibling is already decided
(`settle` publishes each verdict), and a denied or never-decided sibling never
moved the sticky directory. The gateway lock is not used in this mode. Results still replay in provider call order. Rust validates the
order (earlier calls of the same response only; required on returned
evidence, refused otherwise) before any approval or effect. A declared
predecessor that never reached dispatch (denied or refused) is not awaited.
A waiting call observes parent Stop and its deadline within 10 ms, and a batch
Stop wakes it at once; it then settles (cancelled or deadline-exceeded) without
entering its handler. Declared timeouts and the human-wait exemptions are
unchanged: every callback still runs through `execute_tool_pipeline`, which
applies them per call. An
explicit `parallel_tool_names` keeps the read-qualified scheduler for callers
that ask for it; the two selections exclude each other, and an archive that
predates the field refuses the configuration before any provider or tool
effect. Because this mode always settles the model's declarations,
`tool_order` names model call IDs even when a recycled ID was reminted (see
"Recycled model call IDs" below).

**Alternatives rejected:** computing ordering keys in Rust (file identity
needs the host's root resolution and symlink rules, so a second copy would
diverge); ordering in the gateway alone (it cannot tell a denied or refused
predecessor from one still in flight, so a successor could wait forever, and a
Stop while waiting would need a fabricated result); sending every callback the
batch membership (a new handler port for information the settlement already
carries); settling an inheriting command's directory when it starts (its
approval receipt is bound to the arguments its decision saw); keeping the
ordered lane for writes (the owner decision).

Known limits: native approvals are still decided one at a time, in call
order, on the exclusive control slot before the round dispatches, so a pending
human decision delays the start of the whole round rather than only its own
call; a sibling that was approved but then refused by a pre-hook or Stop before
its handler still counts as admitted for an inheriting command (its pre-hooks
run after every approval of the round); a command refused before its handler
does not move the sticky directory.

Measurement (`/tmp` harness through a built archive, stdio host, gateway
default Auto approval and the canonical pipeline; handler replaced by a 0.5 s
sleep; median of three rounds): 4 writes to different files 2.018 s → 0.502 s,
8 writes 4.052 s → 0.507 s (peak overlap 1 → N); 4 and 8 writes to one file
stay ordered (2.017 s and 4.046 s, peak 1). Specs: Rust `tests/tool_scheduler.rs`
and `tests/agent_stdio.rs` (`concurrent_*`, waiting-successor Stop and deadline),
`tests/agent_core/test_native_process.py` (settlement ACK),
`tests/tasks_pkg/test_native_gateway_concurrent.py` (including a denied
sticky sibling) and the installed
`tests/tasks_pkg/test_native_parallel_writes_installed.py` (N slow writes take
about one write; writes to one file keep call order).

## Native application approval composition

An explicitly read-qualified native composition still uses the live write and
ordered-state classification: only admitted read names overlap there.
Scheduling metadata (the qualification and the settled call order) supplies
neither approval nor tool visibility. Original model occurrence IDs and argument
proofs must both match the settled response before execution; equal arguments
do not make two model occurrences interchangeable.

Recycled model call IDs: when the gateway settles the model's declarations, a
native decision precedes pipeline admission. A provider that repeats a call ID
(positional `{tool}_{index}` IDs) made admission remint it after the receipt
had been bound to the old ID, so the write fell back to a human decision
(120 s, then Stop). `NativeGateway.prepare_response` applies admission's own
remint rule first (`tool_dispatch.api.repair_prepared_call_ids`). Callbacks
and checkpoint records keep naming the model's ID; the canonical row, receipt
and effect use the admission ID. Alternative rejected: carrying a receipt
across the remint inside admission, which cannot tell this occurrence's
receipt from a stale one under the same ID. Known limit: a registered child's
file-batch recovery refuses a batch whose model reused an earlier call ID,
because its persisted effect carries the admission ID. Spec:
`test_recycled_model_call_id_keeps_its_approved_identity`.

Concurrent pipeline invocations own separate settlement/envelope carriers.
Shared result caches, call-ID receipts, unchanged-result receipts and search
accumulator budgets publish under a task-local lock; program input receipts use
their existing owner with a separate publication lock. No such lock covers tool
execution. Cache compaction/discard compares the exact observed receipt so an
older call cannot replace a newer result. Progress hashes are computed outside
the lock and merged atomically; closing the carrier prevents later publication.
Success, failure and Stop join every admitted callback before terminal delivery.

`NativeGateway` uses the application's canonical decision gate when no explicit
`decision_policy` is supplied. `authorize_prepared_tool_call` admits one already
parsed occurrence through the same task partitions, Auto/Manual exceptions,
Git guard, delegate and human gate as ordinary dispatch. Automatic exceptions
mint the existing `auto_write_mode` receipt; human, delegate and Git decisions
retain their distinct authorities. It executes no handler. Execution consumes
the exact prepared occurrence and its unused receipt through the normal
pipeline, preserving later hooks and authority checks without another parse or
human prompt. An explicit trusted decision-policy port remains available.

Native round delivery supplies factual round identity. Private prepared carriers
join the existing bounded pending-approval record; arguments and carriers are
charged before the gate and rechecked after canonical argument rebinding. Task
tool rows are borrowed. Worker metadata mutations use a short synchronous lock;
publication, decision waits and handlers run outside it. Cancellation joins the
actual worker and withdraws its row/grant. Terminal projection first withdraws
accepted grants not consumed by execution, refusing a still-pending callback.
Denied or faulted decisions execute nothing and preserve their settled tool row.

Problem: native writes required a separate caller policy and could not enter the
existing application approval lifecycle. Decision: expose decision/withdrawal
ports from the same tool-dispatch authority and reuse its parsed occurrence.
Alternatives: duplicating Auto/Git/human rules diverges; treating all native
writes as preapproved bypasses policy; reparsing creates duplicate tool cards.
This supplies native application composition, not production default selection.
Verify native gateway Auto/Manual/denial, actual approval-owner isolation,
cancel/pressure withdrawal, unused grants, hook refusal and installed native
application tests across disposable storage authorities.

## Approval delegation (`set_approval_delegate`)

The orchestrator may register a task-scoped, natural-language approval policy
through `set_approval_delegate`; a call that would reach the human
write-approval gate is then judged first by a bounded, tool-less adjudicator
child invocation against that policy (`lib/tasks_pkg/approval_delegate.py`).
The adjudicator is a third receipt authority (`agent_adjudicator`, beside
`human` and `auto_write_mode`): an approve binds a single-use receipt to the
exact call, a deny settles the call rejected with the reason returned to the
orchestrator, and any undecided, malformed, or timed-out verdict falls
through to the ordinary human gate. The delegate never widens the gate:
`auto_approval_tools` remains the only Auto exception, destructive-command
analysis still runs first, child invocations are never adjudicated, the
policy dies with its task and is not inherited, wildcard scopes are rejected,
and `set_approval_delegate` is stripped from sub-agents
(`SUB_AGENT_DENYLIST`) and read-only waves (`READ_ONLY_AGENT_EXTRA_BAN`).

## Exact settlement and identity

Explicit task `toolSpecAllowlist` and `toolSpecDenylist` select registered
`ToolSpec.key` groups, not individual function names (`project` owns
`run_command`; `evolution` owns `evolution_explore`). Unknown keys or malformed
lists fail before registry builders and provider dispatch; the turn clears its
tool authority and reports the configuration error instead of continuing
text-only. An empty allowlist still selects no groups; absent/null allowlist
leaves ordinary capability gates in charge. Known keys never override owner,
plugin, role or capability gates. Explicit caller-provided tool schemas and
already-authorized child invocation tools retain their separate assembly path.

Problem: reading compatibility `tools` schema arrays as settings failed before provider entry.
Decision: use the shared flag normalizer, preserving declarations and dotted policy. Rejected alternative: dropping valid caller tools.
Verify `test_tool_gateway_contract_assembly.py`: protocol translation, exact contracts and programmatic on/auto/off.

**Problem:** silent filtering can remove tools required by a long experiment,
or ignore a misspelled denial while the model continues spending. **Decision:**
validate both selections at the registry owner and propagate their typed error
through the task assembly boundary. **Alternatives:** automatic function-name
translation could grant an entire write-capable group unexpectedly; text-only
fallback changes the requested task. Neither is used for explicit policy errors.

A tool call is settled exactly once, including cancellation and failure. The
owner's explicit abort also vetoes reusable-cache delivery before execution. The
runtime distinguishes transport failure, invalid arguments, unavailable tool,
permission denial, handler failure, and user abort. Repair is limited to
syntax/shape problems and cannot silently change the requested capability.

Handler presentation is provisional until execution returns and deadline
decisions settle. Problem: an exception after a handler's `done` update kept a
successful card and audit, while cached failures prevented a fresh observation
on a later call. Decision: the common executor corrects the verdict and shares
its effect-recovery error between presentation and model delivery; canonical
settlement evicts non-success observations and emits the execution outcome
audit once. Proven admission refusals may still audit before dispatch.
The pipeline marks its ownership on each round before dispatch; direct executor
consumers (Paper, Timer) audit on executor exit instead of handler presentation.
Alternatives rejected: rewriting exception text cannot correct cached or
persisted state; retrying automatically could duplicate an uncertain write.
Each subsequent call still passes normal authority and recovery admission.
Verify `tests/tasks_pkg/test_tool_outcome_finality.py` for legacy/V2 delivery,
late exceptions, unknown writes, timeout attribution and successful read retry.

**Exact failure text.** Problem: the common error projection re-escaped multiline
diagnostics and already-formatted partial results inside JSON, consuming context
again. Its observation reconstruction also omitted retained error items.
Decision: `result_projection_text.py` owns the existing gateway's Unicode-length
text framing. The declared error policy may place `summary` after a compact JSON
metadata line when the complete representation is smaller in UTF-8. Error codes,
retryability, next actions, items, retention references and cursors remain in
metadata; short results and unsafe control text keep their original JSON.
The error reader requires an exact body length, and owned observations retain
every item. Source text cannot establish a canonical failure verdict.
Alternatives rejected: clipping diagnostics loses recovery evidence; changing
only the artifact caller leaves other failure paths re-escaping their results;
dropping the error wrapper loses the admitted execution verdict. Verify
`test_error_text_projection`, canonical pipeline, outcome and timeout suites.

**Completed speculative occurrences.** Problem: excluding a failed fetch from
reusable cache discarded an already completed read, so formal dispatch issued
the same request again, including after a server wait hint. Decision:
`tool_dispatch/_prefetch.py` carries the domain source result in the existing
bounded task cache under a separate occurrence key. It binds the physical task
and announced row, principal, conversation/Turn/attempt/world, call ID, arguments,
caller, user question, execution configuration and tool contract before IO.
Project sources also retain the read-start generation. The normal identity-repair owner may remint a
recycled correlation ID only after verifying this same occurrence. Equal arguments
or a copied row never authorize consumption by another call.

The ordinary parser, decision gates, hooks, budgets and executor still run. A
matching occurrence takes priority over reusable observations; the admitted
domain handler consumes its source result once and uses its existing formatting
and effect-recovery owner. This preserves unknown-effect operation evidence and
all successful members of a partially failed batch. A newer concurrent success
is retained: publication invalidates only the reusable receipt observed at read
start. Rejected/aborted settlement, provider restart and orphan retirement drop
unused receipts. Normal stream-worker shutdown preserves completed handoffs for
the following dispatch. Consumption checks cancellation and closed result
collection even when the receipt was already withdrawn; settlement never grants
permission for another source request. Capacity eviction or invalid identity falls through to
normal dispatch. These transient receipts never survive restore or become retry
authority; later calls can observe service recovery.

Alternatives rejected: admitting failures to argument-addressed cache masks
later recovery and conflates distinct calls; formatting a final failure before
executor entry loses its admitted operation evidence; a second unbounded result
registry would add retention and synchronization owners. Verify
`test_prefetch_occurrence_handoff.py`, streaming/source-binding, call-ID repair,
cache-generation, outcome/cancellation and installed native execution neighbors.
The controlled root-loop fixture delivers a failed read to the next model round
before a distinct successful retry. A local 503 with `Retry-After: 3600` now uses
one request across prefetch and formal dispatch, instead of two.

Resource measurement: the existing probed cache capacity and lock remain the
only retention owner; no worker or persistent state is added. In a local fixture
with an explicit 16-entry capacity, 1,000 publications of about 32 KiB retained
524,208 payload bytes, 535,956 traced bytes in total, and 360 traced bytes after
clearing the cache. Median lookup time was 0.6 μs with no pending occurrence and
14.1 μs with matching identity. These are synthetic local retention/lookup
measurements, not production throughput claims.

**Admitted fetch batch completeness.** Problem: the ordinary fetch handler silently
truncated `urls` to ten entries although the canonical schema declares no such
limit; streaming and Paper already retained the full batch. Decision: the handler
preserves every admitted source through the existing demand executor, including
partial failures. The executor's existing resource
policy owns worker reuse and demand; the handler adds no independent count gate.
Alternatives rejected: clipping prefetched records discards already obtained
source evidence; adding a schema limit would restrict the advertised capability.
Verify `test_fetch_batch_completeness.py`. The
controlled 13-URL fixture now makes 13 requests and retains 13 source records,
instead of silently issuing ten. Both legacy and V2 runs reached 13 active source
entries on the fixture host through the existing executor. This measures admitted
work and demand response, not production throughput.

**Settled result publication.** Problem: a worker returning after the parallel
deadline could overwrite result cards and emit new handler frames after the
timeout already reached the model. Decision: common result settlement revokes
that occurrence's handler presentation through its existing runtime context.
The shared publication fence protects bounded result mutations and event
admission; enclosing invocation fences remain in force. A worker entering
after collection closed never enters its handler. Late success or failure
cannot replace the frozen result, verdict, timing or outcome audit.
Resource ownership remains registered until worker exit; closing presentation
does not join an observer, prove that external effects stopped, or grant retry.
Alternatives rejected: preserving only the status still permits contradictory
body text; joining an uncooperative handler removes the finite pool deadline;
discarding its runtime context loses cancellation and cleanup ownership.
Verify handler-entry, display-preparation and late-return races in
`tests/tasks_pkg/test_tool_outcome_finality.py`.
The fence lives on the active tool context, with no new worker or global
registry. The process-local closed marker is stripped by the common storage
projection while effect evidence stays intact. A CPython 3.12.13 allocation
sample of 2,000 active contexts measured about 171 bytes per fence; separate
100/1,000/2,000-cycle create/close/unregister samples each retained 208 traced
bytes and zero active contexts. The durable round projection added zero bytes.

**Result collection recovery.** Problem: an omitted completed Future discarded
an available executor receipt and the final fallback claimed the tool was
unknown. Decision: the pipeline harvests missing terminal Future receipts before
ordered replay, using the same recovery path after a collector deadline. It
neither waits again nor re-enters handlers, and an already collected verdict
always wins. Canonical capture, result budgeting, search accumulation and ordered
model delivery keep their existing owners. If no receipt can be recovered,
`tool_result_missing` identifies a collection failure without asserting tool
absence or changing effect evidence. Retry guidance uses the existing
`ToolContractV2.effectRecovery` policy; missing, contradictory or changed
execution evidence requires outcome inspection. Every new call still passes
normal authorization, and independent work may continue. Alternatives rejected:
repeating the handler may duplicate effects; interpreting a missing result as
tool absence sends the model toward the wrong repair; treating all missing
results as safely retryable invents execution authority. Recovery borrows the
current invocation's Future/result maps and adds no pool, retained registry or
durable metadata. Verify `tests/tasks_pkg/test_tool_result_collection.py` for
original success/failure, sibling ordering, search evidence and recovery policy.


Root MCP authority refreshes before each request through ordinary owner/policy
assembly on detached carriers. Only names in the root's initial MCP candidate
or executable surface can return; catalog and execution contract/namespace
metadata publish together. Provider discovery policies/counts stay frozen so
revocation cannot turn searchable tools into eager wire declarations.
Non-MCP grants and frozen wire remain unchanged.
Child and project-dispatch scopes never rebuild the root inventory. This avoids
latching an empty startup catalog after asynchronous MCP connection, and removes
revoked grants without treating durable declarations as authorization.

Frozen MCP declarations are visibility, not execution grants. If an exact MCP
name in the task's wire schema is absent from its live executable catalog,
ordinary ingestion settles it as `tool_unavailable` with
`reason=declared_without_live_authority`, `declared=true`, and
`executable=false`. It runs no handler and incurs no hallucination streak or
autopilot hallucination penalty. Unknown MCP names still follow ordinary
hallucination rejection; an empty live catalog remains deny-all. Reconnection
or restored permission must rebuild live authority before the call can proceed.
The receipt identifies the membership mismatch, not an inferred outage cause.

**Problem:** cache-stable MCP wire declarations can survive disconnect,
discovery failure, or permission removal; using execution membership alone to
label names fabricated misdiagnoses model calls. **Decision:** classify exact
wire-declared/live-absent names before generic name repair, retaining both
membership facts in the refusal. **Alternatives:** unioning declarations into
the execution allowlist revives revoked grants; admitting arbitrary `mcp__`
names bypasses exact membership; rotating frozen wire breaks prefix stability.
Verify assembly → refusal → recovery in
`tests/mcp_protocol/test_mcp_declaration_lifecycle.py`.

**Declaration slot encoding.** Problem: the frozen wire lives in conversation
settings, and every catalog page, list, get and Turn settlement reads the whole
header. As JSON text (live mean 10.7 KB, maximum 32 KB) it pushed 397 of 533
native headers past the 8 KiB inline entity value bound, so a cold sidebar
catalog page made one blob read per header (live mean 6.3 ms against 0.25 ms
inline, over 16k payload segments on the shared volume) to project four
settings keys. Decision: version-2 slots keep identity/reset semantics and
store the exact wire JSON text zlib-compressed and base85-encoded; version-1
text stays readable. One fail-soft post-ready pass per writer process
(`start_legacy_slot_compaction`) re-encodes owned and copied version-1 slots
through the existing settings snapshot CAS, leaving `updated_at`, `rev` and the
decoded wire unchanged. Seeded from the live headers, blob-backed headers fell
to 50 and catalog blob reads from 806 to 96 per page; wires whose compressed
form still exceeds the bound stay correct but blob-backed. Alternatives
rejected: a separate body record outlives conversation purge on both backends
(the lifecycle-free records already rejected for this authority); an engine
catalog projection index needs a native format change and backfill; dropping or
reselecting the wire breaks prefix stability. Verify
`tests/storage/test_mcp_declarations.py` on SQLite and native lanes.

Retries of side-effecting tools require an idempotency contract. The generic
LLM retry loop must not replay a completed write, webhook, browser action, or
human-guidance resolution.

**Error recovery guidance.** Problem: a blanket "stop and report" can end useful work, while "retry" after interrupted execution can repeat effects. Decision: the common typed-error fallback scopes recovery to the failed operation, asks for outcome verification before repetition, and allows independent authorized work. `retryable` alone is neither replay authority nor a task-stop instruction. Owner-specific advice and error codes retain precedence. Command watchdog and restart receipts preserve partial output and describe unconfirmed effects; an absent process and exit receipt do not prove failure before execution. Custom-tool timeouts, transport failures and rejected oversized results likewise require outcome inspection. A result rejection does not undo the tool operation. JavaScript failures after child calls prefix specific authoring advice with effect reconciliation and retained-input reuse, in both program entry points and before the bounded hint can truncate it. Missing retained inputs explain the evidence gap; byte-budget failures recommend smaller retained-input groups or artifact pages, never source re-execution. Alternatives rejected: treating all failures as retryable weakens duplicate-effect protection; blanket aborts discard independent progress. Cancellation, admission and settlement policy remain with their existing owners. Runtime failures suggest environment repair or available direct tools; cancellation advice never asks to resume execution.

Problem: a failed read's unconditional query-cache eviction could erase a
successful concurrent read. Decision: each invocation retains the exact receipt
it published under the shared receipt lock; failed settlement withdraws only
that receipt if still current. Successful compaction also carries the producer's
exact receipt; failed results never rewrite a success. Alternatives rejected:
argument or body equality cannot establish
receipt ownership, including when a successful document contains error-shaped
JSON. No task-wide receipt history is added. Verify settlement/cache tests and
the ordinary timeout/outcome-finality ladder.

`execute_tools` receipts key call ID with canonical arguments, model round, and
world version because a provider may recycle a positional call ID. A proven
redispatch of the same gateway occurrence may reuse its receipt; changed calls
execute, and a cached failure retains its failure verdict. The task-local table
evicts oldest entries beyond 256.

**Nonretryable batch failures.** Problem: a generic `partial_failure` wrapper
hid explicit child error classifications from the shared failure breaker.
Decision: a nonempty batch whose children all settled as error/rejected with
typed `retryable: false` errors publishes `tool_batch_failed` through the
existing error envelope. The normal compact receipt view preserves each
child's code and recovery advice; original canonical children remain available.
The unchanged operation participates in the existing bounded failure streak,
while corrected arguments or independent work remain possible. Success,
retryable, untyped, delegated and aborted children prevent this aggregation.
Alternatives rejected: treating any failed child as a terminal batch would
stop useful mixed work; recursively interpreting arbitrary JSON would turn
source documents into execution policy. No child is re-executed to classify
its result. For this typed batch failure, shared operation fingerprints omit
only `calls[].call_id`, the gateway's correlation field; child order and
business arguments (including an argument named `call_id`) remain significant.
Otherwise a new response ID would disguise an unchanged failed operation.
Ordered child codes live in `items[0].failureCodes` and participate in that
fingerprint, so a newly observed failure cause starts a new recovery attempt.
Missing, malformed or incomplete cause identity fails open instead of proving
an unchanged failure; the outer error and child recovery advice still apply.
Verify `test_gateway_failure_recovery.py` with root/native progress,
delivery-budget and mixed-result neighbors.

Call IDs are correlation tokens, never conversation- or process-global
execution identities. Fresh assistant batches repair blank or recycled IDs
before history is committed. Each dispatch indexes earlier/enclosing IDs once,
excluding its current assistant carrier and current round. Settled content
belongs to one pipeline invocation and leaves scope after its aggregate-budget
barrier, including across nested gateway dispatch.

**Handler receipt admission.** Problem: a handler could return another call's
ID or a malformed result, allowing sibling output to be overwritten or a
returned effect to be acknowledged without a usable receipt. Decision: the
common executor validates the three-field result, exact dispatched ID and
boolean search flag before acknowledging return or exposing content to cache
and settlement. Invalid receipts fail the owned occurrence, withhold their
payload and retain the existing effect-recovery policy. Non-replayable writes
remain unknown and require reconciliation; other invalid receipts report
`tool_result_invalid` with adapter-repair guidance. A valid empty content value
remains success. Alternatives rejected: rewriting a foreign ID invents
provenance; gateway-only checks miss direct executor consumers; retrying the
handler can repeat effects. Ordinary approval and cancellation still apply.

Every ordered position inside one provider response executes and settles
independently, including exact twins, after provider-response admission. Duplicate IDs are reminted rather than used to collapse siblings. The narrow routing-repair exception applies when
the same response has an `execute_tools` carrier: post-authority direct calls
donate canonical name/argument identities, and matching `calls[]` children are
consumed FIFO with a `delegated` receipt. Same-channel twins, changed arguments,
different responses, and JavaScript programs remain independent. Malformed
occurrences receive typed rejection and never borrow a sibling. Historical
consumers pair only an adjacent assistant/result run by ID occurrence;
ambiguity fails closed.

**Provider occurrence preservation (2026-10-21).** Problem: the response-signature guard collapsed equal tool inputs at different positions, so two append writes became one action, native prepared siblings lost their recovery identity, and client-side stream termination discarded terminal usage. Decision: every response position remains an independent occurrence through Python/native stream assembly, final parsing, execution and durable receipts. Equal names, arguments, caller metadata or provider IDs cannot establish retransmission. Only a repeat recognized within the same stable stream slot may suppress repeated transport bytes; ambiguous IDs are reminted by the common dispatch owner. Python, native providers and gateway children share this rule. Existing argument/authority checks, declared response byte/position limits, task execution budgets, cancellation and stream-completion classification continue to apply. An interrupted response cannot become complete merely because its arguments parse. Alternatives rejected: bypassing collapse only in the native gateway leaves other paths lossy; increasing the signature threshold still changes valid actions; synthesizing duplicate results invents effects and recovery evidence.

Migration: `TOFU_TOOL_CALL_FLOOD_HARD_CAP` is retired and ignored; remove it from deployment configuration. The native v1 `identical_tool_limit` field and constructor argument remain accepted with their historical validation range (2..4096, default 32), but no longer admit or reject response occurrences. Generated compatibility constants and the explicit legacy `FloodGuard` library helper remain for source compatibility; live provider parsers never invoke it. Historical `ProviderToolCallFloodError`/`ToolFlood` envelopes retain their request-scoped classification and automation fence. New responses emit neither `_flood_collapsed` nor `_flood_salvage`. Verify `test_tool_call_flood_admission.py`, `test_streaming_and_prefetch.py`, Responses translation, native `tool_policy`/`tool_assembly`/HTTP tests, and actual native file-recovery tests against both stores.

Native program parents receive a task-unique canonical ID while retaining the
provider token as diagnostics; outputs and children bind only to one active
occurrence. Client-mode custom-tool waits key by `(taskId, callId)`, never
replace an existing waiter, and accept only the first resolution across
response, timeout, and cancellation races.

**Delegated receipt replay.** Problem: cold history and Continue promoted gateway child audit rows to additional assistant actions, even though the gateway's result already contained the exact same execution receipts. Decision: the shared causal replay scanner omits an internal `execute_calls` or `execute_program` child from model projection only when its canonical `toolResultEvidence.resultId` already occurs in an accepted adapter container's `toolContent.results` in the same attempt/task/LLM round. Durable child audit, effect and checkpoint records remain; `delegated_receipt_count` exposes the projection count. Unknown scopes, compacted/unproven receipts and independent provider calls retain the prior path. A missing parent result remains a causal gap. Alternatives rejected: suppressing equal arguments would erase independent actions; replaying every execution row invents tool calls the model never emitted. Coverage state lives only for one replay scan and scales with its existing input rows. A Python 3.12 traced probe of 4,097 rows (4,096 covered children) took 46.35 ms and peaked at 1,664,765 bytes; it adds no resident cache or durable data. Derived continuation/compaction policy witnesses include the shared tool replay projection version. Old working contexts rebuild from authoritative Turns after a projection fix; they cannot bypass the corrected scanner. Durable state is preserved, and ordinary bounded checkpoint replacement retains its lifecycle.

The gateway remains protocol-only in durable activity. Its terminal payload is
decoded from sparse canonical `toolContent`: validation failures project as
warning-level skipped child rows, while an executed child's lifecycle owns its
typed V2 code, message, and next action. The wrapper is not rendered twice.

## Project-dispatch execution admission

`project_dispatch.scope.admit_tool` is the single admission of a dispatch task in
every executor: pipeline batch admission, the serial executor
(`executor/_execute.py`) and streaming prefetch
(`StreamingToolAccumulator._execute_one`). Each calls it before the
Coordinator/Delegate role policies, approval, custom tool environments,
speculative effects or handler resolution, and it never reads the catalog, so a
missing or stale catalog projection cannot admit a tool. It is default deny in
every mode (consult, files, project, coordinator): an ungranted name, any custom
tool environment, or authority that cannot be re-established (lost grant
carrier, revoked or rebound grant, unreadable record) raises
`ToolAdmissionRefused`, which settles as failed `tool_call_rejected`. Only a
cooperative Stop (`task['aborted']`) keeps its cancellation. A prefetch refusal
only defers the call; the pipeline then refuses it and fails the turn.

Problem: the serial executor ran the Coordinator role policy first. That policy
resolves the dispatch continuation and folds any lookup failure into a
model-visible "Coordinator policy unavailable" result, so a Worker whose grant
carrier was lost or revoked continued its turn under the wrong role's refusal,
a racing Stop of a Coordinator continuation became that soft result, and its
ungranted calls became soft denials. Streaming prefetch never ran dispatch
admission: with the request-owned contract documents absent it treats the call
as legacy-permissive, so a consultation (no tools) performed `fetch_url` or
`web_search` network effects before the pipeline refused the call. The files
custom-environment refusal was a plain `AbortedError`, which settles as a user
Stop. Decision: one typed boundary, called first by every executor.
Alternatives rejected: relying on catalog or contract filtering (a projection,
not authority); disabling prefetch for dispatch tasks (drops the latency win of
project Workers and leaves the serial ordering wrong); teaching the role
policies to tolerate dispatch errors (two authorities, and the soft-result path
remains). Specification:
`tests/project_mod/test_project_dispatch_executor_admission.py` (each mode with
the catalog removed: ungranted name, custom environment, removed carrier, Stop,
prefetch).

## Child process environment

Commands and MCP validate string-only, NUL-free mappings at launch and resolve
executables using the environment actually passed to the child. MCP server
configuration overrides inherited values before readiness checks; an explicit
PATH is its search boundary. Missing tools fail visibly rather than triggering
installation. Built-in commands execute the platform shell from `lib.compat`,
not the shell named in the launcher's ambient SHELL variable. Native pipelines
remain supported; approval, authenticated-download and workspace guards remain.
`lib/tools/process_environment.py` owns mapping validation, installed CLI discovery,
persistent cache defaults and resolution against the final child PATH.
`lib/mcp/stdio_environment.py` assembles the configured MCP mapping before
launcher readiness checks; `lib/project_mod/run_command.py` keeps command
credential admission and workspace Python selection. No caller mutates the
server environment or evaluates login-shell startup files.

Vault credentials selected through `credentials` reach only the child; their
exact values never reach model-visible output. `_CommandRedaction` activates at
the injection seam with the resolved `credential_env` and replaces each value
with `[redacted credential: NAME]` in the returned text, in `on_chunk`
progress, in the bounded capture before its head/tail cut, and in
`_format_run_output`, the formatter every run_command and background-command
result passes through. A per-stream redactor holds back only a trailing piece
that could still begin a value, so a value split across reads is replaced
whole. A recorded background wrapper keeps the values active in its worker's
private context, where the result is rebuilt from the record files. Problem: a
command printing `$GITHUB_TOKEN`, or a tool echoing an authenticated URL, put
the secret into tool results, progress events and transcripts. Decision:
exact-value redaction at the injection seam, before capture and truncation.
Alternatives rejected: pattern-based secret detection (heuristic: misses opaque
values, redacts non-secrets); redacting only the final text (the capture cut or
a chunk boundary leaves a fragment); a process-global registry of injected
values (redacts other commands' output and keeps plaintext for the process
lifetime). Known limit: exact values only, not encoded forms (base64,
URL-escaped).

A background command's record files hold raw output, and a result rebuilt from
them after a server restart (a command that finished or died while the server
was down, or one reattached and settled later, including its `poll` and `kill`
results) has no in-memory values. `record.json` therefore keeps `credentials`:
each selected vault entry name with an opaque write revision (a digest of the
entry's ciphertext; every write re-encrypts under a fresh IV), read at launch
before the worker resolves the values. A result rebuilt without the launching
worker re-resolves the values from the vault only while every revision is
unchanged (`resolve_recorded_run_command_env`), proving they are the injected
ones; a reattached watcher resolves once at reattachment. An entry deleted,
changed or undecryptable since launch withholds the retained output behind
`Output withheld (credential_unresolvable): …`, and a record without
`credentials` (written before they were recorded, or unrecordable at launch)
behind `Output withheld (credential_record_missing): …`; both keep the exit
status, and a vault read failure leaves the record for the next recovery sweep.
The record read redacts before its 200 KB head/tail cut and drops a fragment at
either cut that could belong to a value split by it. Problem: recovered
results delivered injected secrets into conversations. Decision: names and
revisions in the record, values re-proven at read, fail closed otherwise.
Alternatives rejected: persisting values or a reversible encryption of them (a
second secret store beside the vault); names alone (a rotated or deleted entry
leaves the old value unredacted); a digest of the value (offline-guessable for
low-entropy secrets); delivering records without evidence unredacted.
Verification: `tests/project_mod/test_run_command_credential_redaction.py`.

Command children never inherit handles to the running server's storage
instance. `_SERVER_PRIVATE_STORAGE_ENV`, declared beside the inherited-secret
sanitizer in `lib/project_mod/run_command.py`, names
`TOFU_STORAGE_CONNECTION_FILE`, `TOFU_STORAGE_TOKEN`, `TOFU_STORAGE_PARENT_PID`,
`TOFU_STORAGE_SERVICE_ID` and `TOFU_STORAGE_SERVICE_STATE`; `_get_cmd_env` and
workspace runtime preparation drop them through the same predicate. Storage
configuration stays inherited: backend selection, `TOFUDB_BIN`, fastpath, RPC
budgets and `PORT` let `debug/inspect_conversation.py` follow the configured
backend to the application's loopback API or the read-only SQLite fastpath.
Problem: with the connection file inherited, any script importing `lib.storage`
selected the live engine as its attach target, and a SQL Sidecar started from a
command would publish its own document to the live path. Decision: the set
mirrors the instance transport keys the manager already excludes from its
configuration identity (`serverctl_pkg/native_storage_host.py`); without them a
script's `StorageSupervisor()` selects a managed child, which contends for the
exclusive project lease instead of attaching. Alternatives: per-script
`env -u` unsets depend on every caller remembering them; dropping every
`TOFU_STORAGE_*`/`TOFUDB_*` name breaks backend-following diagnostics and engine
checks; the pytest scrub in `tests/conftest.py` covers only pytest. Verification:
`test_command_child_drops_live_storage_handles_but_keeps_storage_configuration`
in `tests/tools/test_process_environment.py`.

The command dispatcher passes the containing authorized root explicitly.
The nearest complete `.venv` from cwd up to that root wins, including after
sticky cwd navigation. On POSIX, an incomplete marked environment keeps shell,
Git and non-Python tools usable; Python/pip PATH launchers fail visibly with
exit 127 instead of selecting the server interpreter. Windows retains the
launch-time refusal. A server activation marker or an ancestor outside the
root cannot select Python. Restricted environment symlinks are checked before
availability handling and remain refused.

Problem: eager dependency preparation turned missing Python into failure of all commands, preventing diagnosis and repair. Decision: an unavailable source venv is represented in the private task tree without changing the source; fail-closed Python launchers live in reconstructible tool cache and disappear from selection after the task venv is repaired. An existing task environment wins over a broken source. Bridge creation errors roll back only the newly created task venv under the workspace lease, so retries do not accept partial bridges. Alternatives: silently selecting server Python breaks isolation; refusing every shell command prevents recovery; reinstalling source packages mutates shared state and hides the original fault. Recovery verification: `tests/project_mod/test_workspace_python_recovery.py`. Incomplete-venv environment assembly (100 warm local calls): median 3.73 ms, p95 4.40 ms; ten cached launchers, zero child processes or per-call files. Healthy environment selection does not scan PATH or allocate these launchers. CLI directories include uv's configured bin, `~/.local/bin`, Cargo's bin and an explicit npm prefix. Installer-created bins work in the same command and later calls. Commands and MCP reuse the existing `TOFU_MCP_CACHE_DIR` root (default `data/mcp-cache`); explicit package-manager settings win. Restricted commands use workspace-local install/cache paths. Installed tool environments are user-controlled durable artifacts, never cleared as reconnect cleanup.

Use `command -v TOOL` before reinstalling. For Python project dependencies,
create `.venv` once at the root; use `uv tool install PACKAGE` for persistent,
isolated CLI environments. Ordinary pipelines retain native shell semantics.
`export` lasts for one invocation; configuration and persistent installations
are the cross-call mechanism. The read-only diagnostic is
`python3 scripts/check_tool_environment.py --cwd PROJECT --project-root PROJECT
--launcher TOOL`; add `--kind mcp --mcp-config SERVER_JSON` for one configured
MCP server. Reports contain only path selection and executable resolution.

Problem: installation, environment selection and readiness disagreed, causing
false missing-tool reports and repeated installs. Decision: one validated child
mapping with explicit precedence and persistent discovery. Alternatives:
sourcing shell rc files introduces arbitrary startup side effects; reinstalling
on lookup failure hides PATH errors; disabling pip isolation risks the runtime.
Focused verification: `tests/tools/test_process_environment.py` plus command and MCP suites.

Environment assembly measurement (100 warm local calls): median 0.36 ms,
p95 0.40 ms, zero new files or processes without an installer. Persistent
package/tool directories are created by package managers, reused across calls,
and retained with their configured root. No extra worker, timer, automatic
installer or reconnect-time tool deletion is introduced. MCP sets
NODE_USE_ENV_PROXY, retaining explicit options without injecting unsupported
NODE_OPTIONS flags into old Node runtimes.

## Write and project boundaries

Large or binary outputs follow the producing tool's recovery policy; they are
never dumped wholesale into the next model request or logs.

`edit_file` inserts resolve one unique anchor. `replace_all` is ignored for a
unique-anchor insert and never enables multi-site insertion; only `replace`
grants batch semantics.

`replace` applies the supplied replacement text even when it retains the
anchor at either boundary, inside the text, or as a deliberate duplicate.
Redundant argument characters are not a write error: no style-only retry,
automatic conversion to line insertion, or new line breaks are introduced.
Prefer insert operations for new lines; their existing boundary-echo repairs
remain insertion-only and never rewrite replacement content. Root authority,
read/freshness checks, unique matching and atomic writes remain mandatory.
Exact replacement matches preserve existing CRLF and missing-final-newline
bytes; line insertions retain their existing universal-newline behavior.

Python syntax feedback from a unified edit batch compares each file's original
text with its final successfully written text, once per file. Intermediate
in-memory syntax errors that later edits repair do not trigger follow-up work.
Warnings remain advisory and bounded to the existing Python parser budget;
already-invalid files are not newly blamed, and failed writes produce no
success warning. Desktop edits retain their sequential per-edit commits and
use the final committed state for the same batch-level feedback.

Restricted agent/API absolute reads and writes use the same conversation/task
root registry as project-relative dispatch, including `execute_tools` children.
The trusted dispatch identity binds that registry only for the call lifetime;
a missing scoped registry denies access instead of borrowing global roots.
Targets are checked after symlink resolution, so an in-project symlink cannot
grant access outside the registered roots. A tool's requested cwd/path is not
root-registration authority; the runtime must register its project first.

Absolute-path writes outside every registered workspace root never silently
expand the workspace. All model-facing write tools refuse with
`OutsideWorkspaceError` before registry mutation. Only a reissued call carrying
`allow_outside_workspace` after explicit confirmation may register the nearest
existing ancestor and proceed. Temp scratch registers nothing; forbidden system
paths remain refused. Confirmed roots need no repeated confirmation. UI drops
remain stricter and require adding the folder first.

Same-repo worktree exemption (2026-10-04): a write into a git worktree whose
main repo IS a registered root (or whose repo shares its common dir with one)
is a bounded expansion of the current project, not a silent workspace
mutation. It proceeds like a confirmed write — conv-scoped registration,
root-added signal — without requiring `allow_outside_workspace`. A non-git
directory or an unrelated repository still requires explicit confirmation.

Project identity survives directory renames. `GET /api/v1/project/recent`
reports disk liveness. `POST /api/v1/project/recent/relink` validates the new
path and rekeys owner aggregates in one `project.relink` storage command. It
merges recent counts, migrates active and recoverable conversation project
pins, and atomically rekeys the signal-driven Project Brain projection plus
its retained event tail.
`POST /api/v1/project/git-root-hint`
finds the nearest enclosing `.git`
directory or gitfile for subdirectory staging.

## Bounded result envelopes

`tools.resultEnvelope` ships as `v2`. `ToolResultEnvelopeV2` is the internal
budget/evidence record: stable status, summary, at most 64 structured items,
cursor, truncation, byte counts, freshness/evidence ID, and typed recovery. It
is not copied wholesale into provider tool messages.

Problem: a normally returned error could lose its execution verdict when
serialized, leaving a successful card/cache/audit despite failed canonical
evidence. Decision: host-generated typed errors carry failure metadata through
both serializers; the shared executor and settlement consume that metadata.
MCP's protocol error flag takes precedence over prose prefixes. Successful
responses containing error examples remain data. A failed batch child or an
incomplete source/view does not fail a successfully completed outer operation.
Alternatives rejected: parsing arbitrary result JSON or matching message text
would misclassify documents; changing only a display adapter would leave direct
consumers and canonical receipts inconsistent.
Redaction preserves a producer envelope only while its complete source remains
that same valid envelope with the same status. Independent raw sources never
inherit a view's envelope authority. Typed error code, retryability and recovery
advice survive budgeting without repeating the error inside generic items.
Verify `tests/tasks_pkg/test_tool_outcome_finality.py` and
`tests/tools/test_canonical_tool_result_pipeline.py`.

Settlement splits the record once. Complete text remains text; complete structured success uses its compact JSON value; partial/error results retain only actionable non-empty fields. Constant recovery plumbing (`observation: "new"`, `historicalRecovery: false`, the single-valued `kind: "source"`) stays server-side and is re-inflated from class defaults when the sparse projection is read back; so are source-recovery arguments that would merely restate the still-adjacent tool call — only genuinely narrowed replay queries (`read_files` slices) project `recovery.arguments`, and the constant `cursor: "0"` never projects at all. The bounded `v1` sidecar carries evidence outside provider messages. Artifact-policy tools expose at most 8,000 tokens and store recoverable overflow in the owner-scoped content-addressed repository. Source-policy tools may use the otherwise-idle 24,000-token round allowance and never copy overflow into that repository; the aggregate sibling ceiling stays 24,000 tokens.

`search_files` source recovery states exactly what was withheld. A capped or
char-limited result ends with a bounded two-line trailer naming the fired
limit and the per-file remainder distribution (counts and line ranges from a
short post-cap drain; index-path skipped chunks get an exact bounded count
pass; hitting the counting ceilings marks the total as a `+` lower bound).
Recovery is `path`/`include` narrowing when the remainder spreads across
files and the `from_line` per-file line floor for paging one file — a
line-number cursor like the `read_files` range, never a match ordinal. A
drain that reaches the backend's natural EOF proves the page complete, so an
exactly cap-sized result is not marked truncated. Path-printing searches run
with `--null`, so a path always ends at a NUL byte, and single-file searches
print bare `lineno:` records. Line numbers, the `from_line` floor, match
counts and per-file grouping therefore never depend on a `:` or `-N-` run
inside a path or the content (log timestamps, dated file names).

`read_files` source recovery preserves requested identities, shares preview
space fairly, retains at most 20 public reads, and narrows replay to a shared
600-line window.
 Reversed line bounds are swapped before narrowing, mirroring
the read handler's repair, so replay stays inside the range that was actually
read.
 `none` recovery produces a bounded preview with no
recoverability claim. The 8,000-token decision uses final model projection and
exact visible bytes. Shortened success is `partial`, never `ok` plus truncation;
producer-side omissions remain authoritative.

Recovery tools stay eagerly callable whenever an artifact reference can be
emitted. Internal envelopes live only through the current aggregate-budget
pass or bounded dedup receipt. Reconstructible overflow expires after 24 hours
by default, with a seven-day hard ceiling and bounded reclamation. Storage
failure returns an honest preview and narrower-rerun hint without an unusable
artifact reference. Paper adapters default to V2; the explicit legacy arm is
fingerprint-isolated from live tasks.

Explicit `read_files` line bounds remain authoritative for project, absolute,
and uploaded text paths; small files are never widened. Bounds are positive and
1-based, and malformed values fail visibly.

## Timeouts and loop guards

Timeout attribution is exact. The ordinary pool ceiling (`TOOL_PARALLEL_TIMEOUT`,
default 300 s) bounds each parallel occurrence from submission. Its collection
deadline is the larger of that ceiling and the tool's declared timeout.
Problem: a 300 s batch deadline cut off `await_project_workers` before its
normal 1800 s wait result; its subsequent 1805 s timeout still expired during
result collection. Decision: collect with independent deadlines and derive a
bounded wait's execution deadline from the wait argument's JSON-schema
maximum plus `TOOL_WAIT_COMPLETION_ALLOWANCE_MS` (60 s), the liveness budget
for result collection and handler unwinding, instead of maintaining separate
per-tool timeout constants. The schema declares defaults too: omitted
`await_project_workers.timeout_seconds` waits 1800 s, while omitted `poll.wait_s`
remains an instant snapshot. Per-call values select their own derived bound.
The same rule covers command, task and swarm waits through `poll` and the
compatibility `await_agents` and `check_background_command` surfaces. A
registration wait is not synchronous (`await_device`), and an approval-policy
TTL is not a wait (`set_approval_delegate`); neither gains this allowance.
An expired occurrence requests its own runtime cancellation immediately and
closes result publication; longer siblings continue and retained receipts
stay unchanged. Worker waits and poll waits observe the same per-call
cancellation through their existing interruption loops and unsubscribe their
notifications. This abandons only the wait: no Worker, swarm agent or
background command is restarted or cancelled by its timeout.
Alternatives rejected: reducing the advertised wait would break the one-long-
wait contract; another fixed per-tool deadline can again drift from its schema;
one enlarged batch deadline prolongs unrelated hung tools; a human-wait
exemption removes finite timeout and reaper safeguards. Verification:
`tests/tasks_pkg/test_tool_parallel_deadlines.py`,
`tests/tools/test_tool_declared_timeout.py` and the Coordinator worker suites.
`poll`'s reaper-safe wait of up to 1500 s remains below the stuck-task silence
threshold (`reaper_safe_wait_s`, normally 300 s of margin), and returns on
completion, waiting input or its cap ([cost decision](../LLM_COST_OPTIMIZATION.md#long-waits-and-the-conversation-tail-ttl)).

**Command batches.** Problem: a multi-command `poll` waited serially, so a
running command could hide a completed sibling until the entire wait cap
expired. Decision: observe all command handles before waiting and between the
existing interruptible wait slices. `mode=any` (the default) returns when any
command completes; `mode=all` waits for all, under one shared deadline. Each
observation retains user/task ownership checks, and missing handles are
reported as unavailable rather than replaced by stale snapshots. Cancellation
and pending input end the wait without stopping commands. Alternatives
rejected: shortening the cap increases model calls while useful work runs;
waiting serially preserves the ordering defect. The controlled-clock regression
in `tests/tasks_pkg/test_poll_command_batch.py` changes a completed sibling's
delay from the requested 1500 s to 0 s, and observes a sibling finishing during
the wait within the existing one-second slice. These are wait semantics, not
a claim that the underlying command itself runs faster.

`ToolSpec.timeout_ms_by_name` declares a tool's own deadline: on expiry the
call's runtime context is asked to cancel cooperatively, and only that timer's
own firing replaces the late result with the canonical
`Tool execution timed out: <name>` sentinel, an `error` receipt status, and a
`_declaredTimeoutMs` attribution marker. An outer user abort or the pool
ceiling never produces the declared-timeout marker, and ratified human-wait
exemptions (`_SERIAL_BLOCKING_TOOLS`) never arm one. Declarations are
validated at registration (names ⊆ `provides`; integer within
[1 s, 3600 s]) — a bad declaration fails loud at load, not mid-task.

Loop guards correct before they stop, and never in the same round: a round
in which any guard issued a correction never also stops, so the model always
answers the newest correction. A human steer resets the exact-repetition
chain because it measured an unattended loop the operator has already
redirected (`reset_tool_loop_guard`, called at the steer-drain boundary).
The same reset retires redundant-read and unresolved-artifact warnings,
because the new instruction can require re-reading; a later stop first needs
a fresh correction. Visible read evidence, stagnation streaks and the
explicit-noop history survive the steer. The remind-only rung
(`tool_repeat_reminder`) fires once per repeat digest one step below the
corrective nudge: it never vetoes, advances
no escalation counter, and audits on its own `_toolLoopReminders` ledger so
it cannot consume the one-correction-per-turn slot the nudge/stop escalation
and stagnation detectors read.


### Conditional Git guard rejection metadata

The Git guard's pre-execution refusal stamps `rejection.kind = git_guard`,
`condition = competing_workspace_writers_settled_or_acknowledged`, and
`retryPolicy = {mode: after_condition_change, automatic: false}`. The existing
`retryable: true` means the operation can be reconsidered after its condition
changes, not that any automatic continuation may immediately replay it. This
metadata grants no execution authority and changes neither acknowledgement
receipts nor execution leases. Finalization preserves the descriptor in the
failed Turn error; stall recovery and swarm continuation consume this evidence
as specified in `TURN_SETTLEMENT.md` rather than interpreting refusal prose.


Decision: command progress timing uses the task execution clock: the spawn callback maps the effective timeout budget into that clock's epoch anchor; progress captures `execTimingAtMs`, `execElapsedMs` and optional `deadlineRemainingMs` before observation. These are samples, not a continuously updated stopwatch. The UI formats the latest backend sample and never advances it with its own clock. Missing samples keep ordinary running status without fabricated time. This reuses existing progress frames/coalescing and adds no polling request, thread or queue; existing event byte budgets include the extra numeric fields. Problem: browser/wall-clock jumps changed command countdown and elapsed time. Alternative rejected: correcting a client stopwatch still estimates unobserved execution and creates a second timing authority. Backend sampling keeps the actual spawn boundary, effective timeout and observer delay distinguishable. Local measurement (`benchmarks/execution_clock.py`, Python 3.12.13, 100,000 samples × five): sampling/update median 921.4 ns; all three compact numeric fields add 79 bytes to a compact progress-event JSON envelope. This is producer overhead, not a bound on model/subprocess time. The command's 1 Hz browser ticker and DOM scan are removed; watcher scheduling countdowns retain their existing owner.

## Unified result surfaces and format policy

**Problem:** canonical capture, model delivery and frontend metadata existed, but debugging required separate source searches. Reports were often JSON strings containing escaped newlines; a field mismatch in async completion cards could hide the report while the model received it. **Decision:** `contracts/tool_result_surfaces_v1.json` owns result source precedence, model/frontend format policy, notification report tags, and bounded audit limits. `scripts/gen_tool_result_surfaces.py` derives the Python and TypeScript policy; `npm run check:tool-result-surfaces` gates drift. Unknown built-in, custom and MCP tools retain their producer output by default. `lib/tools/result_surfaces.py` exposes `inspect_tool_round`, `inspect_swarm_notification`, and `inspect_result_document` over existing Turn projections. No parallel result log, result table, transport, or replay owner is introduced. Frontend source resolution uses the same generated contract and honors explicit empty/null results; secondary `results` metadata never becomes proof of model delivery. The observer reports renderer inputs, not mounted HTML or proof of provider consumption. Repeated call IDs retain their Turn indices; unread history, missing evidence, deferred previews and diagnostic truncation remain explicit. Artifact reads use existing owner-scoped continuation APIs.

`get_agent_result` and only recognized agent-report portions of `poll` use an
opt-in text view before ordinary budgeting. All identities, outer and agent
statuses, errors, partial output and extra fields survive. Unsupported shapes,
opaque command/task output and typed errors remain unchanged. Authorized
formatted reports retain their original canonical JSON in the existing bounded
artifact repository; references remain in the evidence sidecar, without adding
plumbing to a complete model response. A storage-free/ownerless host retains
runtime canonical values without claiming durable retention. Programmatic
`tools.invoke` and result reuse consume original canonical values, not text
views. Frontend report cards render safe Markdown; JSON errors keep the generic
verbatim viewer. XML completion reports share their current/historical tags
with the same authority.

**Report evidence fidelity.** Problem: a separate ordinary JSON parse rounded
report metadata, converted finite exponent tokens to infinity or zero, and
silently selected one value from duplicate keys. The poll line formatter could
also rewrite a nested report inside an otherwise opaque error/array document.
Decision: report projection shares the numeric-token-preserving JSON parser and
serializer used by native artifact/gateway views. Only actual JSON strings
qualify as report text; numeric fields and unknown nested values preserve their
validated tokens. Duplicate keys, nonfinite constants and unsupported/deep shapes
retain the original source. Whole JSON objects/arrays that do not qualify are
never scanned as independent poll sections; native poll headers still delimit
one-line report receipts. Canonical retention and output-budget authority stay
unchanged. Alternatives rejected: float/Decimal normalization changes evidence;
dropping ambiguous fields hides source defects; formatting arbitrary nested lines
can erase error structure. Verify `tests/tools/test_agent_report_fidelity.py`,
including canonical retention and the sealed OpenAI, Anthropic and Responses
request bodies. These are offline request-shape checks, not provider execution.

A 2026-10-21 Python 3.12.13 local comparison kept ordinary report views
byte-identical. Seven alternating 200-call samples measured single-report
projection at 14.74 / 20.67 microseconds and a 16-report batch at 208.71 / 278.44
microseconds (before / after). Peak traced temporary allocation for the batch
was 219,289 / 223,497 bytes. This is the measured cost of preserving numeric
tokens, not a request latency, RSS or billed-token claim. Projection adds no
cache, retained history, thread or durable record owner.

**Alternatives rejected:** returning every internal field wastes context;
flattening every JSON result can destroy cursors, shapes and machine-readable
semantics; persisting another debug copy creates a second lifecycle and source
of truth. Audit findings are advisory: many or empty JSON fields require review,
not automatic deletion. Reports embedded in JSON and leaked internal envelopes
are independently identified. The inspector runs only on demand, scans bounded
JSON and caps records and previews. No model call, timer or background worker is
added. Tests: `tests/tools/test_result_surfaces.py`,
`tests/diagnostics/test_inspect_conversation_api.py`, and
`tests/visual/test_visual_tool_result_surfaces.py`.

### Local MCP file paths

`contracts/mcp_local_paths_v1.schema.json` owns the opt-in `tofu/local-paths` metadata
and maintained exact adapters for xuecheng `prepare_doc_edit.out_path` and `update_doc.from_file`.
Only connected stdio servers share host workspace paths; network servers and desktop files cannot claim this mapping.
Unrelated parameters remain opaque: names such as `path` or `out_path` alone confer no policy.
Declared files use admitted stable roots and canonical-to-execution mappings; all targets are checked before side effects.
Unknown roots, traversal outside roots, escaping symlinks, readonly outputs and canonical/peer writes refuse invocation.
Refusals name the effective target: an outside target lists the admitted roots (read-only marked) and the relative or rootname:path forms; an escaping symlink reports where it resolves.
Inputs must exist. Declared output parents are prepared only within authorized roots; no workspace lease is taken or awaited.
Transport cancellation is forwarded and ambiguous calls are not automatically replayed; a call cancelled before invocation is refused.
Receipts retain error, structured and media payloads and report effective absolute paths; transport failures never claim not-started.
Path routing grants no remote publication approval or new root, and does not sandbox the external server process.
External file writes retain their server durability/undo contract; directory preparation is not native file-history publication.
Problem: relative native writes and absolute MCP outputs could select different repositories.
Decision: deterministic primary/rootname paths and declared local-file admission shared by direct/progressive calls.
Alternatives rejected: guessing path fields or following file existence changes tool intent and server semantics.

## Search observation quality

**Search observation decision:** Problem—wrong-topic snippets, circuit-open engines and missing article tables could look like successful research. Decision—`tofu-search` 0.11 separates fetch admission/ranking from observation quality, enforces explicit site scopes before any fetch, retains at most three uncertain snippet candidates in diagnostics, and reports failed/unavailable engines even with nonempty results. The full bounded uncertain set enters the existing result artifact, preserving follow-up recovery without flooding the preview or creating another store. Received DOM tables and omitted copy identifiers supplement article extraction. `lib/search/search_extract_contract.py::EXTRACTION_SCHEMA` owns the helper response: findings require exact inspected-source quotes, deterministic source offsets, and separate unresolved requests. The bounded single helper pass retains original source; invalid/ungrounded output returns raw evidence through the existing fail-soft handler. Full received text and exact quotes prove neither page/website completeness nor semantic entailment. Alternatives rejected: zero-overlap hard rejection loses aliases/languages; treating fetch success as relevance invents verification; another adjudicator model increases cost without recovering lost source. Package/host tests exercise degraded engines, scope violations, late facts, missing fields, false citations and raw-source fallback.

**Automatic evidence admission — Problem:** a generic one-word match could stand in for a substantive query, fetched text could silently upgrade that match, and one-hop links could escape the original site scope. **Decision:** subject terms exclude search operators; partial lexical matches remain explicitly uncertain. Reassess received article text without counting outbound link lists as subject evidence. Bounded recovery uses the original query and freshness when no useful candidates arrive, even if an engine returns unrelated rows; explicit engine selection remains authoritative. Site scopes apply to initial rows and deeper links. Deep crawling uses the remaining search deadline, and uncertain parents cannot amplify junk. Model output preserves source identity, available dates/ranks, uncertainty and coverage; snippets and page excerpts share an explicit presentation budget when one is supplied. Tofu host search automatically uses one 20,000-character content budget across the whole tool call, including batch queries; metadata and vertical results remain visible. The shared presentation owner captures complete received results, uncertain candidates, diagnostics and vertical data before selecting any preview. Serial and streaming cache paths preserve that canonical capture for artifact continuation. Direct-fetch behavior remains unchanged. **Alternatives rejected:** making Deep fetch more junk does not improve precision; strict all-keyword rejection loses valid aliases and multilingual recall; silently broadening queries breaks constraints; buying a new provider or weakening TLS/SSRF is neither necessary nor authorized. This adds no settings, cache or resident pool.

Offline degraded-SERP measurement (20 runs, mocked engine/fetch, no model or
network): 20 generic candidates incurred 20 page fetches before admission changes
and 3 afterward (85% fewer), with no junk promoted into ranked evidence. Median
local pipeline time was 4.750 ms before and 12.482 ms after: explainable evidence
checks add CPU work; this is not a demonstrated end-to-end latency improvement.
A separate 3,000-character payload fixture reduced bounded snippet-only output
from 100,113 to 4,629 total characters; source identity and metadata stay outside
the payload budget. No new resident concurrency or cache is introduced.

Resource measurement: a synthetic 1,000-row two-column table (39,887 HTML
characters) produced 42,677 extracted characters (1.07x), took 0.6664 seconds
with tracemalloc enabled, and peaked at 3,663,523 traced bytes on the verification
host. This measures one received-DOM extraction, not network/LLM latency. No
resident cache/concurrency is added; original fetch byte/deadline, expanded-text,
artifact quota and lifetime bounds remain authoritative.


**Fixed search defaults — Problem:** search had a host tuning layer: four
profiles (fast/balanced/deep/custom), six numeric limits, an LLM relevance-gate
toggle and link deepening, saved in `server_config.json`, shown in Settings and
writable through `update_search_settings`. Under the saved deep profile every
non-`site:` `web_search` in two days of production logs (6 of 6) ended at the
45 s deadline. The gate judged 5 of 7 explicitly requested official
documentation pages irrelevant, and it cost about eleven side-model calls per
search (5,455 input characters each on average) for verdicts the lexical ranker
already provides. Deepening fetched 0 of 4 deeper pages in the logged runs.
**Decision:** search and fetch limits are tofu-search's own defaults; the host
no longer re-declares them, and it keeps the gate off. The fetch blocklist and
the optional search gateway are the only saved search settings. Settings,
the server-config route and `update_search_settings` accept only those: the
route drops retired keys from older clients, and the tool rejects them without
writing. Operators keep the package's `FETCH_*` environment overrides.
With the gate off, `fetch_url` no longer describes it or asks for a relevance
`reason`; its schema shrank from 280 to 217 tokens (o200k) on every request
that offers it.
**Alternatives rejected:** choosing a better default profile keeps four presets
and their controls for one value set; an opt-in gate toggle keeps a control
that measurably removed requested evidence; deleting the gate from the package
removes a feature that standalone callers configure.

Live measurement (16 queries in two phases separated by the 120 s breaker
cooldown, one host, network variance applies): with the deep profile and gate,
the steady-state phase took 17.9 s median (19.6 s mean) and made 87 gate calls
for 8 searches; with fixed defaults it took 4.5 s median (6.2 s mean), made no
gate calls, found the authoritative source for 8 of 8 queries as before, and
delivered about 10% fewer model-visible characters per search. The engine-wave
change in `packages/tofu-search/docs/free-search.md` ("Waiting only for useful
work") alone moved the median to 7.1 s.

**Canonical preview retention — Problem:** representation differences caused false
missing-source hints, while small omitted originals lacked a MODEL recovery ref.
**Decision:** host-only `source_omitted` defaults false; genuine preview producers
opt in. Authenticated non-private omissions retain originals and expose concrete
MODEL refs regardless of size. Lossless `projection_normalized` and duplicate MCP
representations retain canonical evidence separately without prompting recovery.
Application fields grant no provenance; failed retention advertises no ref.
Private child, owner, quota and release rules stay intact. **Alternative rejected:**
full-source comparisons confuse formatting with omission. Tests: `tests/tools/test_hidden_source_provenance.py`.
## Declared shell semantics

Problem: request-owned terminal tools were classified as confirmed writes solely
because their concurrency partition was writable, hiding no-op execution loops.
Decision: `ToolContractV2.shell_command_argument` names a declared string argument;
its private document field is `shellCommandArgument`. Custom-tool callers set
`execution.shell_command_argument`; validation and compilation preserve it through
both explicit and registry catalogs. This metadata grants no execution authority.
The generic progress guard treats these tools like built-in shells: observation,
opaque execution, and confirmed file mutation remain distinct. Exit receipts accept
the built-in terminal marker or a leading `exit_code=N` line. Changed outcomes reset
failure streaks. Alternatives rejected: name/description guessing and declaring all
successful shell calls to be mutations. Neither survives tool renaming or wrappers.


Canonical artifact traversal, signed continuation and query lifecycle follow
[Tool result queries](TOOL_RESULT_QUERY.md). <!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->
