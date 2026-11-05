# Benchmark guidance

## Scope

This directory contains retained, executable measurement contracts. Local run
outputs and candidate result payloads are reconstructible artifacts and remain
untracked according to `.gitignore`.

`storage_history_cohort.py` continues complete private Turn exports through the
production capture/FIFO/commit owners on disposable authorities. Frozen inputs,
all event rows, final projection digests, SIGKILL/reopen and offline integrity
gate comparisons. Metadata, mixed work and terminal clocks remain separate;
neither these controlled continuations nor slow-log selections are live-user
latency distributions. Verify `tests/meta/test_storage_history_cohort.py`;
method and measurements live in `docs/TASK_EVENT_DELIVERY.md`.

`owned_inventory_reads.py` measures real task-list and project-conversation
aggregation with synthetic owned runtime/catalog/history read ports. Compare
alternating fresh `--repository` trees; `--baseline` checks complete response and
fixture digests. History counts are method calls, not storage RPCs, and clocks
exclude the authority, HTTP encoding and imports. Verify
`tests/meta/test_owned_inventory_measure.py` plus the owning pagination specs.

`tool_result_projection.py` measures six fixed native receipts through production
capture/budget/delivery, with an in-memory retention port. Exact canonical values
and complete model views gate byte/token accounting. Verify
`tests/meta/test_tool_result_projection_measure.py`, including deliberately lost
evidence; the result is a component payload observation, never paid-task savings.

`project_assignment_page_reads.py` builds Project assignment history pages over a
disposable SQL Sidecar (or native TofuDB with `--backend tofudb` and `TOFUDB_BIN`),
counting storage RPCs by operation, returned bytes and encoded page bytes per
build, and checks each page against an oracle rebuilt from complete Turn reads.
`--dependencies` pins every file row to the preceding accepted consult row, so each
build also evaluates prerequisite evidence. Run each pinned tree in a fresh process
with `--repository`; counts are the result, clocks are host observations. Reuse
rules are specified in `tests/project_mod/test_project_dispatch_history_projection.py`.

`catalog_origin_reads.py` admits `--admissions` delegated assignments through the
real repository on the same disposable authorities, then reads sidebar catalog
provenance through the tree's `project_catalog_origins`, composed like its
provenance routes, counting storage RPCs by operation per read. Every read
must equal an oracle of each Worker's oldest Coordinator; one more admission
measures the incremental read, and `--writers N` keeps committing while each read
starts after a fresh commit, reporting failed reads by error type. Run each pinned
tree in a fresh process with `--repository` and a source-matched engine; counts
and failures are the result, clocks are host observations. Verify
`tests/meta/test_catalog_origin_reads_measure.py`.

`project_dispatch_admission_reads.py` admits one live file grant through
`scope.admit_tool` on the same disposable authorities, counting storage RPCs,
repository validations and row rereads per admission; `--cpu-threads` adds busy
Python threads so each RPC competes for the GIL. Conversations, binding and the
human source Turn are in-memory fixture ports. Run each pinned tree in a fresh
process with `--repository`; the decision parity it relies on is specified in
`tests/project_mod/test_project_dispatch_executor_admission.py`.

`tool_dispatch_overhead.py` drives the real tool pipeline one call at a time per
tool on a managed-lane task against a disposable SQLite sidecar, emptying the
result-reuse cache per turn, plus one-turn parallel probes. Synchronous
non-event storage operations per call are load-independent; its clocks are
component observations that this shared host varies by 2x, so compare
alternating fresh runs. Verify `tests/meta/test_tool_dispatch_overhead_measure.py`.

`round_prep_inputs.py` runs production `inject_messages` and admitted same-round
target checks against a seeded swarm on a disposable `tofu-db serve`, counting
storage operations per round. `--source-root` points at an exported pre-change
tree so before/after share one engine binary and fixture; `--authority-parent`
places the authority on a slow volume. Conversation-scope reads are stubbed.
It is a component diagnostic for the `inputs.swarm_messages` round-prep step.

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

`round_prep_body.py` replays a growing 100-300 message history (seeded synthetic
sizes, or a local Request Inspector payload via `--fixture`) through production
compaction, prompt admission, `build_round_request` and snapshot projection,
idle and beside CPU-bound GIL-contention threads. Per-round body, projected-row
and measurement digests must match across passes and compared trees before clocks
count. Verify `tests/meta/test_round_prep_body_measure.py`; storage commits,
providers and the real serving thread mix are excluded.

