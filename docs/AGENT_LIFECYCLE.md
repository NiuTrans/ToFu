# Agent lifecycle and recovery boundaries

This is the cross-entry lifecycle map, not another runtime or persistence format.
Execution policy lives in [TASK_EXECUTION_MODES.md](TASK_EXECUTION_MODES.md),
team identity/delivery in [RETAINED_AGENTS.md](RETAINED_AGENTS.md), project messaging
in [PROJECT_COORDINATION.md](PROJECT_COORDINATION.md), and graph/Goal recovery in
[RUN_CONTINUITY.md](RUN_CONTINUITY.md). Those owners retain their domain contracts.

## One mechanism, explicit policy ports

**Problem:** sharing a model loop does not establish uniform durability. Launch
admission, delivery acknowledgement, and recovery authority can still diverge.
**Decision:** reuse the owners below regardless of who created an agent. Keep
identity, assignment and physical execution distinct. Domain adapters select
scope, capabilities and continuation authority; they do not introduce another
model loop, persistence retry loop, mailbox acknowledgement rule or scheduler.
**Alternatives rejected:** a universal workflow interpreter would duplicate the
existing invocation owner; treating every saved transcript as resumable would
replay ambiguous effects and lose cancellation/accounting authority.

| Responsibility | Shared owner | Policy that remains explicit |
|---|---|---|
| Model/tool execution | `tasks_pkg/invocation.py`, `child_invocation.py` | Root settlement vs child evidence/cancellation ports |
| Required storage waiting | `tasks_pkg/persistence_wait.py` | Selected repository, task deadline, cancellability of effect admission |
| Retained launch reconciliation | `swarm/retained_launch.py` | Assign or stable-message claim; same accepted/rejected/unknown classification |
| Whole-message input selection | `tasks_pkg/message_input_budget.py` | Eligible project or exact-run originals and existing batch limit |
| Physical-input receipt witnesses | `tasks_pkg/message_input_receipts.py` | Current project scope or exact team invocation authority |
| Round/event and terminal persistence | Task/event/checkpoint and Turn owners | Destination and required domain result ACK |
| Checkpoint/effect recovery | Existing task, Swarm and graph/native journal owners | Exact accepted lineage, effect proof and accounting handoff |
| Retained discovery | `swarm/retained_repository.py` | Owner/tenant/scope; authoritative versions precede cached roster metadata |

Embedded/headless storage-free execution remains explicitly transient. Sharing
execution machinery must not silently attach application storage to it.

Terminal preparation has explicit owners. **Problem:** the root finalizer mixed
checkpoint merging, request-cost snapshots and diagnostic observers with the
terminal write/release sequence, and the per-round budget path imported that
large finalizer just to project costs. **Decision:** `tasks_pkg/round_costs.py`
owns request snapshots and display totals using the existing pricing engine;
`orchestrator/_completion_receipt.py` projects terminal fields and continuation
metadata; `_completion_observers.py` retains independent best-effort experiment,
metric and logging boundaries. The finalizer keeps event/persistence order,
retry debt and entry-specific handoff; `_post_commit.py` still runs only after
acknowledgement. **Alternatives rejected:** a generic callback pipeline hides
authority/order, while another receipt or pricing engine duplicates existing
state. Verify actual emitted facts, continuation totals, observer failures,
frozen native fees and the chat terminal/child accounting contracts.

Stream recovery retains explicit policy order. **Problem:** one analyser mixed
transport, unusable-response and tool-payload recovery, duplicated round-text
rollback, and was imported by model-refusal fallback for that same rollback.
**Decision:** `stream_handler/_analyse.py` sequences the existing policies;
`_transport_recovery.py`, `_response_recovery.py` and `_tool_recovery.py` own
their respective retry bodies. `tasks_pkg/round_retry.py` shares exact text
rollback through the manager's existing epoch/event ports. `api.py` remains the
typed provider-evidence boundary; normal continuation, task-wide terminal
recovery and phase retry budgets retain their distinct scopes. **Alternatives
rejected:** a callback registry obscures precedence and authority; separate
rollback copies can diverge on discard evidence. Verify typed completion before
tool execution, exact partial prefixes, discarded text/epochs, refusal billing
and route-aware retry diagnostics through the actual analyser and root loop.

