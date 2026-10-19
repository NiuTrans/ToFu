# PDF image ownership

- `_render_worker.py` is a standalone standard-library entry point; import the
  PDF library lazily, never import application configuration or `lib` there.
- `_render.py` owns child launch, cancellation, joined cleanup and JPEG spools.
  The frame constants and render kernel live once in `_render_worker.py`.
- VLM uses isolated rendering; the in-process facade retains the MuPDF safety
  lock for callers already running inside a PDF process.
- Pillow loads inside resize/stitch work; text-only discovery stays codec-free.
  Verify fresh-process import scope and image fallback in
  `tests/pdf_parser/test_image_codec_demand.py`.
- Behavior/lifecycle authority: `docs/modules/ingest_media.md`. Verify with
  `tests/pdf_parser/test_vlm_render_process.py`, then image-spool/task-budget
  tests. Process tests own their fake PDF module, directories and children.
