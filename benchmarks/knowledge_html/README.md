# Knowledge HTML projection experiment

`native_ingest_measure.py --engine /absolute/path/to/tofu-db --pdf /tmp/fixture.pdf
--installation /tmp/html-installation.json --python-command /tmp/python-command.json
--output-dir /tmp/new-native-ingest-run` compares the current Python owner with
the Rust projector through actual HTML, PDF, mixed, interruption and large-input
uploads. The installation receipt comes from `build_native_runtime.py --check
ARCHIVE --archive-sha256 PIN --application-version VERSION --install-html-library
/absolute/new/library.so`; use `--archive` instead of `--installation` to include
archive admission. The JSON Python command is an explicit argv prefix. Use a
single exec image for child-memory observation, not a chain of shell launchers.

Problem: kernel speed omits first-use loading, durable commits and short-lived
children. Decision: first uploads include lazy imports, sealing and loading;
common authority setup precedes clocks. Upload/source fsync, chunks/index commit
and full public readback are timed. Raw CPU includes current-process and reaped
descendant work. Warmup owners retire before reopening for the measured cohort;
the first cohort call includes reactivation. Separate memory workers execute
the full workload and journal records until after counters. The external exit
observer records short child peaks while preserving original parent waits.
For interruption it explicitly observes threaded children: every exiting TID
contributes a snapshot, shared threads form one process peak, and original
producer wait receipts account for every descendant. Sibling processes must
be observed sequential; the memory sum adds ancestors along the largest path.
Missing identities, snapshots, waits or overlap evidence refuse qualification.
`system_native_ingest_thread_memory_20261015.json`
records paired real producer recovery, exact waits and observer-failure cleanup;
this small memory cohort establishes the mechanism, not full admission.
Alternatives rejected: preloading hides first-use cost; sampled RSS misses short
children; timing under ptrace contaminates latency evidence. Post-exec RSS
estimates and sealed bytes form a conservative sum, not a historical PSS peak.

The default contract requires three 100-sample pairs and 100 fresh cold pairs.
Reduced cohorts are pilots. Every run first verifies exact fixtures, original
bytes, replay/owner/rollback/reopen and joined cleanup using the same artifact.
Sources, artifact pins, process identities and original failure receipts remain
in the output directory. Unsupported child topology leaves memory missing and
the verdict incomplete; it never substitutes sampled or zero memory. The
interruption case is post-commit producer loss and recovery, not HTTP cancel.
Neither this domain collector nor a passing subset selects application defaults.

`system_native_ingest_owned_full_20261016.json`
retains all five cases, three full warm/memory pairs and 100 fresh cold pairs.
HTML and large-input measurements have no rejected metric; PDF warm p99,
batch cold p99 and interruption warm/cold tails retain regressions. No default
is admitted. Interruption clocks also include producer/recovery corpus setup
and repeated benchmark source fingerprint work; they do not isolate product
HTTP cancellation latency. Source-scope improvements require a new declared
cohort and must retain these original clocks and the earlier partial cohort.

Problem: the original interruption clock repeatedly scanned checkout provenance
in both the producer and recovery process, unlike the other upload cases.
Decision: clock scopes v2 and v3 verify the full database
source digest and engine/library bytes before and after each worker cohort.
The collector also checks the frozen database sources and artifact bytes around
every worker. A private immutable receipt carries those expected pins into the
owned producer; every opened runtime still compares its actual exported build
identity. Ordinary corpus callers keep their per-open checks.
Source-validation children remain inside memory observation and original-wait
capture even though their work is outside operation clocks. Producer startup,
authority initialization, lazy HTML admission, upload, kill/join, recovery and
duplicate retry remain timed. Alternatives rejected: dropping provenance checks
could certify changed code; caching mutable files without a final check could
hide artifact replacement; preloading the HTML projector would hide cold cost.
This corrects benchmark overhead, not product performance. Results under the
two clock scopes cannot establish a Rust speedup across versions. Earlier full
regressions remain evidence; fresh equivalent-work cohorts are required.
Scope `tofu.native-ingest-clock/v3` keeps the v2 operation boundary and streams
provenance hashing with bounded buffers. Problem: repeated whole-file source and
artifact reads can dominate process peak/retained RSS even outside latency clocks.
Decision: keep all source, byte-pin, exported-identity and original-wait checks,
while bounding their read buffers. Alternative rejected: removing those checks
would weaken evidence; attributing their allocator retention to Rust parsing
would misidentify the cost. Original v2 cohorts and every rejection remain
retained; this harness correction cannot be counted as product acceleration or
waive the need for a fresh, identically scoped full comparison.