## Evidence ordering

1. Commit the scoped identity/assignment claim before dispatch. A lookup name
   is not a receipt proving that dispatch occurred.
2. Classify launch as **accepted**, **proven pre-admission rejection**, or
   **unknown**. Unknown retains the claim; it cannot authorize a replacement.
3. Dispatch through the ordinary invocation owner, with inherited cancellation,
   scoped tools, explicit accounting and the required evidence destination.
4. Persist intent/completed frontiers through existing checkpoint and event
   ports. Required persistence waits retain exact receipt/payload; a storage
   timeout does not justify rerunning model/tool effects.
5. Persist terminal facts before releasing ownership or publishing durable
   success. Lost callbacks reconcile exact owned terminal evidence, not lookup
   keys, roster state or an inferred task-ID prefix.
6. Continuation revalidates the original owner, accepted attempt/generation,
   Stop/replacement state, current policy, paid consumption and effect frontier.
   Proven completed work can settle without executing again; ambiguous effects
   remain refused/unknown until the existing domain resolver establishes them.

For retained launches, actual server-bound session identity is authority; a
predicted or returned lookup handle alone is not. Both `assign` and stable-message
activation preserve the original lookup IDs after uncertain replies. A matching
completion can settle the existing claim, but an absent callback cannot free it.
Explicit reset/abandon fences late retained-memory publication; it is not proof
that a previous external effect did not occur.

## Communication is not execution admission

- Project Coordinator and Worker conversations use the same passive project
  mailbox. Coordinator appointment is role metadata, not a separate transport
  or permission to start another conversation.
- A conversation main agent and its Sub Agents, including teammates, use exact-
  run Swarm mailboxes. Stable retained activation reuses ordinary launch admission.
- Nonhuman messages do not extend tool/human authority. Exact-run/project sends
  do not wake an idle recipient; stable activation has its own explicit contract.
- An idle root is reached only by a final result. **Problem:** a worker's
  `send_agent_message` to its original root is rejected while that root runs no
  turn (`message_target_not_active`/`message_run_ended`), but the rejection named
  no way forward, so a blocked worker retried or stalled beside a runaway
  command. **Decision:** keep the rejection; its `next_action`
  (`swarm/integration/_tools.py::_idle_coordinator_action`) says not to retry or
  retarget, to finish now with a final result stating the blocker, evidence and
  need (ordinary final delivery reaches the coordinator), or else to keep
  working, and, when the worker holds them, to manage its own `bg_*` commands
  with `poll` and `kill`. **Alternatives rejected:** waking or queueing a root
  turn on a worker message (grants workers turn-start authority over their
  coordinator); a worker escalation channel (a second mailbox and wake path
  beside final delivery). Guard: `test_idle_root_rejection_tells_the_worker_what_to_do_instead`.
- Selection scans the bounded mailbox in order for fitting whole originals,
  stopping at the existing per-round batch limit. Unfit originals remain pending
  and inspectable, but do not block later small messages. Already-present carriers
  remain eligible for physical-input confirmation even if fresh input cannot fit.
- Submission, logical insertion, verified input confirmation, consideration and
  task completion are different facts. Failed/cancelled provider requests cannot
  confirm input. A crash after provider completion and before receipt ACK still
  has no persisted pending-ACK outbox: consumption can remain unknown. Never buy
  another model request solely to manufacture an acknowledgement.

## Recovery support is explicit, not universal

| Entry/evidence | Running behavior | Refusal boundary |
|---|---|---|
| Ordinary conversation task at boot | Settle orphaned attempts/results as interrupted; startup does not redispatch billable work | Saved result/transcript alone is not continuation admission |
| Swarm transcript checkpoint | Rehydrate only under exact accepted spawning/Continue authority, including eligible detached children | Stop, replacement, foreign/missing lineage, unresolved in-flight tools |
| Retained identity | Restore identity/history; reconcile exact owned terminal assignment without another launch | Unknown launch with no actual session binding/completion stays busy |
| Native nonterminal Swarm actor | Keep evidence; cold takeover/Continue remain fenced | Transcript recovery cannot replace native actor/account handoff |
| Graph/Goal/native execution | Existing journal, actor/account and accepted-successor ports decide continuation | Unsupported profiles, unresolved effects/usage and changed human authority |
| Embedded storage-free child | Process-local execution only | No durable restart guarantee |

