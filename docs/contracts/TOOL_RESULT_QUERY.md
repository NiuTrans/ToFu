# Canonical tool result queries

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
valid Unicode selector or search term. **Decision:** new `trq2` cursors carry
only the existing bounded position. The unchanged private key authenticates the
complete owner/root/operation/query/selector binding as well as the position.
`cursor.authentication` defines the domain-separated HMAC-SHA256 framing;
compact UTF-8 JSON binds Unicode without escaping it into repeated cursor text.
The full 32-byte MAC and payload use canonical unpadded base64url. Route, seek,
observed-expiry and authority checks remain unchanged. No alias store, additional
record, cache, key rotation or retention change is introduced.

Both authorities still verify previously issued `trq1` cursors with their
original framing and key. Updated handlers accept both formats, and model-view
projection recognizes both without dropping required continuations. A prior
reader cannot consume newly issued `trq2` values; upgrade the reading authority
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
