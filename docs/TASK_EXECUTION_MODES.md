# Task execution modes

This guide owns autonomous and collaboration-mode policy referenced by the
[task-engine domain map](modules/task_engine.md). Delegated tools share parent discovery/initial visibility and exact scoped authority; see [startup context](CONTEXT_INJECTIONS.md#delegated-startup-context).

## Autonomous drivers

Flow-backed chat stores visible role messages as explicit related turns. An
accepted Goal turn starts one durable GoalRun; its worker ⇄ virtual-user Flow
owns all iterations and persists terminal meaning before chat completion.
Goal/Autopilot execution pins every leaf to the conversation's selected model;
its internal role tiers may shape the graph but cannot silently select another
model. Non-Goal Studio flows retain explicit role-tier routing.

Chat Goal workers retain normal-chat context continuity: the launch's API-ready messages (including multimodal content and tool results), followed by their actual model/tool rounds and each virtual-user review. They do not reconstruct history from handoffs or the lossy 48,000-character verifier seed. The ordinary context composer and compaction pipeline bound one current working set; a compacted prefix is never restored from the original launch on the next iteration. Accepted checklist state survives worker iterations. The worker uses the normal chat prompt rather than the delegated-worker persona or a forced first mutation. Its parent-granted tool catalog, Tool Search/execute gateway metadata and configured compaction policy are retained, including delegation controls. The worker is still unattended: `ask_human` is unavailable, required approvals fail closed, and child execution never acquires root settlement or inbox ownership.

The run's authoritative objective is re-stamped every iteration as the registered `goal_objective` composer block, verbatim and byte-identical across the run (inline up to 4,000 chars; larger objectives stay history-carried). Compaction therefore cannot drift the objective the worker judges scope against (the historical "Hole A" re-derivation hazard), and the stable block keeps provider prefix caching at ordinary-chat parity: each iteration's request begins with the previous iteration's exact working-set bytes, with reviews and engine directives appended only at the tail. The governing policy directive forbids scope shrink: completion means the complete original scope is true and verified, never the largest subset that fits the moment.

The verifier remains independent: typed I/O supplies the fixed original launch
context and full latest worker output, plus engine progress evidence. Neither
verifier tools nor its private reasoning enter the worker's history; its review
is explicitly synthetic and cannot grant new human authority. Engine directives
still reach the worker without duplicating its old output. Durable transcripts
remain intact. Working-set state is run-local and released on every graph exit;
child identity, cancellation, evidence and Goal settlement remain separate from
this input policy. Non-Goal Studio flows retain authored handoff semantics.

Executable coverage: `tests/goals/test_goal_worker_context.py` runs repeated worker
and verifier model/tool calls, retains image/policy/tool evidence, exercises real
L1 compaction without restoring old receipts, preserves checklist snapshots and
checks exit cleanup/64-iteration working-set bounds. Child/runner tests cover
parent-granted discovery/delegation, detached policy and unavailable approvals.

Each leaf resolves that route once before dispatch and carries `modelRoute`
(`selectedModel`, `resolvedModel`, `role`, `tier`, `kind`) through live phases
and the durable visible Turn; a changed route is disclosed before the model
call and remains visible in the settled footer.

Goal Mode never stops on budget alone. The loop budget is a checkpoint **window** (default 40, ceiling 64 per window, owner-overridable via `autopilotMaxIterations`), not a hard total: when a window closes without a verdict, a window that produced durable state changes or verified progress is extended automatically and surfaced as a live checkpoint event; only a full window with ZERO of both ends the loop as `stalled` — the evidence-based stall breaker, mapped to `blocked/no_verified_progress`. The verifier may also declare a genuine impasse with `[VERDICT: BLOCKED]`; the claim passes the same style of backend gates as completion (parseable progress receipt, `remaining>0`, concrete justification, one challenge) before the loop ends as `vu_blocked`, mapped to `blocked/verifier_declared_blocked`. Both outcomes render as "stopped early — needs review", never as success. Generic (non-Goal) loops keep the finite `max_iterations` bounded policy.

### Objective provenance and verified progress

**Problem:** `role=user` can carry proactive/synthetic input; tool names prove attempts, not mutations. Neither can authorize new objectives/progress windows. **Decision:** command ingress stamps objectives only from real human inputs or trusted continuation authority. Legacy unstamped human inputs remain eligible; explicit initiator aliases must all be canonical `human`. Conflicting, malformed or synthetic provenance fails closed. Idle continuation uses owner-scoped canonical history, not model projections that can drop origin or merge prompts. Structured settled tool rounds take precedence over names-only telemetry. Mutation credit needs successful native receipts plus non-identity creation, nonempty append or a single nontrivial edit. Creation uses executor prefixes, not UI badges. Failures/no-ops/reads receive no credit. Unproven overwrites, aggregate edits and shell/code calls remain **unverified**: cumulative `fileChanges` and observation `worldVersionAfter` do not prove per-call changes, even on exit zero. The legacy `exploratory` bucket includes unknown effects, not proof of no external mutation. Verifier receipts can establish non-mutating progress. Missing/regressing receipts do not reset the resolved high-water mark; only a full window without advance stalls. Verified work earns further windows without a lifetime iteration cap. **Alternatives rejected:** tool-name/command-text heuristics, cumulative mutation credit, and arbitrary lifetime budgets. Specs: `test_goal_objective_provenance.py`, `test_goal_objective_command_boundary.py`, `test_goal_budget_checkpoint.py` and `test_orchestration_tool_usage.py`.

A VU stop verdict passes a backend acceptance gate before the loop ends. A missing or positive `[PROGRESS:]` receipt is rejected outright, and the first stop is challenged once when the producer changed real state while the verifier used no tools of its own, or when close-out is entirely vacuous. The concluded run's fold report comes from the sanitized terminal VU verdict: the settings fold record carries a bounded excerpt (2,000 chars) while the complete report rides the durable GoalRun transition event and is fetched on demand (`GET /api/v4/chat/autopilot/report?convId&runId`, owner- and conversation-scoped) when the record marks `hasFullReport`. Idle continuation creates a turn pair; busy arming queues it behind the owner-scoped lane; a newer human turn atomically retires or preempts it. VU events never tunnel into a settled parent. Other dispatch is owner-bound.

The standalone `tasks_pkg.autopilot` interpreter has no production callers.
Task finalization cannot launch its VU carrier, and `ask_human` cannot substitute
a generated answer for human input. Already-constructed kickoff carriers settle
with `legacy_autopilot_retired` without model work. Historical helper import
compatibility remains; current controls, close-out and compaction pins belong
to `lib/goal_runs/`. Close-out resolvers prefer owner-scoped settings metadata, inspect a
bounded 128-message tail, and load a full transcript only after an authoritative
unloaded-prefix miss. New behavior belongs in `lib/goal_runs/` or the shared
Flow chat boundary.

Every unattended loop needs a durable command id, owner-scoped lane decision,
a finite authorized budget at every moment (a window that only evidence
extends), honest attribution, and terminal executor-start failure.

Application-managed transient tasks bind an owner/task-scoped `TerminalDelivery` after the application's launch commit. Result delivery is required before terminal state is released; a failed acknowledgement retains the result behind the existing maintenance retry fence. Receivers acknowledge idempotently using their durable task binding and stage receipt. Plain embedded tasks remain storage-free and do not notify durable application observers. Evolution execution/review and coordinator tasks use this boundary; research tasks use the ordinary post-commit observer and the same run transition. Problem: transient storage policy previously suppressed workflow completion. Decision: persistence and required result delivery are independent contracts. Alternative rejected: notifying every global observer for every transient task would couple embedded execution to application stores. Periodic reconciliation remains necessary for missed observations and process loss.

## Shared invocation execution

SDK native children borrow the transient parent's host/loop/callback profile, issue independent owner/tenant routes through its explicit repository and retain the same tree budget and child ingress. Problem: global repository lookup and conversation cache fallback crossed the storage-free boundary. Decision: use trusted dependency injection and the exact child cache namespace. Alternatives: sharing a parent route lease breaks disposal; another loop/account duplicates execution authority. Fatal children deliver through their required receiver and retain lost-ACK debt without root result writes. Real parent/child execution reports 15 tokens once; cancelled unknown child usage fences a finite shared budget. Priced descendants use the same child ports and transfer original quotes directly to the owned root wallet. Application-native Flow roles inherit these ports from the owned graph parent; graph completion validates the closed tree and its original child quotes before committing the terminal Turn. Problem: Flow excluded native binding, while sanitized phase summaries lose billing provenance. Decision: bind before dispatch and settle the original root invoice. Alternatives: reconstructing prices from phase summaries loses provenance; borrowing the standalone root checkpoint creates competing recovery authority. Flow retains its graph journal and no standalone native root checkpoint. Graph parallel/verifier policy migration and production-default promotion remain pending.


Optional flow-abi composition makes Rust own the flattened graph cursor and runtime topology queries. NativeRootConfiguration borrows an explicitly pinned NativeFlowLibrary; request config cannot select a library. Definition IDs map to immutable native indices, and every cursor requires completion of its pending action before another role can be yielded. Rust checks linear, branch, loop-exit and barrier successors; abort is observed before any effect callback. The existing role, parallel demand executor, verifier policy, human gate and execution journal retain their effect/recovery owners. Problem: Python still interpreted every edge. Decision: native control traversal with direct joined C ABI calls and existing callbacks. Alternatives: a process per edge adds transport/startup cost; a second journal duplicates recovery authority; raw pointers admit stale/free races. Scopes close on success, suspension, cancellation and failure; code unload waits for accepted calls and all graph borrowers. Equal-distance barrier ties follow stable definition order in both implementations. The legacy Python walker remains for unselected composition; selected native failures never fall back. Whole-system performance/default admission is pending: eight interleaved HTTP samples per arm measured native/Python median 1.027, with polling, provider and storage time included. That small cohort does not prove a p99 gate or a native performance advantage.


Native role overrides and SDK child declarations resolve exact logical IDs from the parent's current owner/tenant repository. The request-scoped route compiler normalizes once, refuses ambiguous creator identities, and checks authorized candidates before secret access or child allocation. Each child still mints its own route. Role-tier selection uses the durable provider preference and principal tenant, never an ephemeral slot-group ID. Problem: copying the parent ModelRef and requiring equal native selections excluded heterogeneous roles. Decision: resolve through the same canonical compiler and validate the live account actor. Alternatives: wire/display-name lookup guesses identity; copying a route lease breaks child disposal; global repository lookup crosses the owner boundary. Journal trees require explicit checkpoint_child_routes on the original root, including switches made by deeper descendants. Optional child route metadata retains the producer's model/provider and original quote across recovery; legacy same-route rows remain readable. Old artifacts refuse the new capability before model/tool work. A real root/child/grandchild SIGKILL fixture preserves two 7-credit beta quotes, adds one 28-credit root call and settles 70 credits once.


Chat enters `execute_invocation` directly. Goal graph roles and Swarm's
`SubAgent` scheduling adapter both use `create_child_invocation →
run_child_invocation → execute_invocation`. The same task pipeline owns
preparation, model/tool rounds, post-tool state, compaction, retries and terminal
evidence. Swarm has no private request builder, model dispatch, tool pool or
wrap-up model call. Its adapter owns role selection, artifact handlers, presence,
checkpoints and bounded panel projections.


### Common machinery, explicit mode policy

**Problem:** specialized reports and autonomous modes can appear to require separate agent loops, streaming phases and conversation renderers. Copying those owners makes fixes diverge across modes and increases maintenance cost. **Decision:** Chat, Coordinator and children use the existing invocation pipeline, context/admission, tool discovery/dispatch and canonical Turn projection. Tool adapters supply scoped catalogs and policy to shared preparation, never a role-specific search-off or all-schema path. Modes select prompts, capabilities, actors and graph paths. Human input and synthetic verifier input may both be user-shaped messages, but only actual human/trusted continuation provenance grants authority. Presentation is not execution eligibility. Evolution and its coordinator share ordinary task launch and required terminal delivery; adapters retain domain bindings. Child delegation/approval limits, parent cancellation and terminal ownership remain explicit policies; so do Goal verification, evolution learning gates, coordinator wake ownership and durable settlement. **Alternatives rejected:** a new universal workflow engine would duplicate the existing execution owner; flattening all modes into chat would discard distinct approval, verification and settlement semantics; text-based deduplication would hide evidence instead of resolving which Turn/block owns it.


Virtual-user terminal text is normalized before the ordinary output seal on
both storage backends. `lib/agent_verdict/_handoff.py` owns machine-token rules;
`gen_agent_machine_tokens.py` derives browser/native consumers. Sanitation is
not a new answer: preserve its terminal block identity, tool receipts, reasoning
and distinct historical narration. A genuinely changed answer remains a separate
occurrence. Historical native mirrors are presentation-only: select the canonical
completion only when the superseded terminal owner differs solely by registered
machine tokens; never rewrite durable history or apply fuzzy prose deduplication.
### Origin-neutral persistence and retry lifecycle

Cross-entry ownership and supported recovery boundaries: [Agent lifecycle](AGENT_LIFECYCLE.md).
**Problem.** Shared model/tool execution still allowed a required Swarm checkpoint
or lineage registration to turn `database_busy` into a terminal agent failure;
a scheduler retry could then recreate the agent and repeat completed effects.
**Decision.** `tasks_pkg/persistence_wait.py` owns required persistence waiting
for every agent. Creator identity is lineage, not a resilience policy. The same
invocation scope, retry decision, effect checkpoints, accounting and terminal
event builder apply to human-created and agent-created executions. Presentation
and durable domain ports choose an evidence destination, not a second runtime.
Ownership, scoped tools, inherited cancellation and attempt/CAS fences remain.

A classified busy/unavailable/timeout response retains the exact receipt and
payload (or an idempotent checkpoint) and waits with capped backoff. Individual
RPCs still have deadlines; the agent has no transient-storage attempt ceiling.
Registration must ACK before dispatch; model/tool intent and completed-round
checkpoints drain accepted event evidence before the next paid/external effect.
Stop and explicit execution deadlines interrupt effect admission. Accepted
Inspector/event/terminal evidence still persists after Stop. Shutdown returns
unacknowledged debt to existing recovery owners; permanent errors and authority
revocation fail closed. No backend switch or replay of unknown effects is allowed.

Known storage waiting uses the existing `retrying` phase and a task-local witness.
The shared liveness beacon recognizes that witness without claiming model/tool
progress; ACK retires it. Root reaping and session TTL cannot cancel a proven
persistence wait. Waiting creates no extra pool, worker, journal or transcript
copy. Witness count follows concurrent writers and is independent of retry count.

Selected-route provider retries have no delegated-only 429 ceiling. An omitted
`max_retries` inherits the common `autoTurnRetryMax`; explicit values retain their
meaning, including zero. The scheduler does not restart invocation-owned agents.
The common whole-invocation retry is eligible only before tool admission: a reset
must never rerun a completed or unknown external effect. Storage repair never
purchases another provider request. **Alternatives rejected:** a larger private
retry ceiling still eventually fails healthy work; restarting an agent discards
receipts and can duplicate effects; ignoring durability would fabricate success.

Specs: `tests/tasks_pkg/test_agent_persistence_wait.py`,
`tests/swarm/test_agent_persistence_parity.py`, shared invocation/Goal integration,
and task-event delivery/cancellation suites. Synthetic residency measurement uses
nested writers across 50 retries; the wait-witness graph remains constant and is
released at ACK. No new thread or persistent-growth allowance is introduced.

`InvocationPolicy` is explicit: the root owns conversation settlement and kickoff;
a child owns its carrier, scoped tools, inherited cancellation, context/affinity,
and required evidence sink, never the parent's lifecycle. Child creation registers
its immutable owner/parent/conversation identity with the existing Sidecar; transient
parents remain storage-free. Graph phase admission and settlement use ordinary
v3 turns and attempt fences. See [storage ownership](STORAGE.md#child-archive-ownership)
and [lossless phase projection](contracts/CONVERSATION_SYNC_V3.md#goal-phase-execution).

Graph `OrchestrationInvocationRunner.prepare` separates ordinary child construction from its one-shot execution entry. The existing `__call__` immediately consumes this scope; recovery callers can prepare several recipients before consuming any. Model/tool selection, context hydration, registered identity, accounting and output projection retain their existing owners. Unused scopes reject through ordinary task settlement before closing publication/accounting. A scope closing during execution signals cancellation and joins that invocation, including interrupted waits. There is no new task registry, model loop or executor barrier. Alternatives rejected: duplicate child construction for recovery, or leave prepared children pending on partial failure. `test_orchestration_prepared_invocation` verifies unused rejection, complete preparation before dispatch, one-shot races and joined cancellation; ordinary runner/child/Goal-context tests preserve parity.

Request and post-tool Inspector evidence share `round_evidence.py` and the existing incremental snapshot log. Attended roots and visible children retain post-tool state; headless roots keep their explicit optional-mirror policy. Completion supplies canonical output, not a transcript reconstructed from tool previews. `test_goal_invocation_integration.py` runs the same two-round pipeline for root and child, then exercises isolated SQLite archive/accounting, cold Inspector authorization, lost-ACK retry without re-execution, stale-attempt rejection, cancellation and failed evidence delivery. Structural and concurrent boundaries live in `test_goal_invocation_output.py`, `test_child_ingress_commit.py` and `test_child_archive_storage.py`; `test_visual_goal_persisted.py` verifies shipped desktop/mobile rendering after cold reload.

`inherit_child_config` owns the declared configuration whitelist and legacy-key
normalization for both schedulers. Missing reasoning enablement stays unspecified;
explicit off stays off, and depth survives persisted Swarm sessions. The selected
route compiles the same intent for Chat, Goal and Swarm. Cross-entry tests live in
`test_swarm_reasoning_inheritance.py`.

The retired Swarm request path converted absent reasoning enablement to false
and lost the inherited depth. Effort-only Codex routes rejected that invented
disable intent before dispatch. Schedulers now pass the original intent to the
common compiler; they never reconstruct model-specific reasoning defaults.

Resume refreshes delegated policy while retaining native message/tool evidence.
Round checkpoints consume the common loop's working messages. Every execution
has a new child identity; tool rows carry its explicit `inspectorRef` through live
and cold projections, so a resumed card cannot point to another execution's
request. Parent configuration and transcript remain isolated. Cancellation and
optional deadlines reach blocked tools through the same child control port.

Problem: detached Swarm parent proxies lost native execution and fee ownership. Decision: `tasks_pkg/native_parent.py` carries a typed, process-local reference to the exact issued parent separately from frozen config/tool grants. Scope and shared account must still match; model selection and child binding validate the canonical live task. The session ID remains the coordinator identity, while a continued child belongs to its current executor. Root usage counts its own requests; `apiRounds` and cost include descendant fees. Alternatives rejected: copying lifecycle fields grants unrelated authority; task-ID lookup weakens exact-instance admission; switching to Python loses native accounting. Session metadata persists only requirement identity and a native-required flag. Unfinished native actors, including parent-stop resumption, require acknowledged actor checkpoints and whole-tree handoff; transcript checkpoints cannot authorize takeover. Cold start retains evidence and Continue refuses before workers or model I/O. The existing usage-unknown error and completeness fields preserve known fees, prevent new admission even without a token cap, and avoid presenting missing actor accounting as a model transport failure. Terminal evidence remains readable without execution. Specs: `tests/tasks_pkg/test_native_parent.py`, `tests/swarm/test_native_delegation_recovery.py`, `tests/tasks_pkg/test_native_production_delegation.py`. Dynamic native-root recovery remains unqualified; shared-root quiescence is unchanged.

Internal guards stamp `_invocationStopReason` and an existing error envelope before signaling: `stalled` / `deadline` settle failed (`invocation_stalled` / `invocation_deadline`), not cancelled, on normal and exception exits. Explicit Stop is checked first. Physical `client_aborted`, partial output, paid usage and tool receipts stay truthful. Stall diagnostics include only numeric silence and threshold, not beacon notes or provider payloads. Live/cold projections use the existing failed/error rendering. Failure delivery can earn the existing single bounded recovery wake; cancellation cannot. Never blindly replay partial calls. Three consecutive batches containing only the same operation's typed, non-retryable failures stop the common loop with a failed outcome; productive calls remain available without a private round ceiling.

`test_swarm_invocation.py` replaces tests of the retired private Swarm methods
with real request/tool execution, cancellation, resume, artifact, terminal and
Inspector checks. Provider alias/schema/ingestion rules remain covered at their
shared tool boundaries. `test_swarm_agent_loop_chassis.py` rejects direct Swarm
transport/tool execution and includes negative controls for the guard itself.

Problem: root request ordinals excluded descendant fees from native checkpoints. Decision: explicit checkpoint_tree_usage retains root tokens separately from tree consumption and original child quotes separately from root request rounds. The versioned checkpoint schema owns these shapes. Root fees retain public ordinals; child fees retain executor, source ordinal and original billing receiver. Accepted handoff rebinds the root while retaining those quotes. Exact account restoration precedes creation of immutable retained-row witnesses and native model admission. Reopened same-task execution bills retained rows; a new accepted task bills only its fresh rows. Alternatives: renumbering requests corrupts the resume frontier; merging child usage into root reports changes the visible provider evidence; replaying child work repeats effects. Selected embedded TofuDB owns journal CAS, lease/writer fences and byte admission. No extra store, ledger, executor or default switch is introduced. The two-descendant fixture captures 22,610 JSON bytes under its 1 MiB snapshot bound; the existing frame admission precedes durable completion. Bare Continue reads the accepted source's exact owned terminal invoice/capsule on the existing callback executor, then increases historical spend once before any native acquisition. Root usage remains separate, original prices are retained, and the new task bills only its fresh rows. Unknown, foreign or altered source evidence refuses; Stop joins the physical read. One retry tuple lasts with the root: fixture tuple/integers measure 112 bytes on Python 3.12, excluding account dictionary/key and shared state. This grants no journal lease or replay rights. Explicit same-executor SIGKILL recovery retains its own unsettled quotes; only an older executor's contiguous invoice prefix remains paid elsewhere. The selected journal must prove writer death and match the hydrated projection before account/fee restoration and model entry. Interleaved billing origins refuse. Completed paid root/child/grandchild frontiers recover without replay and settle the same wallet hold once after lost ACK; inflight frontiers refuse and retain unresolved holds. Unsupported journal profiles still refuse child entry.

## Child event publication

Problem: the root observer's fixed 30-second drain revoked healthy children whose required evidence was still waiting for storage. Decision: child delivery starts on demand on its task-local FIFO worker, retaining evidence in order. Normal provider completion joins delivery under existing inherited cancellation, execution deadlines and progress guards. Successful deliveries refresh progress; wait intervals do not. Cancellation/disposal allows 250 ms, then revokes publication and drops queued closures. Actual sink failures still fail closed. Alternatives rejected: increasing the grace period merely moves the failure; proceeding before settlement loses evidence and ordering. The existing worker and monitor suffice. Regressions cover slow sinks, FIFO success and stop policies. Production chat/replay commit locks recheck publication after acquiring their owner lock; deferred delivery keeps its original context. No observer I/O runs under revocation locks. An entered transaction retains its attempt/CAS fence; revocation cannot roll back a commit.

Optional Swarm output logs are lazy, append at most 2 MiB per worker (including
existing resume logs), and buffer at most 16 KiB between writes. They flush on
round/terminal boundaries. The full transcript uses the ordinary durable store;
the log ceiling only bounds reconstructible diagnostics. Budget and lifecycle
tests cover concurrent workers, blocked observers, cancellation and log growth.

## Plan collaboration mode

`planMode: true` is one attended, read-only model/tool loop. Config resolution
and runtime normalization enable `ask_human` automatically and disable
autopilot, direct image generation, and selected orchestration flows. This
allows multiple clarification exchanges inside the same turn without asking
the user to discover a second toggle. The canonical schema stays within 325
tokens, bounds question/option payloads, and requires 1–16 options for choice
mode.

The model proposes rather than executes. Only the successful Plan-task terminal
boundary may mint a complete tagged plan into the typed turn sidecar owned by
`lib/plan_contract.py`; arbitrary assistant text is never upgraded into
execution authority. Leaving Plan mode manually does not synthesize an
execution prompt. Execution starts only through the v3 exact-plan command in
[`CONVERSATION_SYNC_V3.md`](contracts/CONVERSATION_SYNC_V3.md).


## Execution unification decisions

**Problem.** Shared invocation execution does not by itself guarantee terminal
handoff, budget conservation or safe result acknowledgement. Treating every
workflow state as a chat Turn would also remove information the current domain
owners need, while replacing asynchronous Swarm with a blocking Flow would
remove existing product capabilities.

**Decision.** Chat, Plan, Goal, Flow roles and Swarm keep the same invocation
kernel. Unify the boundaries that carry the same semantics; retain explicit
policy and domain state where semantics differ:

| Concern from the unification proposal | Running contract / decision |
|---|---|
| Legacy Autopilot loop | Disconnected from normal finalization and human guidance; old kickoff carriers settle without dispatch. Current Goal controls and historical reports remain. |
| Mode normalization and multiple representations | Keep normalization at untrusted admission and inherited-config boundaries. Settings are preferences, attempt config is a frozen execution input, Turn kind/actor is provenance; none replaces the other. Plan permission and Goal continuation are independent axes, with current product exclusivity retained. |
| Node model/tool policy | `model_override` selects a route explicitly; selected-model Goal runs reject a conflicting override. Node `tools` only intersects inherited/role authority. Omission inherits; explicit `[]` grants no tools and survives save/reload. |
| Invocation-tree budgets | One live account covers Flow and Swarm descendants of a bounded parent. Atomic tool admission, per-call reported usage, model-round admission and inherited deadline prevent budget multiplication. Malformed supplied limits fail closed. |
| Liveness / presence | Child execution uses the existing cancellation monitor and `ProgressBeacon`, including tool output progress. The ordinary startup/teardown path remains the presence owner; no graph-only heartbeat thread or second presence registry. |
| Dynamic scheduling | Flow `fanout` joins a validated runtime DAG on `StreamingScheduler`. Existing async spawn/await/get/auto-continue remain available with their existing lifetimes. |
| Identity / child durability | Retain invocation identity and the existing owner-scoped child/Inspector archive. Definition node, dynamic expansion, invocation attempt and visible Turn are distinct identities. Dynamic execution IDs scope each expansion; forced nested chat Turns are not the storage model. |
| Result messages | Shared provider-consumption boundary acknowledges staged Swarm inbox and tool-return results. Domain payloads retain their provenance and authority. A received worker result or VU review is never a real-user authorization. |
| Artifacts / rendering | Keep existing typed output, artifact references, bounded trace and child Inspector projections. Dynamic children use scoped IDs. Replacing domain artifact stores or rendering every domain record as a conversation would discard useful semantics. |
| Verdicts | Keep common invocation outcomes and evidence projection; VU progress/challenge gates and Evolution holdout/checker/learning gates retain their domain owners. A prompt/parser configuration is insufficient to make their acceptance conditions equivalent. |
| Evolution | Explicit terminal handoff advances committed run/coordinator bindings. Durable task observers handle research tasks. Periodic reconciliation remains necessary for crash/missed-callback recovery; no second graph state authority or KV migration is introduced. |
| UI / capabilities | Studio uses existing palette, parameter fields, trace reducer and translations. Evolution's workbench and Swarm's nested presentation remain domain views; tool authority remains enforced at invocation/gateway admission. |
| Recovery / asynchronous suspension | Durable Flow has explicit receipt-based restart recovery via `POST /tasks/<run_id>/resume`; completed effects are reused and ambiguous in-flight agents refuse replay. Durable human gates park without workers and survive restart; Goal working contexts/checklists reuse canonical lane checkpoints. Ephemeral gates and Swarm retain their domain lifecycles. |

**Alternatives considered.** A single Flow interpreter for all workflows would
require a durable suspended-state protocol, versioned continuation program
counter, effect receipts, attempt fencing and explicit reconciliation before
it could replace Swarm or Evolution. Neither a nested Turn nor a blocking human
gate supplies those guarantees. A generic message table or generic verdict
parser would create a parallel authority without removing the existing domain
contracts. Those replacements are deliberately rejected in this implementation.

### Consumption and delivery limits

Tool slots are reserved before dispatch and are not refunded after an admitted
side effect. Token consumption is provider-reported: already in-flight requests
may exceed the last remaining allowance, so this is not a strict token or money
reservation system. Completed children release live references but retain their
usage in the account. The live account does not claim crash-persistent budget
reservation; recovered workflows must use their durable domain counters.

Swarm prompt assembly only stages receipts. A verified successful provider call
acknowledges the durable outbox and then the live result set. ACK failure retains
pending receipts; cancellation/finalization restores unconsumed results and live
reservations. Tool returns use the same staging boundary. Receipts include the
Swarm run identity, so a newer session's reused agent ID cannot be acknowledged
by a prior session's staged result. Direct synchronous master consumers retain
explicit immediate-delivery semantics. Delivery after an uncertain ACK is at
least once, not exactly once. Per-session result persistence remains the outbox;
no new storage backend or process-global message authority is added.

Verification owners: `test_terminal_delivery`, `test_execution_tree_budget`,
`test_goal_invocation_integration`, `test_child_invocation`,
`test_swarm_consumption_delivery`, `test_swarm_async`,
`test_legacy_autopilot_retirement`, and `test_orchestration_fanout_runtime`.


## Dynamic fanout and node execution policy

`fanout` accepts `params.specs_json` (a JSON task array, at most 64 KiB), or
`params.objective` for a tool-free planner invocation. Each task declares a
unique `id`, registered delegatable `role`, `objective`, optional `context`,
`depends_on`, `model_override`, and `tools`. The resource probe determines the
wave/concurrency allowance; ordinary Flow agent-count and invocation-tree
budgets also apply. Unknown dependencies, cycles, duplicate IDs/objectives,
unknown fields and malformed tool lists fail before branch dispatch.

`fanout_expanded` records an execution-scoped expansion and all specs before
scheduler admission. Dependency-ready tasks run immediately through the normal
role runner; the control joins all tasks and emits `fanout_complete` only after
successful completion. Required publication errors propagate and cancellation
joins worker teardown. Static role nodes expose the same `model_override` and
narrow-only tool list. Omitted tools inherit; explicit empty tools remain empty
across authoring/save/execution. Selected-model Goal runs cannot use an override
to change the user's model.

This additive control does not replace asynchronous Swarm. Expansion events
alone are inspection evidence. Durable Flow restart recovery additionally
requires committed execution receipts, described below; uncertain tool effects
are never automatically replayed.
The unification decisions and alternatives are recorded in
[the execution decisions above](#execution-unification-decisions).

The event registry, authoring metadata, HTTP schema, compatibility defaults and
browser reducers share this vocabulary. Regenerate all affected orchestration
contracts and rebuild the retained frontend when changing these fields.


### Resource measurement

A local synthetic measurement (2026-09-20; five scheduling samples) used two
parallel slots and tasks of 20 ms, 120 ms and a 60 ms dependent of the 20 ms task.
Median dynamic execution was 122.34 ms versus 180.56 ms for the equivalent
barrier schedule; the dependent started at 22.46 ms. This demonstrates dependency
readiness, not a forecast for provider latency. All scheduler threads had exited
on return (one main thread remained).

Three 2,000-admission samples at 1/8/64 active children measured median shared
tool-admission overhead of 8.43/29.11/177.86 µs per call on this host. The account
holds active task references only: after 2,000 sequential child closes, one root
entry remained. The check scales linearly with active children, not historical
children; no new resident timer, storage queue or durable artifact is created
by budget accounting. Existing launch-derived child monitor and scheduler
ceilings apply; operators' explicit concurrency overrides remain effective.


## Durable Flow restart recovery

**Problem.** Run/event persistence preserves inspection history but a preview
cannot authorize repeating an external effect after process loss. Retrying the
whole graph duplicates completed work; restoring private interpreter fields
would create a second, brittle control-state schema.

**Decision.** Durable runs created by the run API journal nondeterministic boundaries through `execution_journal.py`: agent results, human decisions and referenced definitions. Admission [binds registered children](contracts/TOOL_EFFECT_RECOVERY.md#flow-agent-identity) before dispatch; the complete result is committed before the interpreter consumes it. Recovery re-executes the existing graph interpreter with those results, rebuilding typed I/O, loop feedback, verdicts and accounting without repeating completed invocations. Scoped, framed occurrence identities cover loops, nested isolated subflows and dynamic fanout. Fingerprints bind each receipt to exact node/context inputs. Definition lookups replay their frozen result instead of reading edited flows. The execution snapshot bypasses browser layout repair. Journal version changes must accompany incompatible interpreter/replay semantics.

Startup still settles orphaned runs as `worker_lost`; it never starts providers.
An authenticated `POST /api/v1/orchestrations/tasks/<run_id>/resume` explicitly
creates one deterministic successor and returns the ordinary durable-run read
envelope. Only startup-interrupted runs qualify. Repository ownership applies
to source, history and successor. Unique run creation elects the launcher;
repeated requests return the same successor without spawning again. A launch
claim remains on the source, so deleting the successor cannot authorize a second
execution. A failed/ambiguous worker handoff stays closed and visible.

The seed is copied into the successor before any new effect. A complete seed
is independent of its parent; an interrupted copy resolves the parent through
the same owner-bound store and refuses missing/cyclic history. No pointer is
trusted as an execution receipt. Existing atomic event/header projection rejects
late publication after abort/terminal settlement on native and SQL authorities.
Publication failure cancels sibling admission and propagates across parallel and
isolated-subflow boundaries.

**Alternatives.** UI `step_complete` previews and fanout expansion records lack
full result and admission evidence. Replaying an interrupted provider/tool call
cannot prove whether an external effect committed, so an unmatched agent
admission needs its exact registered child completion or rejects recovery. A pending human gate can safely ask again; a
committed decision is reused. This is effect-boundary recovery, not tool-round
resume or exactly-once external I/O. Same-run human continuation uses the
protocol below. Ordinary chat/Goal working sets use canonical lane checkpoints;
asynchronous Swarm retains its domain recovery contract. Pre-journal historical runs refuse automatic replay.

**Pending input inspection.** `recovery_frontier.inspect_pending_inputs` runs that same interpreter with a read-only journal policy. Completed receipts rebuild control/dataflow and working context; each unfinished agent must match its exact framed key and node/context digest before its branch transfers control. Parallel and dynamic fanout owners drain every sibling before returning suspension; no placeholder result advances downstream dependencies. Missing/unreachable actors, unused receipts, changed inputs, new effects, cancellation or an exceeded caller byte allowance refuse the entire inspection. The returned node/context/iteration and worker-context snapshots grant no child, wallet or replay authority. `GoalRecoverySeed.inspect_pending_inputs` applies the archived Goal launch policy. Alternatives rejected: a second graph walker, UI preview reconstruction, or synthetic success results to traverse past unfinished work. Tests cover parallel/nested graphs, isolated subflows, fanout dependencies, later loop iterations and actual Goal-writer SIGKILL before native claim. Ordinary automatic pending execution remains fenced until the complete native recipient set is prepared and consumed. Local five-sample inspection of one completed receipt plus 1/8/32 pending parallel workers took median 0.42/3.76/11.84 ms, with peak traced allocations 32/152/447 KB and zero graph workers after return. The interpreter uses its existing demand executor with idle hint 1; this excludes native I/O, recipient and model execution.

**Whole-set checkpoint readiness.** Problem: each recipient's checkpoint factory needs its genuine `RootLoopRequest`, but waiting for other recipients inside that factory can occupy every callback worker. Decision: optional `NativePreparedInvocation.checkpoint_preparation` enters a caller-owned async scope before the ordinary factory, then awaits readiness after the factory worker returns and its policy identity is checked. `NativeGoalHandoff.prepare_recipients` owns that scope for an exact task/source set. Its read-only budget admission uses the same complete-set preflight as the later transfer; it moves no counters. Only all pristine policies together invoke the existing `consume` transaction on the supplied executor. No native client, route or model/tool call begins first. Factory failure or Stop wakes siblings; cancelled handoff joins the issued write and capability adoption before ordinary policy close. Scope exit joins already entered invocations and disposes every abort subscription. The ordinary child owner still owns terminal rejection, execution and policy cleanup. After successful handoff, ordinary execution owns cancellation. Unknown ACKs remain fenced without retry. Alternatives rejected: a barrier in the callback executor, fabricated ROOT requests, a second checkpoint factory, or policy closure racing native adoption. Tests cover a single callback worker, factory/no-policy failures, parent/child Stop, interrupted teardown, cancelled committed/unknown writes and actual native whole handoff followed by original-fee/account load and the next ordinary journal write. Local five-sample readiness/factory/account/close composition at 1/8/32 recipients took median 3.15/17.81/131.65 ms, with peak traced allocations 49/316/1708 KB, one callback worker and none after close. Storage and provider are fixture ports; this includes existing whole-account cut copies and excludes native I/O. Production continues to borrow its launch-sized executor. Automatic pending Goal dispatch still requires explicit application composition with the original checkpoint.

**Prepared Flow recipients.** Problem: preparing native takeover targets ahead of execution must preserve the actual branch's context, event and cancellation owner. Decision: `prepared_recipients.prepare_flow_recipients` scopes ordinary registered children for the exact inspected input set. The journal passes its existing occurrence key, digest and awaited admission through `AgentExecutionAdmission`; one explicit root dispatch policy matches that occurrence to its prepared child. The actual runner must have the same parent object, launch configuration and original worker context before adopting execution. Its ordinary completion owner updates conversation state and journals the full result; child registration, completion CAS and lost-ACK reconciliation are unchanged. Subsequent leaves use ordinary invocation after the complete prepared set has entered. Native readiness still gates provider/tool work independently. Source binding checks the complete unique source inventory; it grants no checkpoint rights. Unused entries reject, entered children cancel/join on close, and scoped maps/context copies are cleared. No additional worker pool or persistent store is introduced. Alternatives rejected: a replacement graph walker, manually constructing child completion, or updating the preparer's stale context instead of the actual nested branch. Tests cover linear/parallel/isolated/nested/fanout traversal with both cursors, event/Stop ownership, mismatch refusal, registered completion and lost acknowledgement. The application fixture also exercises actual native file rounds with fresh prepared children; it is not evidence of interrupted-checkpoint recovery. Local five-sample preparation at 1/8/32 children took median 1.66/10.74/41.82 ms, with peak traced allocations 72/180/546 KB. Complete dispatch/close took 6.95/32.99/117.91 ms and left no graph workers or active child accounts. These storage-free fixture measurements exclude input inspection, native I/O and model latency; retained copies scale with the inspected frontier and end with its scope.

**Actual native Goal interruption.** Problem: composing individually verified recovery ports exposed three ordering/proof gaps: child execution admission preceded whole-set takeover, paid tool charges differed from the earlier fee checkpoint, and a killed, unentered batch had no handler intent to inspect. Decision: the ordinary child owner supplies its same journal callback through `NativePreparedInvocation.admission_scope`; prepared recipients await the whole checkpoint ACK before that callback and before native execution. Admission writes use the existing callback executor and join cancellation; fresh invocations keep their original ordering. Model and fee restoration share the exact recovered actor-account check, including the complete original pending file batch. No paid tools or historical model quotes are added again. File reconciliation requires owned immutable source receipts, or an acknowledged uncharged source prefix plus an unchanged registered source with no exact intent. The latter returns `file_not_published/checkpoint_batch_unentered`; it never executes the old call. A model may issue a new call through ordinary authorization and accounting. Contradictory intent, foreign parent, version races and partial batches refuse. Alternatives rejected: accepting already-started successor effects in storage, requiring stale tool counters to equal the later paid cut, or treating missing receipts alone as proof of no effect. Preparation failures also preserve their original error when child creation publishes rejection before returning a child.

`test_goal_native_source_sigkill` starts a real HTTP Goal with installed native Flow/ROOT, then kills its owned process group after a committed pending checkpoint or file publication. Sequential and parallel fixtures use accepted continuation, native whole-set handoff and ordinary registered child completion. Published appends remain exactly once; unentered calls return observations and the loopback provider explicitly requests new writes. The accepted target Turn completes; original and successor wallet quotes settle independently, and repeated invoice recovery does not charge twice. The target startup is an explicit fixture port composition, not an enabled automatic pending dispatcher. Model quality, unsupported tools/approvals, repeated recovery and full browser delivery are not established by these deterministic provider fixtures.

Legacy Autopilot close-out/run-pin imports re-export Goal owners. Chat Flow
commit retry uses the injected host persistence port.

## Live human-wait publication, cancellation and cleanup

**Problem.** Early publication could expose unanswerable requests; cancellation
and exceptions could leave waiters resident, and missing approvals could be
mistaken for permission.

**Decision.** Shared approval/guidance APIs own registration, required
`on_registered` publication, wait and exact-entry cleanup, in that order. Flow
live gates, tool approvals, attended single/batch `ask_human`, slides outline
review and motion storyboard review all use this boundary. Rejected admissions
publish no request; publication failure releases the registration. Both waits
poll cancellation and heartbeat within the existing two-second interval.
A response that won the registry lock is preserved, but execution rechecks Stop
before continuing. Cancelled/unavailable responses never mint permission,
successful gate receipts or browser grants. Probe failures propagate.

**Alternatives.** Detached waiter threads retain resources; HTTP answer retries
or pre-registration buffering create another authority. Existing owner-scoped
registries supply the live contract without new workers, timers or queues.
Durable Flow human nodes use the persisted continuation protocol below.

Verification: `test_human_gate_resolution`, `test_orchestration_human_gate_runtime`
and `test_tool_settle_all_lanes` cover owner scope, publication, cleanup,
answer/Stop races and aborted settlement without execution.

## Durable human continuation and Goal context recovery

**Problem.** A live human registry keeps a worker resident and loses unanswered
requests at restart. Goal workers also had no settled working-context handoff
between attempts, despite ordinary chat already having a canonical lane cache.

**Decision.** Durable Flow human nodes unwind through the existing interpreter. All parallel workers join before `continuation.py` publishes `human_request` with its effect identity, input witness, context, and approval deadline, then finishes the live generation. The durable run remains `paused`; its stable request ID (`dg.<run>.<effect hash>`) is answerable before live notification. Startup retirement preserves this state on native TofuDB and the SQL Sidecar. An owner-bound answer commits one result using the existing event-sequence CAS. Only the insertion winner launches a new live generation; the durable run ID stays unchanged. Repeated identical answers are idempotent; conflicting answers cannot replace a committed decision. Replaying the graph reuses committed agent, definition and human receipts. Subsequent generation headers validate the preceding segment's seed hash/count without copying its result bodies. Other parallel gates become answerable in later generations. No external effect is dispatched before required receipts.

Cancellation first fences the durable run, then rereads the winning generation
claim and aborts that worker. A generation created after cancellation checks
the durable fence before dispatch. Failed handoffs publish a visible error;
explicit `/tasks/<run>/resume` may reuse the committed decision. Unmatched agent
admissions need a matching registered child completion; unresolved effects still refuse recovery.

Approval deadlines use wall time across restarts. Reads, list/poll and answers
settle expired waits lazily, with bounded tail reads: a committed expiry intent
wins the same sequence CAS as an answer, then an idempotent terminal transition
sets `aborted`. If the process fails between those writes, startup preserves the
intent and the next observation finishes it. An unobserved expired row may still
be physically `paused`; its deadline cannot authorize subsequent work. Input
waits have no implicit timeout. No timer, queue or daemon is added per wait.

Goal's accepted worker messages and checklist are published only after the
visible phase commits, through `ModelContinuationStore`. Explicit Goal continue
creates a new ordinary turn/attempt and restores this prefix through the same
owner, settled-frontier, history/edit, media and tool-policy checks as chat.
Checklist state restores only under matching execution policy. The cache holds
one bounded working set per owner/conversation/lane; it never replaces Turn
history or resumes an old provider/tool attempt. Missing, oversized or invalid
cache entries use the existing cold reconstruction path.

**Alternatives.** Detached waiter threads retain the resource problem. Copying
all prior receipts per human answer causes quadratic result storage growth.
Another Goal snapshot database would duplicate ownership and edit invalidation.
Replacing Swarm/Evolution domain state machines is unnecessary for these gains;
the shared invocation, lifecycle and recovery boundaries provide the reuse.

Verification: `test_orchestration_continuation`, `test_goal_worker_context`,
`test_storage_application_contract` and the authenticated orchestration API
suite cover concurrent decisions, cancellation/handoff races, parallel waits,
expired/rejected approvals, interrupted expiry publication, restart and owner
isolation. Operational bounds and measurements live in
[storage operations](STORAGE_OPERATIONS.md).

Reasoning-only output exhaustion uses the existing bounded terminal recovery
with reduced reasoning, retaining the original deliverable and output format.
Problem: a generic short-blocker instruction conflicted with complete code/JSON
requests even when no tools were available. Decision: recover toward the
requested result; use native tools only when necessary and available.
Alternative rejected: raising token budgets or accepting a status message as
the requested artifact. Recovery still cannot establish artifact correctness.

Problem: pre-dispatch budget rejection could mark otherwise complete consumption
as unknown. Decision: the admission port emits a typed refusal bound to its
budget account; the root adapter preserves completeness only when that same
account is complete. Earlier unknown usage remains unknown, and no flags are
cleared. Alternatives: error-text matching or an empty current usage map cannot
prove non-dispatch; neither authorizes a zero-cost claim or replay.
