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
