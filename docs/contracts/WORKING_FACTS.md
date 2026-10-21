# Conversation working facts

The payload and bounds are defined in
[`working_facts_v1.schema.json`](../../contracts/working_facts_v1.schema.json).
The domain owner is `lib/memory/working_facts/`. Routes, tools and automatic
context are adapters to that owner, not independent stores or execution loops.

## Problem, decision and alternatives

Repeated source reads spend context and time, while summaries can silently keep
claims whose requirements changed. Conversation working facts preserve exact
source versions and derive dependency freshness on each acquisition. They remain
separate from reusable long-term memory. Treating a model summary as verified
truth was rejected: an exact observation, an inference and an owner assessment
have different meanings. Blanket invalidation was rejected because independent
observations remain usable after an unrelated correction.

## Meaning and dependencies

An observation exactly quotes one supported source with its declared temporality. A derived claim
always needs review; persistence does not verify it. Owner confirmation preserves
its sources and records an assessment, not an independently proven result.
Owner correction creates a new owner assertion and replaces its dependencies;
owner retraction withdraws it. All three increment the fact revision. Agents
cannot overwrite a record last decided by its owner, including a retraction.
A new sourced proposal remains possible.

Supported sources are an immutable conversation artifact's SHA-256 and exact
quote, an artifact or ordinary assistant Turn acceptance criterion's generation digest and full text, a
host-issued physical project file's SHA-256 and exact quote, a host-issued canonical
tool result's historical execution and exact quote, or another fact's ID and revision. Bindings form an acyclic graph. Changing a source
revision makes dependent facts stale transitively; unrelated facts remain
usable. Missing or inaccessible sources become unavailable. Unreviewed
inference propagates `needs_review`. Artifact versions describe historical
content, not the latest state of a file or the outside world.

Capture checks artifact bodies once. Subsequent reads check owned metadata and
criterion generations, with request-local deduplication; they do not reread
unchanged artifact bodies. A timeout or failed source query is not evidence of
withdrawal or success. No missing source silently deletes durable facts.

Criterion sources default to artifact ownership. Optional `targetKind: "turn"`
selects an ordinary main-lane assistant Turn in the same owned conversation;
`id` then identifies that Turn and `criterionId` its requirement. The common
generation and exact-quote checks still apply. Removing and re-adding an equal
requirement does not revive an old binding. Artifact and Turn namespaces have
separate request-local cache keys.

Problem: changing a task requirement could not invalidate an accepted artifact
whose evidence depended on that requirement. Decision: extend the existing
criterion source and reuse `lib.acceptance.turns.read_requirements`, which checks
ownership without recursively reading decisions or facts. An artifact review can
bind the resulting fact revision; changing the requirement makes that evidence
stale while retaining independent accepted outputs. Alternatives rejected: copying
task requirements into artifact requirements creates two authorities; a separate
dependency graph duplicates freshness, CAS and owner boundaries. This does not
authorize retries or make acceptance an automatic model judgment.

## Ownership, storage and commands

One selected-store record aggregate belongs to an explicit user, optional tenant
and conversation. Every operation validates conversation ownership. The
repository uses `record.get`/`record.put` with expected-version CAS, never direct
SQL, another backend, or a module-global user. Lost write acknowledgments are
accepted only after exact revision and commit-digest readback. Otherwise callers
must query; there is no blind retry or exactly-once claim.

`GET/POST /api/v4/conversations/{conv_id}/working-facts` uses the normal `chat`
authority. POST accepts a proposal or an explicit owner confirm/correct/retract
with a note and aggregate revision. Stale revisions return 409, missing/foreign
scope 404, invalid requests 400 and forbidden owner decisions 403.
Mutation responses contain only the selected fact. Refresh the full view to
retain independent facts and reevaluate transitive source states after a decision.

