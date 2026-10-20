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
short cursor aliases add another lifecycle; shortening signed values invalidates
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
