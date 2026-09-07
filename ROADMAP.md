# Architecture roadmap

This roadmap contains only unfinished structural work. Product contracts and
current behavior live in `docs/`; completed work lives in Git history and
`CHANGELOG.md`.

## Direction

Tofu optimizes for one authoritative implementation per concept, explicit
ownership and lifecycle, and source layouts that a model can understand in one
bounded read. Compatibility layers are deleted once in-tree callers move. A
fallback may reduce capability, but may not introduce a second state machine,
transport, repository, or error taxonomy.

## Current sequence

1. **Make frontend source native.** Move retained JavaScript sections into
   typed modules by domain and delete the composed runtime once no retained
   section remains. Runtime and stylesheet composition stay deterministic
   while that inventory shrinks.
2. **Thin orchestration modules.** Reduce `server.py` to composition and
   lifecycle wiring; split `lib/turn_lifecycle.py` and other remaining large
   modules by state owner, with explicit inputs and failure semantics.
3. **Externalize distributed runtime state.** Make task events, aborts,
   scheduler leases, human-input waits, quotas, and invalidation safe across
   replicas before enabling enterprise deployment.

## Completion rules

- A migration is complete only when the old production path, flags, tests, and
  documentation are gone.
- Generated artifacts have a checked composer or generator and are excluded
  from default search.
- A boundary has one machine-readable contract and outcome-focused tests.
- New persisted access carries explicit identity and stays storage-dialect
  neutral.
- Every retained document appears in `docs/catalog.json` and describes current
  behavior.

Start from `docs/README.md`. Enterprise gaps and their evidence are maintained
in `docs/ENTERPRISE_READINESS_AUDIT.md`; frontend ownership is maintained in
`docs/FRONTEND_ARCHITECTURE.md`.