Discoverable tools `read_working_facts` and `record_working_fact` use the common
gateway, write approval and settlement. Read is fresh, read-only and available
to programmatic execution; write is not programmatic. Owner decisions are not
model tools. A record omits `id` to create a server-assigned identity; a supplied
`id` updates only an existing fact. Observation text equals the source quote,
without attribution or paraphrase; derived claims retain review requirements.

Problem: an invented update ID was reported like unavailable storage, making
repairable proposals look like outages. Decision: tool failures distinguish
missing targets, invalid proposals, conflicts, forbidden owner overwrites and
unavailable outcomes, with recovery advice from the tool contract. HTTP retains
404 for missing/foreign scope or targets, 400 for invalid proposals, 409 for
conflicts and 403 for forbidden decisions. Unknown write settlement remains
non-retryable until readback. Alternatives rejected: accepting arbitrary IDs or
paraphrased observations weakens identity/evidence semantics; automatically
replaying writes risks duplicate effects.

Every tool result carries fact IDs/revisions; records also carry
the producer task identity. These facts never confer execution authorization.

## Context, lifecycle and bounds

The existing ROOT preparation boundary refreshes facts before prompt admission,
including the native adapter. Acquisition uses the shared context provider
executor and immutable scope, respects cancellation/deadlines, and cannot mutate
a task from a late result. Open tool batches and transient invocations acquire
nothing. One registered evidence block uses the common renderer, context budget,
content-addressed reuse, supersession and Inspector manifest. Selected fact IDs,
revisions, freshness and consuming task identity accompany the manifest.

Unchanged snapshots append no prompt bytes. Corrections supersede older
snapshots; failed acquisition injects an explicit unknown-freshness notice.
Compaction can reacquire the current view from durable state. A bounded newest
selection includes whole facts and reports omitted count. Final model admission
retains its existing authority.

Current protocol limits are 256 records per conversation, eight sources per
record, 8 MiB aggregate storage, 128 KiB HTTP command, 4,096 tokens per automatic
snapshot and 15 seconds per acquisition. Limits bound one semantic record and
prompt contribution, not worker concurrency or machine throughput. Capacity
refusal preserves existing evidence; there is no automatic durable-state eviction.
No new pool, timer, background refresh or persistent cache is created. Aggregate
reads and source checks scale with the bounded graph; artifact criterion reads
also inherit the artifact ancestry traversal cost.
Turn criterion resolution reads one owned Turn and one requirement record per
distinct Turn per acquisition, regardless of the number of referenced criteria.
The owned Turn read includes its projection; no persistent cache or worker is
added. `test_working_turn_criteria.py` pins request-local deduplication, both-store
restart behavior, ownership, acquisition failure and selective invalidation.

Only current fact revisions are stored; this is not an append-only review
history. Current external/network state, independent acceptance and whole-task
verification remain outside this contract. Historical tool observations never
certify that the external state remains unchanged.

## Physical file sources

Problem: an immutable artifact can remain valid after its originating file has
changed. Decision: opt-in `read_files(bind_sources=true)` issues `{kind: file,
id, version, path}` through the existing authorized project read owner. Add an
exact UTF-8 `quote` when proposing a fact. The default read has unchanged behavior
and cost. Ranges still control displayed lines; the digest covers the entire
file. Binding proves source provenance, not that a provider consumed every byte.
Actual working-fact consumption remains the shared admission/return evidence below.

A request-owned host capture stages metadata from descriptor-pinned snapshots.
Only the existing successful terminal tool boundary publishes those witnesses.
Problem: caching the content-only speculative read result bypasses the canonical
handler and silently omits requested source descriptors. Decision: source-bound
reads are announced during streaming but execute through the canonical handler
after the response is accepted; ordinary reads retain speculative execution.
Alternative rejected: a second speculative witness/settlement owner would split
source authority from the shared handler.
Owner, tenant, conversation, task, call, physical root identity and digest remain
bound. Cancellation, failed settlement, changed scope and forged JSON cannot mint
witnesses. Scoped file dispatch preserves its exact path/ownership fence; local
project absolute paths use the same verified roots. Remote transports, converters,
attachments, binary/invalid UTF-8 and files outside verified roots issue no source.
An unsupported or failed read never silently becomes evidence.

