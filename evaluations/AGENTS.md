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
- Owner directive 2026-11-05: our benchmark runs (Terminal-Bench, SWE-bench)
  use this repository's launchers (`terminal_bench/`, `swebench/`) with the
  project's own API keys, never the YourProvider canvas (画布/TerminalBenchPipeline).
  `swebench/yourprovider_sandbox/` is the legacy canvas submitter, and canvas
  results are excluded from reported numbers. Driving the EnvSandbox gateway
  from our own runners is not canvas use.

## Verification

Run the focused tests matching the changed evaluation package, then its
documented preflight/export check. Use `-m live_llm` only when explicitly
authorized; release gates must still have a hermetic path.
