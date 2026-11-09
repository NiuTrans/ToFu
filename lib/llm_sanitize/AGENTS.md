# Message transformation guidance

This package owns canonical message cleanup, exact text normalization and
content-free transformation witnesses. It has no task policy, provider I/O,
cache classifier or persistence authority.

- `context_sections.py` defines managed section identity once; the composer
  re-exports it. Never derive identity differently in inspection or rendering.
- `lineage.py` seeds request-local ranges from producer-owned carriers,
  observes named transformation stages and maps actual protocol emissions.
  Update ranges at the transformation that emits the content. Unknown changes
  lose their witness; never recover event identity by substring matching.
- Transient spans and the private body sidecar must stay out of provider JSON.
  `lib/llm/request.py` binds the finished map to sealed content and request
  identity. `contracts/context_lineage_v1.schema.json` owns its exported shape.
- Source maps retain IDs, hashes and positions only. Existing task evidence
  and `snapshot_delta.py` own persistence and degradation; do not add a store.

Read `docs/CONTEXT_INJECTIONS.md` for the investigation contract and current
coverage. Verify `tests/context/test_context_lineage.py`, sanitizer whitespace
and same-role tests, protocol conversion, request sealing and
`tests/tasks_pkg/test_context_lineage_storage.py` after changing this boundary.