Admission checks the issuing task's current read scope, the witnessed whole-file
digest and the exact quoted bytes. The selected quote and private root/call metadata
are committed with the fact in the same aggregate CAS. HTTP may reuse an existing
exact seal but cannot introduce arbitrary paths or probe new quotes. Current seals
survive a selected-store restart; new quotes need a new authorized read if the
transient witness expired. Correcting/replacing facts prunes unused seals; inactive
facts do not read live files. A physical root replacement, symlink, missing file or
read race makes the source unavailable. A different digest makes it stale, including
all transitive fact and recorded task dependencies. A removed execution worktree is
not silently remapped onto another checkout.

The witness bank uses the existing launch-probed tool-receipt capacity, retains
metadata only, and is released by the task's terminal heavy-state owner. Overflow
requires reacquisition; it never evicts durable facts. Each fresh query hashes a
physical file once, with bounded streaming memory and the existing acquisition
deadline. There is no watcher, new thread pool, polling timer or copied body archive.
The shared descriptor reader belongs to `lib/artifacts/file_snapshot.py`; publication
retains its previous cancellation/path mutation behavior through that same owner.

Alternatives rejected: raw paths in the fact API would introduce a new file-read
authority; stat-only freshness can miss changes; copying whole source bodies into
a new archive would duplicate durable storage and require another retention owner.
This implementation deliberately pays whole-file hashing for exact freshness.

Measured on warm local files, three repetitions with `tracemalloc` enabled (2026-10-15):

| File | Ordinary full read | Bound full read | Later digest check | Digest-check peak memory |
|---|---:|---:|---:|---:|
| 1 MiB | 2.02 ms | 5.15 ms | 1.71 ms | 130 KiB |
| 4 MiB | 7.85 ms | 15.39 ms | 4.78 ms | 130 KiB |
| 64 MiB | 153.25 ms | 281.49 ms | 70.40 ms | 130 KiB |

The 64 MiB initial bound read peaked at 192 MiB versus 128 MiB for the ordinary
read; full bytes exist only during that explicit read. Each measured issued receipt
was 352 serialized bytes, independent of file size (scope/path lengths vary).
These are local read costs, not an end-to-end speedup or a token/correctness result.
The source-body-free aggregate and bounded stream memory are tested independently.

## Historical tool sources

Problem: model-visible tool text may be a preview, a cached answer or a compacted
summary. Decision: the existing executor issues a task-local start witness only
when dispatch begins; shared canonical settlement can issue a `tool_result`
descriptor after successful execution. The descriptor binds owner, tenant,
conversation, task, tool/call identity, settlement observation time, source coverage and
canonical version. Failed/rejected/cancelled calls, cache replay, terminal tasks,
changed scope and incomplete retention issue no descriptor. Upstream completeness
is independent: `unknown` or `partial` coverage remains visible.

`read_working_facts(include_tool_sources=true)` exposes bounded newest-first
metadata pages. A new fact supplies an unchanged descriptor plus an exact quote.
For inline results, the retained existing round must still match the original
canonical SHA-256. For spilled/composed results, the owner-scoped canonical
artifact query verifies the quote. Read pages identify immutable segments; a
quote can span pages of one segment but cannot combine separate parts or children,
even when child call IDs repeat. Normalized previews and advisory origin labels
cannot mint source authority. An evicted or compacted unsealed carrier requires
a new authorized observation; there is no implicit tool rerun.

Admission seals only the selected quote and provenance with the fact in its
existing CAS. HTTP can reuse an exact seal, but cannot issue a task witness or
probe arbitrary new quotes. Sealed historical observations survive task teardown,
compaction, restart and original archive expiry; expiry prevents new quotations.
The UI displays the tool/call, observation time, source coverage and an explicit
historical-observation notice. To track mutable input, pair the observation with
live file/requirement dependencies. User corrections retain the existing targeted
transitive invalidation and task-impact semantics.

