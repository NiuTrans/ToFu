# Project modification guidance

## Scope

This package owns model-facing project reads, writes, and bounded command
execution. Tool schemas/approval live in `lib/tools/`; project coordination and
Git publication have separate owners.

## Editing rules

- Resolve every target beneath an explicit authorized project root. Reject
  traversal, unsafe symlinks, ambiguous roots, and path changes between check
  and use.
- Reads and writes carry project/root attribution and freshness evidence. A
  stale snapshot or mismatched write set fails visibly instead of overwriting
  concurrent user work.
- Prefer atomic replacement and recoverable file history. Multi-file changes
  define rollback behavior; never broaden a requested deletion or cleanup.
- Command execution uses structured arguments where possible and explicit cwd,
  environment, timeout, output, process-tree, and cancellation bounds.
- Schema validation and model intent are not authorization. Preserve approval
  receipts and deny unattended side effects that require user consent.
- Redact secrets and bound diffs, file content, command output, artifacts, and
  retained history.

## Verification

Run focused project read/write/command tests, then atomicity, rollback, root
attribution, freshness, symlink/traversal, approval, cancellation, and concurrent
writer tests.
