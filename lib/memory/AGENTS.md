# Memory guidance

## Scope

This package owns durable memory storage abstractions, relevance, prefetch, and
user-profile projections. Context ordering and injection live in
`lib/tasks_pkg/context_composer/`. Read `docs/modules/context_engineering.md`.

## Editing rules

- Every read/write is owner-scoped and carries source, provenance, lifecycle,
  and visibility. Never mix global convenience state with user memory.
- Durable memory uses repository/semantic storage operations. File-backed
  implementations remain behind the declared storage seam and reject traversal
  and unsafe links.
- Selection and ranking are deterministic for equal inputs, bounded before
  expensive work, and fail soft without injecting stale or foreign-owner data.
- Context injection receives a frozen snapshot and explicit token/item budget.
  It does not mutate memory or silently displace required system/task blocks.
- Separate user-authored durable state from reconstructible embeddings,
  indexes, prefetch caches, and derived profiles; only the latter may be
  reclaimed by bounded policy.
- Redact sensitive memory content from logs and metrics.

## Verification

Run focused storage, owner-isolation, relevance, prefetch, symlink/path, and
context-budget tests. Add durable reload and cache-rebuild tests when persistence
or derived data changes.
