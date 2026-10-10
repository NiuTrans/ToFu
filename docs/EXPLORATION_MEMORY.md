# Exploration archive memory

Authority: `lib/evolution/semantic_memory.py` and the semantic definitions in
[`evolution_program_v1.schema.json`](../contracts/evolution_program_v1.schema.json).
Program progression and immutable evidence belong to
[`EVOLUTION_DESIGN.md`](EVOLUTION_DESIGN.md); execution budgets belong to
[`RUN_CONTINUITY.md`](RUN_CONTINUITY.md).

## Problem and decision

Recent context previews cannot recover an old counterexample phrased in another
language. Literal recall remains useful and free of model consumption, but
paraphrases require a relevance judgment. The current experiment executor can
request `evolution_explore action=semantic_recall` with a query. One ordinary,
read-only child invocation ranks a bounded window of complete archived receipts.
It inherits the parent's authenticated model route, cancellation and shared
consumption account. It has no tools or project access. There is no independent
model loop, background indexer or new resident worker.

Alternatives considered: keyword search alone misses meaning across languages;
copying the entire archive into each stage grows context without bound; an
embedding service requires another model route, index namespace, invalidation
policy and budget owner. On-demand relevance uses the existing invocation and
account boundaries. It trades extra model calls for an explicit, resumable scan
and cannot promise the latency of a prebuilt semantic index.

**Problem:** a receipt may contain only a 24,000-character preview, hiding a
late counterexample despite a complete terminal archive. **Decision:** explicit
`source_kind=deliverable` or `tool_evidence` scans frozen originals; default
`receipt` preserves JSON receipt/review retrieval and existing cache identities.
Diagnosis can also use `action=read source_kind=deliverable|tool_evidence|artifact_evidence|background_evidence` for exact 6,000-character original pages without a ranking model. Follow `next_offset` with the returned `source_digest`; missing, foreign or changed originals fail instead of returning previews. This avoids requiring semantic search merely to inspect a known failed experiment; read access grants no verification, replay or learning authority. Archive read/recall (including cached semantic results), mission and validation-context delivery recheck the current executor and program after I/O; cancellation or executor replacement refuses delivery, while pause still permits evidence reads. Start-only authorization was rejected because a slow read can outlive its executor.
Keep the same source kind when following `{after,offset}` cursors. Each source
kind is a separate search scope: searching one does not cover the others.
**Alternatives:** enlarging receipt headers duplicates immutable evidence and
context; using the preview as a missing original fabricates coverage. Original
acquisition instead uses the existing owner-bound outcome pages, without a
second archive, worker, model loop or persistent index.

**Problem:** successful short deliveries formerly lacked `result_source` even
when the complete original was sealed. **Decision:** new program receipts bind
short originals to the last executing receipt task, with exact Unicode length
and UTF-8 digest equality. `receipt_sources.py` owns this resolution. For older
receipts, frozen tool/artifact references must name the same executor and match
its sealed metadata; inline text must equal its entire delivery by length and
digest. Original pages supply content. Existing receipts are never rewritten,
including settlement after a receipt was committed before the header advanced.
**Alternatives:** inline preview substitution lacks original provenance; a
bulk migration changes immutable evidence; forcing short run reviews into
source-tool mode changes review policy. Missing provenance stays unavailable;
conflicting references, foreign stages and damaged originals refuse ranking.

## Continuation input and bounded previews

