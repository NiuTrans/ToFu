# Canonical tool result queries

**Verbatim read text.** Problem: JSON-string escaping adds model tokens to code,
quoted documents and newline-heavy artifact bodies after page metadata is already
compacted. Decision: the declared read projection can emit one compact metadata
line followed by `content (text; N chars):` and the exact body. `N` counts Unicode
codepoints, including CR/LF and tabs. Indexed batches retain ordered per-page
sections, error entries and every unknown metadata field. The complete candidate
must use fewer UTF-8 bytes than the prior compact JSON; otherwise JSON remains.
Non-display control characters also retain JSON. Body text is never interpreted
as application JSON or as another section.

Canonical capture, decoded program results and frontend metadata retain the
original authority page. Normal V2 budgets count the selected model view before
fitting, including metadata and signed continuation. Common error framing keeps
partial failures readable without changing their failed settlement. Legacy
result mode retains its existing producer JSON delivery. No source query, token
limit, cursor, storage record, worker or cache is added. Alternatives rejected:
omitting provenance or cursors removes recovery; raising budgets preserves the
escaping cost; locally clipping a body can strand evidence behind its cursor.
Verify `test_artifact_read_text_projection` and `test_artifact_read_text_backends`
through both storage authorities, ordinary settlement, reopen/replay, owner
denial and OpenAI/Responses/Anthropic request encoding.

A fixed five-receipt capture/budget/delivery probe preserved complete views and
canonical hashes while exact `o200k_base` counts fell from 4,554 to 3,838 (15.7%).
The code and mixed-failure samples fell 20.2% and 18.3%; short and unescaped prose
were byte-identical. This is a component payload measurement, not paid task
savings or a latency claim. Formatting uses transient strings under the existing
result bounds, without another retention or concurrency owner.

Canonical tool-result reads/searches share `contracts/tool_result_query_v1.json`
with the SQL authority. `artifact_query.rs` walks bounded immutable manifests,
retains child provenance/media limitations, applies JSON pointers, and signs
continuations bound to owner/root/operation/query/selector/observed expiry. Raw
segments retain both codepoint and byte positions so resumed pages use verified
blob ranges without replaying prior body prefixes. Each provenance span includes
its root-relative segment route; repeated child labels cannot join separate quotes.
A page with insufficient bytes for its next Unicode codepoint returns immediately.

Problem: plain-blob-only reads could not resolve canonical result trees and numeric
search cursors could not continue them. Decision: extend the existing native
artifact owner and IR with the common contract. A Python-specific walker or a SQL
fallback was rejected because either would create a second native authority path.
The persistent HMAC key is one 32-byte owner-scoped Entity value plus key/record
metadata, initialized atomically by artifact put/import or existing expiry
maintenance. It survives reopen/backup with the authority, has no per-query growth
and cannot be read/written through generic records. Older native archives without
a key require an artifact publication/import or that maintenance round before
signed queries; reads never silently create a new write transaction. Query memory
and work use the shared scan/manifest/depth limits; results remain bounded per page.

Cursor encoding: **Problem:** repeating the full query binding in every cursor
consumed model context and could exceed the 2,048-character cursor limit for a
valid Unicode selector or search term. **Decision:** `trq2` cursors carry
only the existing bounded position. The unchanged private key authenticates the
complete owner/root/operation/query/selector binding as well as the position.
`cursor.authentication` defines the domain-separated HMAC-SHA256 framing;
compact UTF-8 JSON binds Unicode without escaping it into repeated cursor text.
The full 32-byte MAC and payload use canonical unpadded base64url. Route, seek,
observed-expiry and authority checks remain unchanged. No alias store, additional
record, cache, key rotation or retention change is introduced.

Both authorities verify previously issued `trq1` and `trq2` cursors with their
original framing and key. Updated handlers accept all three formats, and model-view
projection recognizes them without dropping required continuations. A prior
reader cannot consume newly issued `trq3` values; upgrade the reading authority
before issuing them. Existing numeric read offsets and zero search cursors keep
their prior behavior. **Alternatives rejected:** shrinking valid query/selector
limits removes capability; shortening an existing signed token breaks its MAC;
per-cursor aliases add persistent lifecycle state; shorter MACs discard integrity
bits unnecessarily. Shared synthetic protocol vectors, live Unicode pagination,
wrong-binding/key/expiry refusal and source-matched native tests cover this change.

