# Debug utility guidance

## Scope

This is a scratch and diagnostic boundary, not an application package. Only
explicitly whitelisted diagnostics are shipped; most files are intentionally
ignored and must never be imported by production code.

## Conversation diagnostics

For a pasted conversation ID, the first command is always
`python3 debug/inspect_conversation.py <conv_id>`. Extend that read-only tool
when the canonical diagnostic is missing evidence; do not hand-query SQLite or
guess storage tables or authority paths. It must fail closed when a fastpath
shadow exists but no unique write front can be proven.

## Editing rules

- Diagnostics are read-only by default. A migration or recovery script must be
  clearly named, separately authorized, idempotent where possible, and support
  a dry run plus postcondition verification.
- Use public operations or repository adapters so turn-native projections match
  the running sidecar. Direct database access is limited to documented offline
  maintenance boundaries.
- Bound log scans and output; redact tokens, cookies, prompts, personal paths,
  and user content. Never commit captured databases, logs, screenshots, or raw
  transcripts.
- Promote reusable checks into `scripts/` with tests; do not let production code
  acquire a dependency on a debug helper.

## Verification

Run the focused test for a shipped diagnostic and exercise it against a
temporary fixture. Confirm `git status --short` contains no generated debug
artifacts before finishing.
