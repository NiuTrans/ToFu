# Conversation synchronization guidance

## Scope

This package owns server-side v3 conversation command/query services and
generated contract bindings. Read `contracts/conversation_sync_v3.yaml` and
`docs/CONVERSATION_SYNC_V3.md`.

## Editing rules

- The v3 snapshot, turn, attempt, change, and replay model is the only live
  conversation synchronization authority.
- Commands carry owner identity and expected revisions into one atomic Sidecar
  operation. Commit acknowledgement precedes any wake or push hint.
- Preserve idempotent command identity, conflict semantics, replay cursor,
  heartbeat policy, terminal attempt state, and deterministic projection.
- Files named `generated_contract` are regenerated from the canonical schema.
  Do not add permissive parallel parsing to hide drift.
- Keep service policy independent of HTTP routes and frontend representation.
  Hints invalidate; they never contain or write authoritative state.

## Verification

Run `npm run check:conversation-sync`, the focused
`test_conversation_sync_v3*` tests, and relevant Sidecar turn-operation tests.
Add frontend reducer/projection tests for a public snapshot or event change.
