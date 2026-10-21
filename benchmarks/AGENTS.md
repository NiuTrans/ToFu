# Benchmark guidance

## Scope

This directory contains retained, executable measurement contracts. Local run
outputs and candidate result payloads are reconstructible artifacts and remain
untracked according to `.gitignore`.

`tool_result_projection.py` measures six fixed native receipts through production
capture/budget/delivery, with an in-memory retention port. Exact canonical values
and complete model views gate byte/token accounting. Verify
`tests/meta/test_tool_result_projection_measure.py`, including deliberately lost
evidence; the result is a component payload observation, never paid-task savings.

`tofudb_context_progression.py` measures real append/read/checkpoint advancement
with an independent public-header digest and exact suffix oracle. Keep initial
certification, later checkpoint writes and appends separate; round zero's appends
belong to setup. Freeze CLI/library identities and compare fresh alternating
processes. This compares production algorithms, not equal-algorithm engines.
Verify `tests/tofudb/test_context_progression_measure.py`; the public recovery and
older-writer boundary is in `test_harness_frontier_runtime.py`.

`native_engine_manifest_measure.py` compares frozen source-fingerprint owners in
fresh alternating processes. Require the same compiled source digest on every
call and include isolated child CPU. This component diagnostic has no memory,
application or default-selection verdict.

`native_shared_library.py` compares independent/shared admission through the same
installed NativeHost in alternating fresh processes. Both call real token and
Flow ABIs and join all owners. Validate exact work, source pins and physical
member identities before ratios; ru_maxrss is a component high-water observation,
not whole-system RSS/PSS admission. Verify test_native_shared_library_measure.

`native_agent_root.py` compares actual ROOT tasks over a borrowed disposable
application authority and a loopback provider. Decoded requests, durable content,
status and token usage must agree before timing is permitted. One pair is only
comparability evidence; it grants no performance or default-selection verdict.
Run `tests/tasks_pkg/test_native_root_comparison.py` with a verified archive;
`tests/meta/test_native_root_comparability.py` fences unequal-work receipts.
Paired discovery/execute and both direct/gateway mixed orders require exact
requests plus actual checklist/delegation effects; explicit child IDs are fixture
inputs. These functional comparisons still grant no timing/default verdict.
Injected ROOT comparisons assert that tool intent sees post-admission context
on both loop owners; keep injected and plain receipt names distinct.
`native_agent_root_measure.py` runs alternating fresh warm/RSS/cold workers on
that real root boundary. Keep raw clocks, exact work hashes and joined cleanup;
include live storage and waited native CPU. RSS/PSS sums can count a vfork's
shared address space twice: retain those observations and separate syscall
diagnostics. Neither a memory interpretation nor one backend admits defaults.
`native_root_memory.py` pauses only its disposable memory worker tree through
pidfds, confirms every thread stopped, and groups KCMP_VM identities before
procfs reads. Retain raw PID sums and unique-VM RSS/PSS plus pause durations.
Unsupported/denied observations refuse qualification; cleanup resumes owned
stops and joins failed worker trees. Its clocks never grant latency admission.

`process_exit_memory.py` owns a dedicated Linux exit-stop observer. It preserves
original parent waits and defaults to post-exec single-thread/one-exec child RSS
estimates. Explicit threaded-child observation snapshots every exiting TID and
groups shared process peaks once. Unsupported or denied observations refuse and reap the
owned tree. Verify `tests/meta/test_process_exit_memory.py`, including observer
death. Exit PSS is a snapshot; traced clocks never admit performance.

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

`token_cold_start.py` measures one encoding's first operation in each fresh
process using token_ingress_measure --cold-only. Verify distinct PIDs, zero warm
calls, both clocks, identical fixtures/instrumentation and unchanged sources.

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

`search_html_reference.py` scores frozen extraction outputs against external
HTML references. Its text-boundary metric is owned by the search package's
`docs/relevance-quality.md`; verify `tests/meta/test_search_html_reference.py`.
Keep prior metric versions and missing-output cases in their original reports.

`http_h2_credit.py` separates handled cancellation from blocked response DATA:
exhaust both H2 windows, grant connection credit only, keep SSE paused, then
verify RST cleanup and reuse. Run meta/test_http_h2_credit and paired actual
servers. Preserve the original HTTPX failure/recheck evidence; this diagnostic
does not identify historical unobserved windows or grant performance admission.

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

Application timing and stopped-memory collectors accept explicit
`--reuse-native-workers` only for the application entry. Freeze the requested
policy and the actual native single-worker PID/readiness/invocation receipt;
missing or mixed receipts refuse qualification. Historical reports without a
policy remain recomputable. Include native host, process and worker-pool sources
in the frozen witness. Verify with `tests/test_native_application_root_benchmark.py`
and `tests/meta/test_native_root_memory.py`; a reuse mechanism gain never waives
full application warm/cold/memory or whole-system admission.

