# Debug utility guidance

## Scope

This is a scratch and diagnostic boundary, not an application package. Only
explicitly whitelisted diagnostics are shipped; most files are intentionally
ignored and must never be imported by production code.

## Conversation diagnostics

For a pasted conversation ID, the first command is always
`python3 debug/inspect_conversation.py <conv_id>`. Extend that read-only tool
when the canonical diagnostic is missing evidence; do not hand-query SQLite or
guess storage tables or authority paths. Respect the configured backend: never
read a leftover SQLite file as native state. SQLite discovery must fail closed
when a fastpath shadow exists but no unique write front can be proven. Native
locators identify authority; they never grant query access or contain secrets.

It works on conversations that are being written (running agents, swarms).
Exit 0 means one verified revision; exit 3 means partial evidence: an
`INCOMPLETE` budget stop, a `LIVE` final Turn still being written, or a
`BEST-EFFORT` view whose turns are marked `[as of revision N]`. Never treat a
best-effort view as one snapshot, and never weaken the per-read fences to make
one; the strategy and its limits are owned by
`docs/contracts/CONVERSATION_DIAGNOSTICS.md` (concurrent-write reads).

## Editing rules

- Diagnostics are read-only by default. A migration or recovery script must be
  clearly named, separately authorized, idempotent where possible, and support
  a dry run plus postcondition verification.
- Use public operations or repository adapters so turn-native projections match
  the selected authority. Direct database access is limited to documented offline
  maintenance boundaries.
- Bound log scans and output; diagnostic value handling follows docs/LOGGING.md.
  Never commit captured databases, logs, screenshots, or raw transcripts.
- Promote reusable checks into `scripts/` with tests; do not let production code
  acquire a dependency on a debug helper.

## Verification

Run the focused test for a shipped diagnostic and exercise it against a
temporary fixture. Confirm `git status --short` contains no generated debug
artifacts before finishing.
