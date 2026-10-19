# Conversation synchronization guidance

## Scope

This package owns server-side v3 conversation command/query services and
generated contract bindings. Read `contracts/conversation_sync_v3.yaml` and
`docs/contracts/CONVERSATION_SYNC_V3.md`.

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

`command_service.py` stamps the authenticated tenant into accepted task config.
Dispatch reads that config through the explicit owner-only lifecycle port; public
attempt queries keep their default private-config filtering.
Task startup resolves optional native ROOT/Flow composition from the current app,
inside pre-bind rollback. It never parses request fields into native authority or
installs a process-global backend; disabled composition leaves native imports lazy.
Flow owns its graph journal; role invocations borrow native child ports and retain
the original root invoice rather than billing sanitized phase summaries.

read_accepted_resume_lineage reuses storage-issued _resumeSource from the owned
accepted config. Verify current bound task/Turn and distinct settled source;
request config, foreign/stale/missing records grant no authority. The immutable
relation grants no journal takeover. Verify lineage and real legacy wallet resume.

validate_resume_producer follows the same stored receipt owner through repeated
interrupted handoffs; cycle/byte/cancellation fences bound temporary IDs. Verify
foreign/live/superseded ancestry and real repeated file ROOT restart; this grants
inspection only, never journal takeover or wallet settlement.
