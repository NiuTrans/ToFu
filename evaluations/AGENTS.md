# Evaluation guidance

## Scope

This directory contains hermetic adapters and release evaluations for agent,
tool, and long-context behavior. It is evidence infrastructure, not a second
production implementation.

## Editing rules

- Reuse production contracts and public runtime entry points. Evaluation-only
  projections may normalize results but may not change product semantics.
- Pin dataset/task identity, model and harness versions, prompts/configuration,
  checksums, seeds, budgets, and acceptance criteria.
- Default tests stay offline and deterministic. Live providers, external
  sandboxes, and expensive suites require explicit markers/flags and bounded
  concurrency, time, tokens, retries, and artifact retention.
- Keep baseline and candidate inputs equivalent. Label inference separately
  from measured evidence and retain enough metadata to reproduce comparisons.
- Place cloned repositories and run outputs in the supported external/ignored
  evaluation roots. Never commit credentials, unpublished user data, or raw
  workspace snapshots.
- A candidate graduates through the production owner and its tests; do not
  import evaluation adapters from `lib/`, `routes/`, or `tofu_agent/`.

## Verification

Run the focused tests matching the changed evaluation package, then its
documented preflight/export check. Use `-m live_llm` only when explicitly
authorized; release gates must still have a hermetic path.
