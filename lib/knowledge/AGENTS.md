# Knowledge ingestion owner

`ingest.py` parses source evidence; `assets.py` validates and extracts visuals;
`store.py` owns file rollback plus atomic repository publication. Read
`docs/modules/ingest_media.md` and `docs/STORAGE.md` before changing durability.

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