**Problem:** complete legal goals, acceptance and future hypotheses, or escaped
history/error prose, can exceed a 24,000-character launch seed. **Decision:**
new seeds budget their serialized size. They retain six recent sequence/run/
outcome, uncertainty and original-source pointers while shortening result/error
previews with explicit truncation flags. Full receipts are unchanged. When the
mission and hypothesis alone cannot fit, `current_context_source` binds their
complete canonical JSON by digest and character length; it is required input,
not a summary. The executor reads `evolution_explore action=read_context`, pages
of at most 6,000 characters, carrying `source_digest` after the first page.
The read projects the immutable program objective/acceptance and admitted
current step; it checks owner, current run/task and executor role. Pause permits
reads. Changed digests and stale/foreign tasks refuse. Reading never dispatches
a model, writes a record or grants learning/verification rights. Existing frozen
launch seeds stay exact. Complete inline mission input can be used directly;
optional reads resolve missing context or evidence gaps. `evolution_source` reads
settled experiments, not the current unfinished stage. After required checks,
the executor delivers through the existing terminal answer port. Final audit
requirements still use the full originals.
**Alternatives:** rejecting valid large inputs stalls exploration; increasing
every stage's context grows recurring cost; truncating requirements changes the
goal. Bounded presentation plus original paging preserves both mission and
archive without another store, worker or summary authority.

## Coverage and evidence

Each window reads at most 50 receipts and supplies at most 24,000 Unicode
characters, split at 6,000-character boundaries. Only one complete receipt is
resident during archive acquisition. The structured continuation cursor carries
`after` and `offset`; advancing it visits every character, including long
reviews and failed results. For receipt scope, `scanned_through` counts completely read receipts;
a non-null cursor indicates more evidence remains. A window with no match does
not establish absence from the archive. Original scopes visit up to 50 receipts
and read up to 24,000 characters in 6,000-character pages; only these pages and
one receipt/metadata pair are resident. Frozen task, source kind, full digest
and Unicode length must match before ranking. Unsealed, missing or damaged
referenced originals refuse ranking. A receipt lacking provable original provenance is
counted in `sources_unavailable` for that window and skipped; it is never
substituted by inline text. Empty source windows return their continuation
cursor, perform no model request, and cannot establish absence.

**Problem:** a real provider returned a quote longer than the 240-character
contract despite an explicit instruction and JSON-object output mode; the
request consumed its shared budget before validation refused it. **Decision:**
the child selects a candidate and an `evidence_id` instead of generating a
quote. Each complete chunk is presented once as consecutive numbered parts of
at most 240 Unicode characters, deriving that width from the canonical quote
contract. Adjacent parts concatenate to the exact original, with no omitted
characters. Relevance still considers the whole chunk; its returned quotation
is the exact selected part. This changes quote granularity, which can split a
sentence and must not be mistaken for the full supporting context. Callers
inspect the original chunk/source before using the judgment.
**Alternatives:** increasing the bound or truncating a model-authored quote
weakens the evidence contract; requiring provider-specific schema enforcement
adds a route capability requirement while still failing to establish source
correspondence; retrying adds consumption without guaranteeing correct
copying. Explicit evidence selection uses the same single ordinary invocation
and parent budget, without depending on provider schema enforcement.

The child must produce strict JSON with at most eight supplied candidate IDs
and their supplied evidence IDs. Duplicate JSON keys, duplicate/absent
candidate IDs, absent evidence IDs, nonfinite/out-of-range relevance and
malformed output are rejected without retry or repair. The resolver takes the
selected exact original part and revalidates the existing durable rank/cache
contract, including the 240-character quote bound and source correspondence.
The ranking policy version advances to 2, separating new cache identities from
old judgments; earlier cache/history evidence and consumption are not rewritten.
Sequence, chunk offsets and a SHA-256 digest of the complete canonical receipt
bind each receipt hit to its source. `read` uses the same canonical serialization
and offsets. Original hits additionally retain `source_kind`, `run_id` and
`source_ref`; their offsets address the raw frozen source, never receipt JSON.
Inspect them with `evolution_source` using that kind and reference. Source text and queries cannot grant instructions or privileges.

A valid quote proves source correspondence. It does not prove the source's
claim, consumer demand, causality, novelty or product value. Relevance and its
explanation remain unverified judgments. Full receipts, underlying task evidence
and independent reviews retain their existing authority. Recall creates no
learning rights, verdicts, accepted hypotheses or frontier changes.

## Consumption, cache and lifecycle

Only the current bound executor can invoke semantic recall. Active status is
rechecked before a new ranking. Pause allows an existing cached read but refuses
new consumption. Parent Stop is checked before child creation and through the
ordinary invocation's cancellation port. Failed or undelivered child results
cannot become successful ranking entries.

