# Conversation frontend guidance

## Scope and first reads

Read `README.md`, `docs/CONVERSATION_SYNC_V3.md`, and
`docs/RENDER_CONTRACT.md`. This tree owns typed conversation projection,
presentation models, and the modern conversation surface.

## Editing rules

- The server v3 snapshot plus ordered changes is the authority. Do not create a
  second browser message array, attempt state machine, or settlement path.
- Keep reducer/domain state independent of DOM rendering. Build presentation
  models explicitly, preserve stable turn/attempt identities, and make replay
  and reload deterministic.
- Pending, streaming, terminal, failed, cancelled, superseded, and restored
  states stay distinguishable. Never infer completion from missing UI nodes.
- Conversation lifecycle commands use their generated contract and refresh the
  authoritative graph; delete/restore/clone are not local array mutations.
- Preserve teardown, abort propagation, accessibility, focus, scroll anchoring,
  localization, and bounded rich/tool content.
- Compatibility adapters may project the owner but cannot regain write
  authority or publish private state onto `window`.

## Verification

Run `npm run check:conversation-sync`, focused conversation surface/reducer/
presentation tests, and `npm run typecheck:modules`. Use the browser journey or
visual gate for streaming, scroll, focus, or responsive rendering changes.
