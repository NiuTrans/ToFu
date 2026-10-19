# Knowledge ingestion owner

`ingest.py` parses source evidence; `assets.py` validates and extracts visuals;
`store.py` owns file rollback plus atomic repository publication. Read
`docs/modules/ingest_media.md` and `docs/STORAGE.md` before changing durability.
`publication.py` owns the per-call storage-entry witness shared by repository
and store. Unknown outcomes retain candidate files; the marker never resets on
retry. Verify publication failure tests and actual native restart tests.
`file_durability.py` batches POSIX directory barriers before metadata entry.
Knowledge descendants share an existing durable data/fixture anchor. Verify
directory-fault, native batch and restart tests; file fsync remains per writer.
Candidate writers share one rename/rollback owner. Visual preparation closes
extracted spools once; verify file-ownership faults before publication tests.

native_html.py owns explicit pinned HTML library bindings, not default selection.
Its synchronous calls lease code through output decoding; close joins all leases.
Verify native HTML binding/contract and release archive tests with actual bytes.
Profile data derives from gen_native_html_projection.py; never hand-edit it.
Generated CFFI types derive from the ABI contract via gen_native_html_projection_abi.py.
Runtime never parses C declarations; verify memfd fallback cleanup and import scope.
extract/add_document/reindex_document borrow an explicit html_projector through
nested EPUB/email work. Keep default fallback and explicit refusal distinct.
native_html_service.py joins one caller-supplied lazy binding factory. Unused
owners never load native code; close joins accepted calls, and initialization
failure never silently retries. Verify test_native_html_service.py.
html_application.py registers per-application factory selection and joined
lifespan cleanup; HTTP routes borrow its projector without ambient parser state.
Verify test_html_application.py and test_html_application_http.py with real bytes.
test_native_html_http_storage.py also requires configured source-matched native
storage; it uses normal HTTP repositories through restart and owner deletion.

`asset_spool.py` owns anonymous extracted image bytes until publication or
rollback. Asset counts and aggregate bytes never truncate a corpus. Keep
metadata/provenance, duplicate-content policy and per-image validation explicit.
Read one asset at a time; never materialize a corpus of raw images. Extraction
propagates spool I/O failure, and every store path closes the owned sequence.
Nested extraction transfers evidence into its parent's spool and closes its
child; metadata edits retain immutable raw offsets.

Verify `tests/knowledge/test_asset_spool_demand.py`, PDF resource/vision cases,
and knowledge data-layer/repository tests. Visual traversal uses actual document length; pipeline OCR has no local page
quota. Explicit OCR ranges, text/model budgets, permissions and paid enrichment
retain separate authorities. Verify `test_knowledge_page_demand.py` for late pages.