Every dispatched physical model attempt uses ordinary budget admission. Unknown
usage remains incomplete and fences later finite-budget requests; failed output
validation does not refund a dispatched request. Ranking actuals displayed on
a cache hit describe the original request and never add new consumption.

The owner/program record cache is reconstructible, separate from immutable
receipts. Its key binds the query, complete-source digests, chunk offsets, model
selection and policy version; original identities also bind task/source kind
and the complete frozen reference. It retains the latest 32 computed rankings within
512 KiB; cache hits do not manufacture new creation times. `refresh=true`
explicitly recomputes. Concurrent cache CAS refusal or storage unavailability
preserves already delivered evidence and reports `cache_persisted=false` with
the canonical storage code. It never hides original consumption or erases the
archive. Concurrent misses may each consume a request; there is no extra worker
or cross-process in-flight authority.

The history GET `/api/v4/evolution/programs/{id}/memory` reads only; it cannot
trigger ranking. It returns at most 32 entries and projects source metadata to
the eight matched chunks while retaining whole-window coverage counts. Ownership
is checked before cache access. The notebook shows original costs (a lower bound
when incomplete), exact quotes and buttons that page source receipts. Polling
preserves open details and focus; unavailable history leaves program progress
and receipt reads usable. No model request is started by opening this UI. Original-source hits offer an
explicit opener at the exact quoted chunk offset through the shared outcome
reader. Opening another hit in the same original selects its requested offset;
failed reads retain the admitted page and retry that offset and identity. Receipt inspection remains available
separately.

## Verification

`tests/evolution/test_evolution_semantic_memory.py` covers complete chunk traversal,
source validation, cache invalidation and retention, pause/owner/Stop boundaries,
unknown consumption and cache failure. Frozen originals cover late Unicode
counterexamples beyond previews, separate tool sources, missing references and
changed/damaged source refusal before any ranking. Its controlled HTTP test runs the ordinary
child engine, real streaming ingress, budget sealing and actual settlement.
`tests/api/test_api_v1_evolution.py` checks the authenticated read envelope.
`frontend/src/features/evolution/testing/programs.test.cjs` checks exact text,
source paging, partial failure, polling focus and route races. Real browser
journeys live in `tests/evolution/test_evolution_program_browser.py`.

A synthetic owner-scoped record measurement retained 32 rankings after 36
distinct queries in 19,528 serialized bytes. Two hundred repeated cache reads
created no additional ranking; peak traced allocation was 121,744 bytes.
Median/p99 cache-read times with allocation tracing were 55.90/60.39 ms.
These include schema validation and the in-memory record seam, exclude provider
and persistent-storage latency, and establish neither retrieval quality nor
production throughput. The byte ceiling also applies to larger entries;
immutable source receipts are never part of cache eviction.

Synthetic semantic judgments test plumbing, not retrieval quality. Quality
evaluation requires real model outputs against labeled held-out queries, both
positive and negative evidence, coverage, measured cost and failure accounting.
Consumer value requires separate problem evidence and measured baseline lift.

On 2026-10-01, real GLM-5.3 outputs were evaluated against the existing eight
synthetic failure receipts and twelve fixed labeled queries (eleven positive, one
negative). The ordinary current-source AgentRuntime used this module's exact
ranking prompt and parser with tools disabled. Complete returned source sets
matched nine queries, versus two for literal lookup. The first hit matched all
eleven positive queries; the negative query returned no hits. All quoted spans
passed exact source correspondence. Three outputs included additional related
evidence; two linked ambiguous quota consumption with ambiguous message delivery.
Those analogies may suggest experiments but cannot prove the queried problem.
The fixed source receipts and labels are retained
in [the evaluation fixture](../tests/fixtures/exploration_memory_retrieval_20261001.json).