These limits are intentional safety boundaries, not a claim that long-running
continuity is finished. Additional continuation support must extend the existing
journal/admission ports with crash tests; it must not infer replay permission
from a successful process restart.

### Reading retained Swarm checkpoints

- Problem: retained checkpoint volume grows with every conversation-scoped
  wave, while each storage response is bounded by the 64 MiB swarm stream
  ceiling. A startup read that returned every resumable session with every
  agent's messages eventually exceeded that ceiling on every boot; the refusal
  was then retried as transient load shedding and reported as "no sessions",
  so nothing resumed and the unresumed sessions were never reaped.
- Decision: `swarm.resumable.list` returns owner-local session keys only, and
  every session read (`persistence.load_session`, shared by startup, Continue,
  status and retained messaging) combines the `swarm.session.get` header
  (`include_agents=false`) with `swarm.agents.page`. A page packs whole agents
  below the ceiling and names the next agent; one agent core always fits.
  `swarm.session.get` with agents still refuses instead of returning incomplete
  recovery state, and that refusal is a non-retryable `domain_resource_bound`.
  Startup resumes each readable session independently. An unreadable index or a
  transient session read raises into the boot recovery step's bounded backoff
  (2 s, 5 s, 12.5 s), which skips sessions already live; a session that is
  deterministically unreadable is logged with its key and keeps its evidence.
- Alternatives considered: raising the response bound (history keeps growing
  and boot still materializes all of it); omitting terminal agents' messages
  (restart safety checks read them, so `restart_checkpoint_invalid` decisions
  would change); paging sessions without paging agents (one long
  conversation-scoped session alone can exceed a response); retrying from the
  maintenance tick indefinitely (a deterministic bound never heals, and the boot
  step already owns the transient schedule).
- Tests: `swarm::tests::retained_checkpoints_beyond_one_response_stay_recoverable_in_bounded_reads`
  and `resource_exhaustion` in `packages/tofu-db/src/`,
  `tests/swarm/test_swarm_resumable_recovery.py`,
  `tests/storage_authority_sql/test_swarm_agent_pages.py`,
  `tests/server/test_server_startup_recovery.py` and the swarm script in
  `tests/tofudb/test_tofudb_differential.py`.

### Swarm results outlive their session

- Problem: a swarm session and its agent rows are keyed by the reusable
  conversation swarm key. TTL eviction (`SESSION_TTL_SECONDS` of inactivity)
  and a new wave replacing a terminated session called `swarm.session.delete`,
  silently deleting every agent's final result, which is user state; a
  terminated run left behind by a restart was re-attributed to the next run
  and deleted with it. `get_agent_result` could not name an earlier wave and
  `<swarm-update>` carried no run identity, so a long-lived conversation could
  not recall its delegated results.
- Decision: the conversation-keyed rows remain resumable state. Every settled
  outcome is also recorded by `lib/swarm/persistence.py` as a portable record
  keyed by owner, `swarm_run_id` and agent ID (owner, tenant, conversation,
  run, agent, role, objective, status, final answer, errors, usage and record
  time); the master records it before publishing `<swarm-update>`, which now
  carries `<swarm-run-id>`. Teardown (`integration/_state.py`
  `_record_results_then_delete`) records anything still unrecorded, from the
  in-memory orchestrator or, without one, from the durable rows, and deletes
  the rows only after every record is acknowledged; otherwise the rows stay.
  A new run claiming a conversation key first records and reclaims rows an
  earlier run left there. `get_agent_result(swarm_run_id, agent_id|agent_ids)`
  admits only the named run: the live session when it is that run, then that
  run's durable rows, then its record, which must match the caller's owner
  (record key and value), conversation and tenant.