`model_request_cpu.py` replays a growing append-only tool loop sized by the local
tokenizer (default 50k and 200k tokens) through production `build_body`,
selected-slot adaptation without an admitted count and `prepare_request` for
Chat, Anthropic and Responses wires, in calling-thread CPU and wall time, idle
and beside CPU-bound GIL-contention threads; `--model` picks the logical model
(Claude adds cache markers and Claude adaptation). Sealed payload, semantic and
prefix identities, admitted tokens and both clamped budgets must match across
passes and compared trees; copy one benchmark file into each compared tree and
run them in fresh alternating processes. Verify
`tests/meta/test_model_request_cpu_measure.py`; providers, storage and the
serving thread mix are excluded.

`round_token_count_cpu.py` replays a growing tool loop (default 50k and 200k
tokens, 20 rounds, an OpenAI and an Anthropic route) through every production
stage of a round that counts the request: compaction pipeline and gate, prompt
admission, `build_round_request`, selected-slot adaptation and
`prepare_request`, each round inside the loop's round scope when the tree has
one. `counting` is calling-thread CPU inside the counting entry points
(outermost call only); tokenizer texts and entropy scans per round are
receipts. `--no-l1` keeps the request at the history's size; `--source-root`
measures an exported tree. Gate/admission measurements, clamped budgets and
sealed payloads must match across passes and trees before clocks count. Verify
`tests/meta/test_round_token_count_cpu_measure.py`.


`ocr_gil_ticks.py` runs a burst of synthetic Chinese/English screenshot OCR
jobs through one tree's `lib.knowledge.assets._ocr_image`, one task thread per
image beside CPU-bound GIL-contention threads, while the main thread ticks every
millisecond. It reports tick-gap quantiles and stalled time; per-image output
digests must match across compared trees before clocks count. Compare an
exported pre-change `--source-root` with this tree in alternating fresh
processes. Real Tesseract data is required; storage and providers are excluded.
Verify `tests/meta/test_ocr_gil_ticks_measure.py`; the product spec is
`tests/knowledge/test_knowledge_ocr_process.py`.

`model_dispatch_barrier.py` compares the former awaited pre-request barrier with
`checkpoint_model_dispatch` over the production event entry, snapshot emitter
and checkpoints, with a slow authority stub and a simulated request. Both modes
must commit the identical ordered log before clocks count; it reports the
RoundPrepTiming `barrier` stall, the tool-barrier wait the deferral can shift
and round wall time. `--lane child` measures a child invocation's Inspector
evidence lane the same way: the paid call's pre-call stall is
`build_bodyMs.snapshot` plus `barrier`, against an emulation of the former
acknowledged append. Verify `tests/meta/test_model_dispatch_barrier_measure.py`;
real storage engines, providers and concurrent tasks are excluded.

