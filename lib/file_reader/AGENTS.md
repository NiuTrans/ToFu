# Local file-reader guidance

`_router.py` selects the native image, PDF, Office or plain-text leaf. `_docs.py`
owns text encoding/binary recognition and lazy parser calls; `_image.py` owns
model image preparation. Read `docs/modules/ingest_media.md` and the relevant
parser owner before changing format policy.

Supported native file bytes reach their reader without private size admission.
Plain text and Office use complete default extraction; PDF inherits its parser
policy. Do not clip results in the router before shared result projection. Preserve
encoding fallback, binary identification and real file errors. Caller/project
read grants remain in `lib/project_mod/abs_path_guard.py`; a reader path is not
ownership authority.

Verify `tests/project_mod/test_project_file_demand.py`, exact-range tests,
`tests/file_reader/test_inspect_image.py` and façade/lazy-import checks.

Image compression and crop pixel windows are model views of the original, not
source-byte admission. Zero-text parser diagnostics are errors, never successful
Office body text. Verify `test_native_file_source_demand.py` with real large
inputs and failure/unavailable parser cases.