- Alternatives rejected: never deleting the rows (agent IDs collide across
  waves under one key, and restart recovery would resurrect expired
  sessions); re-keying the rows by run (every recovery, status and messaging
  reader resolves the conversation key); a new storage operation or table in
  both authorities (the generic record operations already store identical
  owner-scoped documents in SQL and native TofuDB); recording only at
  teardown (an agent settling after an abort, or a crash, would be missed).
- Growth and retention: [STORAGE_OPERATIONS.md](STORAGE_OPERATIONS.md#swarm-run-result-retention).
  Tests: `tests/swarm/test_swarm_run_results.py` (TTL eviction, new-wave
  replacement, cross-wave fetch, owner and conversation isolation, retained
  rows on a failed record).

### Swarm continuation is result-driven

- Problem: a count (`SWARM_AUTOCONTINUE_MAX_CHAIN`, env
  `TOFU_SWARM_AUTOCONTINUE_MAX`, default 3 consecutive continuations, reset by
  a human turn) cut autonomous multi-stage work at an arbitrary number: a
  tuning knob that also cut late-settling frontiers the human had launched.
  Forbidding continuation-launched work from waking the next continuation
  replaced the knob with a stall: a chat whose continuation launched a second
  stage sat idle with unread results until a human returned.
- Decision: swarm continuation follows the project Coordinator, which every
  Worker settlement resumes with no count or deadline (owner decision "No
  spending ceiling", [PROJECT_COORDINATION.md](PROJECT_COORDINATION.md)).
  `integration/_autocontinue.py` wakes for an owned settling session only with
  undelivered stamped (completed/failed) results of that session's own
  `swarm_run_id`; each settled delivery frontier wakes at most once (durable
  command identity) and new required work opens a new frontier whichever turn
  launched it. One failure-bearing recovery per human chain, and Stop,
  live-conversation and blocked-main refusals remain. A chain ends when the
  model launches no further work. The count, its env var, the in-memory counter
  and the turn-prelude reset are removed.
- Alternatives rejected: keeping or tuning the count (constraint 5);
  provenance gating that never wakes for continuation-launched work (stalls
  autonomous flows, and differs from Coordinator continuation for no
  authority reason: continuations already carry no human authority); waking
  without new results (spurious loops with nothing to act on).
- Tests: `tests/swarm/test_swarm_failure_continuation.py`, and
  `TestAutoContinueGuardrails` and `TestContinuationWaveChain` in
  `tests/swarm/test_swarm_async.py`.

## Verification and cost boundaries

The focused launch, mailbox and roster tests inject crashes/lost replies before
and after their durable boundaries. Existing `test_agent_persistence_parity.py`,
`test_agent_persistence_wait.py`, `test_swarm_restart_continue.py`,
`test_swarm_detached_restart.py`, `test_native_delegation_recovery.py` and
`test_message_input_receipts.py` protect the shared mechanisms and refusal cases.

Roster enumeration merge-joins ordered authority headers with metadata-only
index pages. Matching versions do not hydrate dialogue; missing/stale projections
read only that exact identity and retry cache repair on a later list if its write
fails. Deleted identities cannot reappear from leftover cache rows. Pagination is
not a transaction-wide snapshot: concurrent changes appear at observed versions.
There is no new journal, poller, worker or durable per-message copy. Metadata-only
`record.list(include_value=false)` preserves existing cursor/scope/page semantics
on SQL and native storage without fetching JSON/blob values. Physical read windows
remain bounded; accepted identities/transcripts retain their existing lifecycle.

Cost evidence: a real Sidecar fixture retains 9,437,184 dialogue bytes while its
authority header response is 189 JSON bytes. Selector-only five-sample warm
medians at full mailbox size were project 8.071/13.794 ms and Swarm 3.663/5.748 ms
(byte/token-reject prefixes). These use the host heuristic counter, exclude
storage/scope/provider latency and do not qualify native/BPE tokenization.
Project acquisition validates each distinct sender once per injection (including
denials), then discards the cache; physical request admission still revalidates.
No new resident execution or persistent-growth allowance is introduced.
