# Conversation and Project Brain guidance

## Scope

This package owns owner-scoped conversations and the signal-driven Project
Brain application boundary. Turn synchronization lives in
`lib/conversation_sync/`; Project Brain event/projection transactions live in
`lib/storage_authority_sql/operations_pkg/_project_brain.py`; Git publication lives
in `lib/integration_control.py`. Read
`docs/modules/conversations_project_brain.md`.

## Editing rules

- Scope every query and command by explicit owner plus the canonical project
  key. Do not add a process-global user or storage/path shortcut.
- Work items derive only from successful runtime signals. Their conversation
  is immutable and their state is exactly active/completed/failed/cancelled;
  never add claim, lease, block, reopen, transfer, or autonomous dispatch.
- Project Context is a final user-role dynamic suffix. Never modify the system
  prompt, replay snapshots, or acknowledge narrative before provider success.
- Cross-conversation path overlap advice is bounded and execution-local. Push
  is a UI hint; neither is durable coordination state.
- Charter decisions require an immutable registered Checker version. Checker
  processes use argv without a shell, bounded output/time, and fail publication
  without changing terminal work state.
- Git/ref behavior delegates to integration control and its repository contract.

## Coordination message owners

`compaction_policy.py` reads owned coordinator/source protections for the default
ConversationStore's manual admission and commit recheck. The reusable engine
uses that store port; policy and verification live in `docs/CONTEXT_COMPACTION.md`.

`project_coordination.py` owns explicit coordinator metadata binding and the
ordinary root role guidance; it starts no execution.
`project_messages.py` owns passive same-project originals and consideration
receipts; `contracts/project_messages_v1.json` is the shared tool/wire authority.
Neither module dispatches work or extends ProjectWorkItem lifecycle. Optional
input budget and exact physical-request witnesses live in
`lib/tasks_pkg/message_input_{budget,receipts}.py`; do not copy those policies
into queue, HTTP, UI or message handlers. Regenerate coordination TypeScript
with `scripts/gen_project_coordination_contract.py` after schema changes.

`coordinator_reports.py` publishes human-facing reports through canonical
assistant Turns. Its portable records contain a transient retry intent and a
latest-report/read pointer; never put a second transcript or model executor
there. `project_coordination_v1.schema.json` owns report shapes. Verify with
`test_coordinator_reports.py`, `test_coordinator_reports_authority.py`, the
coordination HTTP journey and the real Coordinator browser journey.

`coordinator_workers.py` adapts current-request delegation, result waiting and
review, cancellation and paged status to ProjectDispatchService. No private executor or synthetic human input.
The source/policy authority is `lib/project_dispatch/delegation.py`; verify
`test_coordinator_workers.py` and native `test_coordinator_workers_authority.py`.

`coordinator_budget.py` owns owner/project future defaults and passive request
usage projection, using dispatch receipts. Saving defaults cannot amend a running
request or create work. Verify the budget HTTP CAS/owner journey and native restart.

## Verification

Run `tests/project_mod/test_project_brain_signal_driven.py`, then the Sidecar cutover,
context-composer, integration-control, API, and frontend gates for crossed
boundaries.