A 2026-10-21 component measurement compared 24 paginated plain/canonical
read/search pages across both disposable authorities, with three repetitions.
Next-cursor tokens fell 53.0% on SQLite and 52.9% on TofuDB. After normalizing only
the freshly generated canonical `resultId`, exact `o200k_base` model-view totals
fell from 5,034 to 3,831 on SQLite (23.9%) and from 5,118 to 3,873 on TofuDB (24.3%).
All remaining non-cursor page fields, including body, provenance and coverage,
matched. These are sample payload savings, not whole-task cost or latency claims.

Position encoding: **Problem:** position JSON keys still dominate short range
reads even after query bindings are removed. **Decision:** new `trq3` cursors use
the canonical `cursor.positionEncoding` layout: unsigned LEB128 flags, route
length/indices and ordered position fields. Every position bit remains, including
the native segment byte offset, observed expiry and partial-coverage flag. The
full 32-byte HMAC authenticates the exact payload and the original query binding
under a distinct `trq3` domain. Authentication precedes position decoding.

Both readers reject overlong or overflowing uint64 values, unknown flags, foreign
byte-offset layouts, missing fields and trailing bytes; route depth/node bounds
and expiry checks still apply. New encoding adds no alias store, key, cache or
record lifecycle. Tool metadata, quotes, provenance, coverage and recovery errors
remain unchanged. **Alternatives rejected:** JSON tuples retain unnecessary text
encoding overhead; compression adds framing and decompression work to tiny
payloads; shortening the MAC loses integrity; source-reference aliases add state.
Shared independent wire vectors and malformed signed payloads cover all three
cursor versions, including exact integer boundaries and changed owner/query scope.

A paired reader-upgrade probe followed 380 pages across 20 plain/canonical
read/search cases on the same disposable SQLite and TofuDB authorities. For
8-byte reads, exact `o200k_base` model-output totals fell from 32,294 to 26,499
tokens on SQLite (17.9%) and from 33,186 to 26,795 on TofuDB (19.3%). Complete
single-page reads stayed byte-identical. All non-cursor page fields matched;
52 prior-reader replay/continuation calls succeeded after reopening with the new
reader. OpenAI, Responses and Anthropic request encodings retained the exact
model content. These are fixed-query payload measurements, with no provider
request or whole-task cost/latency claim.

Query recovery: **Problem:** the handler labeled every storage protocol failure
as `invalid_cursor`, losing invalid-selector and expired-evidence causes and
their recovery actions. Other permanent storage failures were marked retryable.
**Decision:** the shared `failures` table declares query refusal codes,
retryability and model guidance. SQL raises those typed storage failures; native
query errors retain their type through semantic execution and the storage wire.
The handler and tool error declarations consume that same table. Correcting a
selector or restarting at cursor 0 reads retained evidence and never re-executes
its producer. Expired/missing continuation evidence stays distinct from malformed
input, and a failed batch item does not discard following successful items.

Unclassified storage protocol failures remain storage failures; their text cannot
establish a cursor diagnosis. Known storage errors retain their retryability,
while unexpected exceptions receive no speculative retry promise. Diagnostics
remain in host logs; model feedback uses declared, bounded recovery advice.
An older engine without the new codes gets this generic storage-failure handling.
There is no new retry loop, record, key, pool or background work; successful
query payloads and existing scan/owner/expiry fences are unchanged.
**Alternatives rejected:** parsing localized exception text creates a second
taxonomy; retrying every rejection wastes calls; repeating a producer creates a
new observation and may duplicate effects. Verify `test_artifact_query_recovery`
on both authorities, shared storage-error wire tests and native semantic error
classification, alongside cursor, budget and batch contracts.

Artifact settlement: **Problem:** failed queries emitted a failure badge but
settled the occurrence as successful, and one failed batch item suppressed media
from successful siblings. **Decision:** the handler publishes the final owned
result's verdict through common settlement and carries media only from successful
items in the fitted result. Partial failure keeps its successful evidence while
remaining an error outcome for the occurrence, event and audit. Successful source
text containing error-shaped JSON remains ordinary data. **Alternatives rejected:**
inferring verdicts from badges or application text splits settlement authority;
dropping every image on partial failure discards completed work. Verify actual
legacy/V2 pipeline delivery, captured parts, events, audits and worker cleanup in
`test_artifact_query_settlement`, alongside decoded media and batch budgets.