Alternatives rejected: copying every result into a second durable evidence store
would duplicate content and retention; trusting arbitrary tool-message JSON would
let previews fabricate provenance; treating the latest tool observation as current
external truth would conceal later state changes. These sources add no grants,
execution loops, workers, polling or source-body cache. Start/settlement banks use
the existing launch-probed tool receipt capacity and shared source lock, are
metadata-only, and are released at terminal task settlement. Unreferenced private
seals are pruned with the same fact mutation.

Measured metadata admission (`begin` + shared `settle`, 100 repetitions,
launch-resolved receipt capacity 256, `tracemalloc` enabled, 2026-10-15):

| Existing canonical body | Median admission | Serialized receipt | Retained delta for 100 receipts |
|---|---:|---:|---:|
| 1 KiB inline | 0.120 ms | 390 bytes | 69 KiB |
| 64 KiB inline | 0.165 ms | 390 bytes | 65 KiB |
| 1 MiB inline | 0.943 ms | 390 bytes | 65 KiB |
| 8 MiB already archived | 0.126 ms | 466 bytes | 65 KiB |

The inline 1 MiB case temporarily allocated about 1.06 MiB to encode/hash the
existing text; the archived case did not copy the body. Scope/call lengths affect
receipt size. A standalone process without materialized launch budgets measured
2.60–3.44 ms because budget fallback probing repeated; normal launch materializes
the existing budget. These are local admission costs, not task latency, model
cost savings or evidence of complete source coverage.

## Task dependency evidence

Problem: fact-to-fact invalidation did not identify tasks exposed to an older
basis, and Inspector snapshots expire. Decision: retain one owner/tenant/
conversation/task record in the existing selected-store record authority.
The shared prompt-admission boundary records potential consumption of a complete,
hash-matched working-fact block before dispatch. Final request observation checks
the actual block bytes again; only the existing verified provider-return owner
confirms that exact prepared set. Python fallback/rescue observes sealed physical
input through the existing request lease; native context observes its owned
admitted history. Both use the same dependency service. Suppressed, compacted-away and altered blocks cannot
borrow a manifest's old receipt. Composer acquisition remains read-only.

Preparation derives the block hash from the admitted message contents. A task's
cached hash map is not evidence: list identity and length stay unchanged when a
message or its text is replaced. Render-local reuse remains local to rendering;
passing that unowned map across the dependency boundary was rejected because it
can bind facts the request no longer contains. The later wire check is retained
as an independent confirmation boundary, not a substitute for correct admission.

A crash, cancellation, unavailable confirmation or replaced pending request keeps
explicit uncertain consumption. A new successful request cannot confirm an
older unknown request, nor can a revision between recorded extrema be invented.
Lost storage acknowledgements use exact commit-digest readback. Confirmations
never rerun a provider call. Unchanged acknowledged bindings add no storage work
while the task's existing lifecycle retains its scope-bound receipt.

`GET /api/v4/conversations/{conv_id}/working-facts/consumers` uses normal `chat`
authorization and returns bounded cursor pages. It evaluates recorded revision
extrema against current fact/source versions, including transitive changes.
Changed, withdrawn, unavailable or unreviewed bases require revalidation;
independent current bases retain their status. Revision extrema summarize actual
observations; they do not claim that every intermediate revision was observed.
The response declares coverage: admitted automatic working-fact context since
enablement, not historical tasks or direct tool-result consumption. Pages are
live reads, not an atomic whole-conversation snapshot. Missing evidence never
proves a task unaffected or accepted.

Each task retains at most the conversation's 256 fact identities and the existing
8 MiB record ceiling; task count is paginated rather than capped. Records contain
identities/revision extrema and one pending set, not bodies or per-round history.
They share the conversation's ownership/access lifecycle; deletion hides them and
restoration makes them accessible again. No worker, timer or independent cache
is introduced. Owner correction does not erase output or authorize automatic
tool reexecution. Later acceptance must bind the resulting deliverable versions.

