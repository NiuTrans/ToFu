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