Problem: clipping search quotations to fit the response budget could remove the
matched term while retaining a cursor that had already consumed those hits.
Decision: the handler retries the same immutable query/cursor with a smaller
match count until complete quotations fit. Source references, offsets, coverage,
and the resulting continuation remain exactly as returned by the authority.
If even one hit or the page metadata cannot fit, the existing explicit refusal
retains the request's reference, query and batch index; it exposes no misleading
continuation. Alternatives rejected: shortening text or dropping hits locally
strands evidence behind a signed cursor; exceeding the response budget causes
the continuation result itself to spill. This can require more pages, but each
successful page remains usable evidence.

Problem: equally dividing the response budget before observing page sizes
discarded evidence even when the complete requested batch fit, and halving a
large requested limit could repeatedly fetch an unchanged short EOF page.
Decision: each page first fits the whole response allowance; the ordered batch
then measures its actual model view. If that view fits, every page is retained
unchanged. Otherwise, small pages keep their full demand and release unused
space to larger pages through max-min allocation, after framing and failure
costs. Failures remain unchanged and are not retried for budget fitting. Only
oversized successful occurrences re-query, concurrently through the same shared
runner, starting from their already-fetched page. Duplicate occurrences remain
independent; owner, alias, original cursor, query and selector keep their bindings.

Reduced limits use both the requested limit and observed UTF-8 body bytes or hit
count. Empty bodies, single codepoints and single hits cannot be shrunk further;
unfitting metadata returns the existing refusal without repeated queries.
The source still owns whole codepoints, quotations and signed continuations.
The complete wrapper is measured again before return, with the existing
whole-batch refusal if metadata cannot fit. Alternatives rejected: increasing
the response ceiling consumes more context; fixed per-item shares strand free
space; locally trimming signed pages loses evidence; collecting maximum raw
pages before any fitting needlessly increases temporary memory. Verify
`test_artifact_batch_demand_budget` and `test_artifact_batch_demand_backends`
for fitting batches, oversubscription, concurrent fitting failures and restart.

Problem: the query handler counted full canonical JSON before the common model
projection removed replay-only metadata. Sixteen 8-byte reads could issue 64
queries and all refuse, despite every requested model page fitting its share.
Decision: single-page fitting and aggregate batch fitting count the same declared
model projection as common delivery. The handler still returns the exact
authority page, including signed replay/next cursors, provenance and coverage;
canonical capture and decoded program values keep that JSON. Unknown shapes,
required next-page cursors, clipped-view replay cursors and unknown metadata
remain charged. Source scan/byte bounds, response token limits and batch size
remain in force; oversized model pages still re-query the original immutable
cursor. Alternatives rejected: raising token limits hides double accounting;
stripping canonical metadata breaks later recovery; separate filtering rules
would drift from the common model-view owner.

A 2026-10-21 component probe exercised a real sixteen-read batch over plain and
canonical sources on both SQLite and TofuDB, using `o200k_base` accounting.
Before the fix all four cases issued 64 source queries and returned no bodies.
Afterward each issued 16 queries and returned the requested 128 body bytes;
model responses used 5,529–6,361 tokens within the unchanged 7,000-token ceiling.
This measures eliminated false refusals and redundant queries, not whole-task
latency or throughput. Full pages and retained originals are verified by
`tests/tools/test_artifact_model_budget.py` and the real backend query suite.

Model-view replay cursor decision: **Problem:** opaque cursors can dominate short
results even after empty defaults are removed. **Decision:** the declared
`artifactProjectionReplayCursor` policy keeps the signed page-start cursor in
canonical evidence and decoded program results, and omits it only from an
unclipped model view. `nextCursor` still advances the source; source pagination
does not mean the delivered page was clipped. Clipped or unknown view states,
unsigned cursors and future cursor formats retain the original replay value.
An omitted replay cursor does not mean offset zero. Existing canonical capture
and owned retention provide later compaction recovery; this adds no cursor
store, cache, new query or persistent record class. **Alternatives rejected:**
short cursor aliases add another lifecycle; trimming existing signed values invalidates
them; removing the next-page cursor prevents continuation.

A 48-page component measurement used three repetitions of plain/canonical,
terminal/paginated read/search results on disposable SQLite and native TofuDB
authorities. Before/after views differed only by the replay cursor; exact
`o200k_base` counts fell from 11,220 to 6,776 tokens on SQLite (39.6%) and from
11,428 to 6,858 on TofuDB (40.0%). Encoded bytes fell 30.1% and 30.5%.
These are model-view payload savings for that sample, not whole-task cost or
latency claims. The backend projection tests additionally verify retained
original JSON, restart recovery, owner denial and unchanged next-page progress.

