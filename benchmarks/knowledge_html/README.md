# Knowledge HTML projection experiment

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
