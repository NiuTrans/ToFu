# Benchmark guidance

## Scope

This directory contains retained, executable measurement contracts. Local run
outputs and candidate result payloads are reconstructible artifacts and remain
untracked according to `.gitignore`.

## Editing rules

- Keep datasets, manifests, pricing inputs, random seeds, model/provider
  identity, and acceptance criteria explicit enough to reproduce a result.
- Default runs must be bounded and dry-run/offline when practical. Live model
  calls require an explicit opt-in and must report their cost and limits.
- Separate measured observations from conclusions. Do not promote a one-off
  result into a product default without a controlled comparison and the owning
  product test.
- Use canonical cost, token, task, and event contracts rather than copying
  production calculations into a benchmark.
- Write large artifacts outside the repository or under ignored output roots;
  never commit credentials, transcripts, cloned workspaces, or raw provider
  payloads.

## Verification

Run the smallest benchmark self-check and its matching tests under `tests/`.
For context-efficiency work, start with the commands documented in
`context_efficiency/README.md` and the `test_context_efficiency*` contract
tests; for replay changes, run the `test_task_replay*` tests.