Boundary verification
retains the original diagnostics, refused memory pilot and corrected all-case
pilot. The pilot checks the mechanism and does not admit performance.

The benchmark view delegates to `lib.knowledge.html_text`. It uses a
per-call lightweight tree, omits attributes, and preserves the canonical hidden
content, whitespace, headings, table-cell and entity rules. The knowledge HTML adapter uses this parser lazily; PDF extraction, repository
write and scheduling owners remain separate.

Run `python benchmarks/knowledge_html/check.py` for seeded exact differential
checks. Run `python benchmarks/knowledge_html/measure.py --output /tmp/knowledge-html.json`
for three alternating independent-process pairs on article, table and eight-file
batch workloads. Each variant has 100 observations after 20 warmups; memory uses
another process. Every output trace must match the independently compiled pre-change HTML
function from Git commit `8c62baae3` (see `reference.py`).

Timing covers the full knowledge extraction text/metadata operation. Fixture
construction and trace serialization are outside the latency/CPU samples. Peak
RSS includes imports, fixtures, warmups and output hashing; it is process residency,
not parser-only allocation. The cold operation now includes first lazy parser use,
and the reference comes from immutable Git source rather than the current adapter.

Additional exact entity/edge and retained HTML corpus checks are recorded in the
external run directory. Synthetic equivalence and warm gains do not complete
web/PDF, batching scheduler, cancellation, bounded memory or persistence admission.
Production promotion retains that independent oracle and validates the owning
adapter, lazy import, parser-fault fallback and full knowledge/backend gates.

`check_durable.py --engine /absolute/path/to/tofu-db` extends exact comparison
through the canonical `add_document` facade: original source writes, extraction,
chunking, search-term indexing, native atomic commit and public content read.
Each arm uses its own temporary embedded TofuDB authority. The engine must have
`libtofu_db.so` alongside it; its compiled source identity must match this
checkout. The application backend, runtime and data directories are untouched.
The check exercises duplicate reuse, exact command receipt replay, foreign-owner
denial, empty input and injected pre-commit failure cleanup, complete close/reopen
readback and the engine's offline integrity verifier. This is not a process-crash
or lost-ack fault certificate.

`durable_measure.py --engine /absolute/path/to/tofu-db --output /tmp/html-native.json`
uses 100 observations after 20 warmups and three alternating pairs for article,
table and eight-document batch cases. Memory uses another fresh process; both
arms execute the same native build. Every batch member has a distinct source
digest to force actual parsing/committing rather than duplicate lookup. Original
bytes and exact public content traces are verified outside timing; final content
is verified again after closing and reopening the authority. Raw CPU/wall samples,
fixture/source/library hashes and process identities remain in the external report.
Uploads include source fsync and native acknowledgement. Peak RSS includes native
initialization, corpus growth and reopen; retained-memory isolation, HTTP/download,
PDF, cancellation, crash recovery and scaled concurrency remain separate gaps.
Reduced `--samples`/`--pairs` runs are pilots and cannot establish admission.

`upload_phases_measure.py --engine /absolute/path/to/tofu-db --output /tmp/html-phases.json`
diagnoses article uploads with the same canonical worker, owned native authority,
fixtures and exact content checks. It wraps only private function namespaces and
owned repository methods to record digest lookup, extraction, chunking, visual
settings lookup, search-index construction, original source write/fsync, native
create and native/public content reads. The upload/read totals include nested
stages; do not sum totals with their children. Cold and 20 warmup observations
are excluded from the returned phase vectors, which align with raw wall samples.
Defaults retain three 100-sample alternating pairs and separate memory workers.
Instrumentation adds overhead: these results cannot override the uninstrumented
article P99 failures or establish admission. Engine/source identity checks still
apply, and final content is still read after closing and reopening the authority.