`event_append_congestion.py` replays durable task events through production
`wait_for_persistence`, `event_log.append_persistent_event`, the event batcher
and the real `StorageV2Client` retry loop against an in-process FIFO authority
that charges queue wait against each request deadline. Foreign writers share
the client (and so the deadline policy's observations); CPU-bound threads add
GIL pressure. It counts attempts and refusals per operation, row sends per
durable row and concurrent duplicate sends, and every lane must commit its
exact rows. `--compare` alternates fresh workers between an exported
pre-change tree and this one. Authority service times are inputs; the real
engine and the delivery coordinator are excluded. Verify
`tests/meta/test_event_append_congestion_measure.py`.


`turn_event_batch.py` persists one prepared FIFO run of K structural task
events on a fresh attempt of a disposable SQL Sidecar or native `tofu-db`
(`--backend tofudb`, `TOFUDB_BIN`) through K `record_task_event` calls and
through one `record_task_events` (`turn.event.record_batch`) call, alternating
modes. Clocks count only after both modes commit the identical projection, K
revisions, K rows and K sync patches (one digest). `--writers` adds background
producers on their own conversations (commit-queue congestion), `--gil-threads`
CPU-bound threads (interpreter-lock pressure), `--authority-parent` a slow
volume. Verify `tests/meta/test_turn_event_batch_measure.py`; this is the
barrier persistence component, not delivery-coordinator or application latency.

`swarm_checkpoint_rounds.py` replays a Swarm child's per-round checkpoints
(`model_in_flight` write-ahead, `tools_in_flight`, `ready`) through production
`SubAgent._save_checkpoint` against a disposable `tofu-db serve`, growing a
transcript from real `lib/*.py` text (default 200 rounds, about 300K tokens).
It reports sink latency per phase and the engine's written bytes
(`/proc/<pid>/io` `wchar`), and refuses to report unless the durable agent row
equals the replayed transcript and final usage. Compare an exported pre-change
`--source-root` with its own source-matched engine (the schema handshake
refuses a mismatched pair) and one shared `--corpus-root`; `fixtureSha256` must
agree. `--authority-parent` places the authority on a slow volume. Models,
tools and round preparation are excluded.

`task_event_delivery_contention.py` drives `EventDeliveryCoordinator` from
`--source-root` (an exported pre-change tree for before) with bursty producers
on independent lanes, a simulated persist time and optional CPU-bound
GIL-contention threads. Its counting state lock (installed while the
coordinator is constructed), pump-job and notification counts are
tree-agnostic; each sample proves exact per-lane commit-then-publish order
before it reports. `--backlog 16,64,136` instead measures the durability
barrier over N structural events queued behind one in-flight head, through the
selected tree's production `append_event` and `turn_lifecycle` record code
against a slow Turn authority stub at the storage client (`--authority-ms` per
transaction); it reports only after every row committed once in order and
every frame published after its commit. Compare alternating fresh processes and verify
`tests/meta/test_task_event_delivery_contention_measure.py`; storage engines,
push and whole-application throughput are excluded.


`delivery_resume_reads.py` runs the real integration worker for whole minutes over
a disposable SQL Sidecar (or native TofuDB with `--backend tofudb` and `TOFUDB_BIN`)
holding `--history` acknowledged delivery rows and `--pending` held receipts, and
counts integration storage RPCs by operation per minute from worker start. Held
receipts go to a recorder that returns them unchanged, so counts are the resume
loop's own reads, not Git or publication work. Run each pinned tree in a fresh
process with `--repository`; the behavior is specified in
`tests/project_mod/test_delivery_resume_demand.py`.

`turn_projection_incremental.py` measures per-event Turn projection capture,
projection and patch time for seeded 10/100/500-round tasks, idle and beside
CPU-bound GIL-contention threads, one fresh worker per arm; `--source-root`
names an exported pre-change tree. Every arm of one size must produce the same
projection/patch digest before clocks count.
Each seeded history also includes canonical intervention notes, and projection
cycles introduce another note, so cumulative identity work participates.
`--entry append` instead drives
the production `append_event` producer entry on a delivery lane and reports
producer-thread CPU per frame and per capture; its digest covers every admitted
charge and captured structure. Verify
`tests/meta/test_turn_projection_incremental_measure.py`; storage, delivery
queues and providers are excluded.

`turn_event_persist_encode.py` measures calling-thread CPU and wall time per
persisted turn event of a seeded long task (default 300 rounds): Request
Inspector snapshots through `build_round_evidence`, the lane detach and the
delta projector, structural and progress frames through `record_task_event`,
and a non-conversation task's two partial checkpoints per round, idle and
beside CPU-bound GIL-contention threads, one fresh worker per arm
(`--source-root` names an exported pre-change tree). The storage stub encodes
each request as the native client does. Arms must produce identical request
bytes, carried rows and checkpoint texts before clocks count. Verify
`tests/meta/test_turn_event_persist_encode_measure.py`; the authority, delivery
coordinator and providers are excluded.

## Editing rules

`model_routing_request.py` measures owner snapshot copying, primary/fallback
compilation and real ephemeral Slot mint/disposal. Compare alternating frozen
source roots with identical fixtures and receipt hashes. Authority/audit IO is
in-memory; this is not inference or chat latency. Verify its meta self-check.

`dispatch_contention.py` reproduces dispatcher-lock contention: about 1,600 live
request-scoped slots minted through the real model-routing adapter, a configured
pool, real `lib.key_stats` admission reads against a store in `--stats-dir`,
CPU-bound GIL threads, pinned/unpinned pickers and one group minter. Latencies
are wall clock around public calls, lock waits included; authority and audit are
in-memory. Compare alternating fresh processes of frozen source roots and put
`--stats-dir` on the volume class under study. Verify
`tests/meta/test_dispatch_contention_measure.py`; never read it as chat latency.

`route_group_retirement.py` compares frozen `slot_pool.py` owners using the same
real Slot fixtures, ordered publication and shuffled retirement. Fresh alternating
`--pool-source` processes report complete lifecycle clocks; `--allocations` adds
separate index-only memory observations. Verify `test_route_group_retirement_measure`;
it excludes storage, provider IO, route compilation and application capacity.

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

`compaction_continuity.py` replays frozen real archives with captured model
narratives through the production continuity owner. It checks exact state
retention every generation and reports packet/candidate estimates and source
hashes. No provider is called; this cannot prove semantic accuracy, fewer tool
rounds or task latency. Verify `tests/compaction/test_unified_continuity.py`.

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