**Page reference inheritance.** Problem: native provenance rows and search hits
repeat full digests already named by their page. Decision: the model-only
`artifactProjectionInheritedReferences` contract omits a supported row's
`artifactRef` only when it equals the nonempty page reference. Each batch page
binds independently. Different child references, paths, offsets, segments,
unknown row fields and source content remain exact. An alias is not inferred
equal to a digest. Unsupported reference shapes retain the original source
representation. Canonical JSON and decoded program values keep every reference;
reintroducing the page reference reconstructs each native row. Both short tool
descriptions and detailed help explain this default.

The declared `budgetFormat: canonical` policy sends these tools through the
existing single-result retention/budget owner in both wrapping modes. A mixed
round uses that same canonical aggregate budget and its retained source
carriers. This preserves replay cursors and native JSON through restart and
aggregate pressure. Retention failure remains visible and preserves live source
evidence; it never becomes a claim of durable recovery. No producer-side
prewrapped success record bypasses source retention. Alternatives rejected:
blanket reference removal conflates children; minting aliases adds storage work;
compacting a legacy page without canonical retention loses recoverability.
Verify reference projection/backend tests and `test_artifact_projection_delivery`.

A paired disposable-SQLite sample using `o200k_base` retained identical canonical
pages. A 72-byte canonical read fell from 592 to 499 model bytes and 207 to 163
tokens. Twenty distinct same-source segments fell from 1730 to 730 read tokens
and 1683 to 683 search tokens; every segment/hit remained present. A page alias
different from its provenance digest saved no reference tokens. The two provider
schemas together gained 22 tokens for the inheritance explanation. These are
payload measurements, not whole-task billing or latency claims. See
`../RESOURCE_CONTROL.md` for retention cost and projection timing.

**Repeated search excerpts.** Problem: neighboring hits can repeat the same
quotation and source metadata, consuming the response budget and forcing the
handler to re-query fewer hits. Decision: after canonical capture, the declared
`artifactProjectionMatchGroups` model format groups only consecutive native
matches with identical text, artifact reference and optional path. A group keeps
those fields once and lists every original `offset` in `offsets`, preserving
order, duplicate occurrences and exact integer spelling. Missing and empty paths
remain distinct. An unknown match field, a reserved `offsets` field or an invalid
path disables grouping for that match. Groups never cross source/path boundaries
or intervening different matches; no neighboring quotations are stitched together.
Expanding each group yields the exact original sequence. Canonical/decoded
program results remain individual hits; cursors, gaps, coverage, media, batch
indexes and errors retain their existing meanings and budget enforcement.

Alternatives rejected: deduplicating hits loses occurrences; overlapping-window
merging invents source alignment; blanket page-level reference hoisting hides child
identity. Grouping adds no store, cache or authority. Unknown result shapes retain
their original projection. The tool declaration explains the local `offsets`
representation, and canonical retention still supports recovery after compaction.

Measurement used 24-hit plain and canonical sources on disposable SQLite and
TofuDB authorities, following every continuation with a 20-hit page request.
Exact `o200k_base` output totals fell from 7,061 to 1,931 tokens on SQLite and
7,068 to 1,938 on TofuDB (72.6% combined). Bounded reads and one-hit pages kept their
model output byte-for-byte. OpenAI, Responses and Anthropic request encodings
preserved the exact projected content, including all grouped offsets and
distinct sources; no provider request was sent. These are fixed-sample payload
savings, not whole-task cost or latency claims.

Verify both selected authorities with `tests/tools/test_tool_result_query_backends.py`,
SQL cursor budgets with `tests/tools/test_tool_result_query_canonical.py`, and native
artifact/migration neighbors. Task quote admission additionally verifies
`tests/memory/test_working_tool_sources.py`; [Working facts](WORKING_FACTS.md) owns
the historical-observation seal and user correction semantics.

A three-repetition local SQLite measurement over a 1,200-byte Unicode body with
37 requested bytes returned the same 36-byte text and signed continuation before
and after the codepoint-boundary fix. Body-window queries fell from 14,414 to two;
median query time fell from 208.37 ms to 0.126 ms. This isolates that boundary
case; it is not an end-to-end task speedup or a native-engine throughput claim.
