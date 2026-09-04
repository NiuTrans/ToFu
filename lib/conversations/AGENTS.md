# Conversation and Project Brain guidance

## Scope

This package owns owner-scoped conversations and the signal-driven Project
Brain application boundary. Turn synchronization lives in
`lib/conversation_sync/`; Project Brain event/projection transactions live in
`lib/storage_sidecar/operations_pkg/_project_brain.py`; Git publication lives
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

## Verification

Run `tests/test_project_brain_signal_driven.py`, then the Sidecar cutover,
context-composer, integration-control, API, and frontend gates for crossed
boundaries.
