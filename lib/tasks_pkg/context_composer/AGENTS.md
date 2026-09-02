# Context composer guidance

## Scope

This package is the single owner of assembling model-visible context from
system, task, conversation, memory, project, and tool sources.

## Editing rules

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

Run `tests/test_context_composer.py`, context-limit/token-counter tests, and
permission, ordering, deterministic budget, cache-epoch, and provenance cases.
