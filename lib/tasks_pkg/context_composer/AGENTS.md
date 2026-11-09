# Context composer guidance

## Scope

This package is the single owner of assembling model-visible context from
system, task, conversation, memory, project, and tool sources.

Start implementation investigations at `_render.render_context`: prepare,
reconcile visible snapshots, admit budget, place carriers, publish receipts.
These stages stay together in `_render.py`; `injections.py` owns chronological
events and rollback. `registry.py` owns policy, never a second dispatch loop.
For exported request drift, use `scripts/inspect_context.py BEFORE [AFTER]`;
`inspection.py` joins actual section hashes to source policy and receipts.
`lib/llm_sanitize/context_sections.py` owns the shared identity;
`_sections.py` is its compatibility facade. `_lineage_inspection.py` validates
exported transformation witnesses and joins existing cache records.
Marker observations never grant
authority, and an input difference alone never proves a provider cache miss.

## Editing rules

- Register every automatic source in `registry.py` before adding a provider or
  chronological carrier. Use the public `context_block` / `append_context_blocks`
  or `append_injection` interface; do not hand-append synthetic prompt dicts.
  Necessity inventory, protected lanes and Inspector receipts are specified in
  `docs/CONTEXT_INJECTIONS.md`.

- Providers return explicit blocks with source, priority, required/optional
  status, permissions, cache epoch, and token/byte cost. Composition order is
  deterministic.
- Resolve identity and permissions before fetching or rendering a source. A
  provider failure cannot inject stale, partial, or foreign-owner content.
- Required blocks are never silently displaced. Optional blocks degrade by one
  documented budget policy and report omissions/provenance.
- Use canonical token counters and model context limits. Freeze inputs for one
  model attempt so concurrent state changes cannot produce a mixed snapshot.
- Keep rendering pure after acquisition and do not persist, compact, or mutate
  source domains from the composer.
- Bound providers, fetch time, blocks, tokens, bytes, attachments, and diagnostic
  receipts; redact sensitive content from logs.

## Verification

Run `tests/context/test_context_composer.py`, context-limit/token-counter tests, and
permission, ordering, deterministic budget, cache-epoch, and provenance cases.
`test_context_lifecycle.py` pins reuse/failure/retraction and open-tool batches;
`test_context_inspection.py` covers exported requests, source attribution and
the read-only CLI, including missing/stale evidence and quoted markers.

Provider acquisition uses the shared demand kernel through `_provider_executor.py`.
Launch knobs size idle reuse, never active-read or pending-count admission.
Preserve request deadlines, detached inputs, late-result exclusion and local
lease cancellation; only executor-owned close joins physical workers. Verify
`tests/context/test_context_provider_executor_lifecycle.py` with real startup
faults, 32 blocked reads, healthy sibling work and close beyond one second.
A required read cannot degrade, so its deadline only makes the wait visible:
`turn_requirements.py` keeps its in-flight read through
`persistence_wait.wait_for_persistence` (`docs/contracts/ARTIFACT_REVIEW.md`);
never turn storage slowness into a fatal admission error. Verify
`tests/artifacts/test_turn_requirements_context.py`.