Alternatives rejected: expiring snapshots cannot prove durable dependencies;
indexing acquisition before admission includes unused facts; confirmation on
request construction mislabels interrupted calls; blanket task invalidation
discards unrelated evidence. Prepared/confirmed records preserve uncertainty
without changing task execution or acceptance authority.

A 15-record-per-population SQLite/native mechanism probe stored 391–392 bytes
for one dependency and 8,553–8,554 bytes for 256 dependencies, using synthetic
seven-character fact IDs. Duplicate confirmation retained the exact row/version;
unchanged task-local acknowledged bindings added no consumer repository calls.
These are serialized record sizes, not WAL footprint, model cost or latency
admission. The independent source-refresh cost remains. Raw receipt digests,
source identities and timing limits are in
`working_fact_consumers_20261015.json`.

## Owner interface

Problem: source corrections and consumer impact were available only through the
API. Decision: the existing conversation bar's Context disclosure opens **Current
basis**, a demand-loaded native UI-kit dialog. `frontend/src/features/working-facts/`
projects schema-generated DTOs, source versions/quotes/status and owner decisions;
the existing context meter owns navigation/disposal. A separate task dashboard,
another polling loop and inferred task acceptance were rejected alternatives.

Corrections, confirmations and withdrawals require an owner note and the displayed
aggregate revision. Conflict or uncertain acknowledgement preserves the draft and
disables further decisions until an explicit reload; writes are never replayed.
Reload displays current facts alongside preserved edits. A confirmed decision
invalidates the displayed impact and rereads it; impact-query failure does not
undo the decision or leave the previous impact looking current. Closing or changing
conversation aborts reads/writes and fences late responses; an aborted write still
requires a fresh read on reopening because cancellation cannot undo a server commit.

Fact pages retain at most 20 summaries, consumer pages at most 50; source/dependency
details mount only on expansion. Drafts are bounded by the server's 256-fact limit
and die with the dialog. There are no startup reads, timers or browser persistence.
Task impact exposes changed, unchanged and uncertain recorded inputs separately,
coverage limits and live page revisions. Missing records never prove a task is
unaffected; no task is rerun, accepted or erased by this surface.

Browser verification: `tests/visual/test_visual_working_facts.py` exercises the
actual application in three themes, Chinese/English and desktop/mobile widths,
owner correction, transitive impact, independent inputs, unknown consumption,
durable reload, concurrent edits and teardown. Focused request/draft boundaries
live in `frontend/src/features/working-facts/panel.test.ts`; generated DTO drift is
checked by `scripts/gen_working_facts_contract.py --check`.

## Verification

Run `tests/memory/test_working_facts*.py` for SQLite/native reload, lost ACK,
source ownership, exact quoting, targeted/transitive invalidation, owner
protection, actual v4 requests, context budgets/cancellation, native prompt
admission and common gateway settlement. Neighboring gates cover tool inventory,
write-risk rendering, API/OpenAPI and the common composer. Performance evidence
must report body reads, metadata reads, prompt tokens and acquisition latency;
these mechanism measurements do not establish paired model task success.
Consumer records additionally use `tests/memory/test_working_fact_consumers.py`
for reload, exact confirmation, targeted impact, owned pagination and real v4
authorization; native/ordinary round tests verify the shared admission and
provider-return boundaries.

A 15-sample warm local measurement with one 175,017-byte artifact (39,960
tokens) and 256 facts measured SQLite/native query p95 of 8.40/8.16 ms and
whole-fact selection p95 of 1.90/1.96 ms. Each query made two conversation
checks, one aggregate read and one artifact metadata read, with zero body reads.
The selected context used 3,913/4,027 tokens and about 28 KiB peak traced Python
selection allocations. A fresh one-fact context used 183/177 tokens. These are
synthetic mechanism results; they exclude cold startup and do not claim a
model-level success-rate or cost improvement. Identical context adds no tokens.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->
