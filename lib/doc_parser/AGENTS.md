# Office and plain-text parser guidance

`_dispatch.py` owns complete default extraction and explicit positive output
character budgets. HTTP reception belongs to `routes/upload.py`; public behavior
is documented in `docs/modules/ingest_media.md`.

`_office.py` traverses actual read-only worksheet XML and owns workbook close.
`_tables.py` chooses dense or sparse rendering before padding ragged data; sparse
row labels are table ordinals. `_truncation.py` is the single warning formatter
for caller-requested cuts. Preserve format checks and genuine backend failures.

Verify document-source-demand, truncation-honesty and backend-cleanup tests under
`tests/doc_parser/`, then knowledge table/parser neighbors.