`check_tokens.py --engine /absolute/path/to/tofu-db` checks a bounded call-local
search-token candidate against the immutable `search_tokens` function from
commit `f2ea9f0ea`. It preserves first-seen term order, identifier components,
CJK bigram/trigram ordering, term lengths and cap semantics. Short repeated
words/runs share work only within one invocation; each memo has at most `cap`
entries and excludes source strings longer than the canonical term limit.
Saturated output returns immediately. The candidate has no retained cache and
does not modify production functions.

`token_measure.py --engine /absolute/path/to/tofu-db --output /tmp/knowledge-tokens.json`
uses the canonical actual upload/readback driver with the current fast HTML
parser in both arms. Only the search-token function differs. Do not compare
these ratios to historical-HTML results as if the baselines were identical.
The default 100 observations, three pairs and separate memory processes remain;
source bytes, public chunks/search text, receipt/owner/rollback/reopen behavior
and compiled native identity must match before any production decision.

`native_profile_measure.py --engine /absolute/path/to/tofu-db --output /tmp/html-native-profile.json`
runs current fast HTML plus the immutable current token baseline in every trial.
It follows the storage charter's profiler off/on/on/off order, in fresh worker
processes, with an independent memory process for each trial. Only the disposable
worker receives `TOFU_DB_REQUEST_PROFILE_EVERY`; the application is untouched.
On trials reconcile request spans/histograms/queue coverage with
`scripts/tofudb_profile_report.py`. Native dispatch profiles include cold and
warmup requests; raw upload samples exclude them. Slow sample numbers are native
local counters, not upload IDs. Observer overhead and native wall-time breakdowns
are diagnostic; they cannot waive uninstrumented tail failures or become CPU or
full end-to-end performance claims.

Problem: extraction-only results omit source durability, native indexing and
interruption before a receipt reaches the caller. Decision: `domain_measure.py`
measures actual native HTML/PDF/mixed/large ingestion and a real owned producer
interruption after commit, cold recovery and digest retry. Alternatives rejected:
exception-only fake cancellation does not test recovery; deleting committed
state on interruption violates durability; switching the application backend
contaminates the measurement. Synchronous ingestion has no independent
pre-commit cancellation protocol; this case does not claim one.

`DurableHtmlCorpus(initialize=False)` cold-recovers a caller-owned, previously
initialized fixture after its producer exits. Default initialization and owner
behavior remain unchanged. `check_interruption.py` proves document identity,
original bytes and exact content survive two killed/joined producers.
`check_ingest_domain.py` checks HTML, real four-page PDF, mixed batch and three
large HTML documents through upload/readback/reopen with all PDF admission
released. Run the default-owner `check_durable.py` after changing the harness.

Generate frozen PDF bytes with `fixture_pdf.py --output /tmp/ingest-fixture.pdf`.
Pass this file and `--engine /path/to/tofu-db --output-prefix /tmp/ingest-domain`
to `domain_measure.py`; first use `--samples 2 --pairs 1`, then its default
100-sample/three-pair cohorts. Every input is distinct by an appended HTML/PDF
comment, so parse/commit work is never hidden by digest reuse. Timing includes
original-source fsync, actual parser, chunk/token/index writes and public
readback; interruption includes producer startup, kill/join and native recovery.
Raw CPU accounts parent and reaped producer/CLI children, excluding the live
resource tracker. Separate memory workers sample aggregate descendant RSS at
5ms, include parent high-water RSS and report post-disposal positive RSS growth
from a warmed/disposed baseline. Short child peaks may be missed; one lifetime
is not long-run leak proof. Missing domains keep the whole-system goal open.

The complete v2 ingestion cohort retains all five cases with three 100-operation warm/memory pairs and 100 fresh cold samples per arm. The collector completed with exact outputs and joined cleanup, while four performance guards rejected the candidate. Bounded-provenance v3 requires its own full cohort; neither version erases the earlier negative evidence.

> Evidence note: the the former evidence/ artifact snapshots cited here were retired from the tree. The cited JSON artifacts remain recoverable from git history.