Provider-reported usage totaled 37,625 input and 1,033 output tokens, counting
cached input in the total. Median end-to-end time was 3.79 seconds per query.
This is one small synthetic retrieval evaluation, not a production child-flow,
durability, billing, demand, novelty, or consumer-value result. The transient
runtime's prelude excludes durable background receipt recovery; native roots
retain it. Reproduction requires the admitted model route and secrets through
the normal owner boundary, without recording credentials in evaluation artifacts.

## Research prototypes and admission boundary

**Problem:** an inspected research draft attributed a historical incident to a
newer baseline, and unresolved evidence can outlive an individual invocation.
**Decision:** `evaluations/evolution/decision_aids.py` provides model-free local
prototypes for declared claim-impact suggestions and prerequisite reviewability.
They are evaluation infrastructure; production does not import them.
**Alternatives:** careful existing archive/source inspection and complete trace
scanning remain valid comparators. Equal declared-graph results provide no new
correct decisions, and processing savings exclude unmeasured mapping/adoption
costs. Do not admit a candidate from those savings alone.

Run `python -m evaluations.evolution.decision_aids INPUT.json OUTPUT.json` with
a bounded JSON graph, explicit principal/namespace and locally supplied events.
The output is a new artifact. It never calls a model, mutates archives, rewrites
historical judgments or authorizes execution. A satisfied prerequisite means
ready for review only; revocation, partial observations, cancellation and expiry
remain separate conditions. Event attestations are unverified inputs.

Missing named dependencies and cycles refuse. An unstated real dependency is
undetectable from graph structure, so semantic coverage stays unverified and no
claim is certified unaffected. Independent dependency labels, trustworthy
instrument/provider attestations and measured total user benefit remain
prerequisites before production admission. Verify with
`pytest -q tests/evaluations/test_evolution_decision_aids.py`.

Receipt pagination binds an optional `source_digest` on every requested page.
Problem: a supplied digest was previously ignored during receipt reads. Decision:
reject digest mismatch and missing or different requested sequences with typed
errors before returning content; never substitute the next available receipt.
Alternatives: silently ignore the digest or use a later record; both break
diagnosis provenance. First reads can discover the digest; continuation callers
should carry it with `next_offset`. This adds no persistent state or workers.

Semantic windows lazily read receipts after one owner-scoped program authority
read. Problem: each visited receipt previously reread the full program header.
Decision: share a bounded sequence iterator with ordinary history pages; stop
fetching when the character window fills and preserve exact continuation cursors.
Alternatives: prefetch 50 receipts or retain a history cache; both add unnecessary
memory or invalidation state. There is no prefetch, worker or persistent cache.
Missing or mismatched receipt identities refuse rather than skip evidence.
A hermetic 50-receipt window verifies 50 receipt reads and one program-header
read, versus 50 headers previously; this measures authority calls, not wall-clock
latency or resident memory. Early-stopped iterators perform no prefetch.

Background semantic scope

Problem: late owned command completions were archived but unavailable as an
explicit semantic search scope. Decision: `background_evidence` searches the
complete sealed background join through the existing original-source reader.
Its run/task/digest/Unicode length remain exact; missing references report
unavailability, changed or unsealed originals refuse before model spending.
Cache identity includes source kind and exact chunk manifest; other scopes are
separate searches. Alternatives: rank the short delivery or merge scopes without
provenance; both hide late counterevidence or falsely imply complete coverage.
Window/cache bounds, parent budget, pause/cancel and ownership fences are shared;
this adds no worker, persistent namespace or eager index.
The notebook memory history labels background scope explicitly and opens the
matching original at its recorded character offset, preserving source identity.

Shared goal-tool admission rejects locally cancelled or terminal carriers before
reading durable run authority, including cache-only exploration requests. Problem:
run reconciliation can lag task cancellation/completion. Decision: reuse the task
engine's terminal status set and the task's abort flag/event at the shared tool
boundary; existing archival and settlement paths retain their own authority.
Alternatives: trust a still-executing run or add checks to each tool; the former
permits stale proposals and the latter drifts. Paused live tasks can still inspect
evidence; this adds no storage call, resident state or worker.