Application phase diagnostics cover storage.v2 attempts as fixed labels and
separate context preparation, round presentation/cost settlement, terminal
projection/identity and durable writer spans. Nested spans overlap; never add
them or use instrumented clocks for performance admission. Wrappers retain no
operation, payload, owner or credential values. Verify wrapper behavior in
`tests/test_native_application_root_benchmark.py`.

Native application stage diagnostics retain only cataloged storage operation
labels and fixed parent phases. Fixture Attempt polling carries its own phase;
other may include work dispatched without context propagation. Verify
tests/meta/test_native_storage_stage_labels.py and actual ROOT profiles; never
use instrumented clocks for timing admission or sum nested stage durations.

HTTP ROOT completion_observer remains poll by default. worker-future borrows
scheduler Futures through native_completion.py and verifies durable readback
separately after retirement. Never mix observer modes or report removal of
fixture polling as product speedup. Verify native_completion_observer tests and
both real application arms; calibration does not erase earlier rejections.

Formal application timing/memory collectors freeze completion_observer through
native_application_authority.check_completion_observer. Include native_completion
in source witnesses; missing future-mode tags cannot receive qualification.
Verify native_completion_collectors tests and actual worker propagation.


HTTP ROOT native_usage_summary freezes explicit terminal projection selection;
its native witness counts delivered summaries separately from paid frozen usage.
Paid frozen requests do not prove replacement of Python accumulation. Timing
collectors reject missing/mixed selections or delivery witnesses; memory rejects
this unqualified variant. Verify ROOT and completion collector guard tests.


native_terminal_delivery uses a pinned real binary and existing transient receiver;
provider/receiver/terminal counts and exact long history must match before clocks
are accepted. Each operation owns/joins a native process. Tail starts before final
provider bytes, not at a Rust candidate timestamp. No mocked persistence or durable
ROOT qualification. Verify meta/test_native_terminal_delivery_measure.py.

Terminal delivery completion_reference records one actually validated final
reference per operation. Full events remain default; freeze selection independently
of paid work. Verify the reference witness guard before timing comparisons.


compare_reference_pair requires the same binary/work/clock setup, distinct
nonempty worker identities and physical off/on reference witnesses. Ratios remain
transient observations, never application/default admission; preserve small-task
adverse tails alongside long-history gains.


HTTP ROOT native_terminal_reference freezes requested selection and counts actual
accepted SDK reference frames. Timing requires the exact per-worker count; missing
or mixed policy refuses, and memory rejects this unqualified variant. Durable
semantic smokes do not admit defaults or establish reference performance causality.
Verify ROOT benchmark and completion collector guards.


compare_native_reference_pair compares actual native rows with shared canonical
durable/financial validation; freeze archive/authority/work/policy/affinity and
clock scope, require fresh joined workers and exact reference frames. Never rename
native rows to Python or infer migration admission from these warm-only ratios.


Terminal retry_reference counts verified retry frames separately from final
references. compare_retry_reference_pair requires actual receiver retries and a
fixed final-reference policy; neither optional flag nor transient timing admits
durable/default behavior. Verify collector witness and pair guards.

Application ROOT collectors accept explicit daemon and embedded storage engines.
Freeze the deployed binary and, for embedded, its validated sibling library.
Check the ready runtime's actual backend/transport/PID and joined stop, not just
requested environment values. Historical receipts remain recomputable without
claiming new observation coverage. Verify meta/test_native_application_authority
and actual Python/native workers before collecting separate backend cohorts.


Pipeline metrics use `conversation_concurrency.capture_storage_metrics` for
process/birth/incarnation and quiescent-boundary provenance. Event-only timing
is `phase_ms.events`; legacy productive includes admission/recovery. Exact
multi-operation attribution uses `tofudb_profile_report.py --pipeline-window`
and refuses reset, in-flight or dropped populations. Profile-on embedded
commands use a different dispatch path; only profile-off paired arms qualify
latency. See `docs/STORAGE_PERFORMANCE_CHARTER.md`.

`live_pipeline.py` validates actual application browser receipts from
`tests/visual/test_visual_live_pipeline.py`. Require selected ready storage,
complete producer/commit-notice/SSE/DOM joins, bounded clock alignment and no
hot snapshot recovery. Logical command/SSE bytes are not WAL amplification;
notification clocks are not fsync clocks, and DOM/frame callbacks are not pixels.
Use fresh serial processes and the same built assets for policy comparisons;
verify `tests/meta/test_live_pipeline_measure.py` before interpreting receipts.
For loaded runs, require all participant completion/content/delivery receipts
inside the measured window and overlapping producers. Process CPU/WAL cover the
whole cohort; foreground command/SSE/DOM observations retain their own scope.
The explicit native producer uses `tests/support/native_live_pipeline.py` and a
verified archive through run_native_task. Require ordered complete text hashes,
joined clients, visible partial text and input samples overlapping the stream.
Its isolated-client/context/terminal work differs from the Python synthetic
producer; that regression arm is not an equivalent-work speed comparison.
