# Benchmark guidance

## Scope

This directory contains retained, executable measurement contracts. Local run
outputs and candidate result payloads are reconstructible artifacts and remain
untracked according to `.gitignore`.

`task_affinity_measure.py` compares an exact frozen pre-change task factory
against production through fresh task creation, actual repository-tool
publication, and a real loaded Quart request. Cold clocks exclude interpreter
and common imports. Each worker owns its runtime/log directory and settles its
audit queue before reporting residency. Verify first-operation receipts,
source identities, fresh PIDs, and loaded-web warm samples independently;
neither task creation nor this tool boundary proves whole-agent admission.

`audit_preflight_measure.py` compares a frozen exact rotation function through
actual short/wide audit writes, forced copytruncate and async queue completion.
Workers own temporary runtime/log paths and settle their queue. Calling-thread
CPU and whole-process completion CPU are separate; RSS workers retain no clock
vectors. Verify raw quantiles, process identities, exact records, inode fences
and source digests. Preserve earlier tail failures when larger cohorts resolve
a measurement concern; component admission never proves whole-system speed.

`usage_signature_measure.py` measures public usage-cache counts against an
exact frozen canonical signature. Separate timing/RSS workers own runtime/log
paths and settle the audit queue. Verify full-prefix bytes and failure behavior,
output/fixture/source digests, PIDs, raw quantiles and residency. Larger targeted
cohorts retain earlier failures; these fixtures exclude full context/inference.

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
For active-conversation performance, `conversation_concurrency_contract.json`
owns coverage and latency budgets; `conversation_compare.py` runs paired native
and SQLite authorities. Verdict tests live in
`tests/tofudb/test_conversation_benchmark_acceptance.py`; the operator contract
is `docs/STORAGE_PERFORMANCE_CHARTER.md`. Microbenchmark throughput is not a
conversation-capacity acceptance result. Requested restart flags are not
recovery evidence: verdicts require actual running/final restart measurements,
full running readback, a declared process-crash mechanism and successful integrity
verification. Failed/skipped phases remain coverage gaps.
For context-efficiency work, start with the commands documented in
`context_efficiency/README.md` and the `test_context_efficiency*` contract
tests; for replay changes, run the `test_task_replay*` tests.

`embedded_process_recovery.py` owns standalone embedded-process crash checks.
Run its supervisor tests in `tests/tofudb/test_embedded_process_recovery.py`;
interpretation and invocation live in `docs/STORAGE_PERFORMANCE_CHARTER.md`.
