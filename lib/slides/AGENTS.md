# Slides guidance

## Scope

This package is the slide-deck production recipe: the PPTD YAML DSL (single
source of truth), the HTML/PNG preview renderers, the native PPTX exporter,
and the nine-stage checkpointed pipeline. Shared durable stage, recovery, and
publication behavior lives in `lib/production/`; theme/font/design-bible
governance lives in `lib/design_sys/`. Read `docs/modules/production.md`
first; the domain owner doc is `docs/modules/ingest_media.md` §4.2 and the
model-facing format contract is `guide/PPTD_CHEATSHEET.md`.

## Editing rules

- PPTD is the only authoring format; the HTML preview and the PPTX export
  are two RENDERINGS of one model and must never fork its semantics —
  theme resolution stays centralised in `pptd.py`.
- Keep research, outline, design, author, assets, render, layout_qa,
  visual_qa, and export as explicit finite stages with checkpoint lineage;
  resume is idempotent and an upstream change invalidates every dependent
  suffix. Chat-initiated decks also carry an `outline_gate` stage between
  design and author (interactive human review): it must stay a no-op with
  zero footprint for headless jobs, and authoring always reads the
  EFFECTIVE outline via `_effective_outline` (the gate's checkpoint
  outranks the outline stage's original on live and resume paths alike).
- Publication discipline: fallback pages block delivery; `status` (delivery
  axis) and `artifact_quality.degraded` (product axis) stay separate; font
  embedding is a hard export gate, never a best-effort note.
- Visual repairs use `visual_repair.py` to recover the original YAML/preview
  pair after interruption. A candidate passes page layout and pixel review,
  then whole-deck coherence, before adoption; known major/blocker findings
  prevent export. Native export is atomic and requires `pptx_qa.py` package QA.
- Conversational edits are bounded revisions over a FINISHED deck:
  deterministic field mutations belong in `patch.py` (zero-LLM, validated,
  layout-measured, rolled back on regression), deck-level token swaps belong
  in `theme_ops.py` (one manifest rewrite, one re-render, one export), and
  `author.edit_page` stays the LLM fallback for genuine content rewrites.
  Every edit path re-validates and never leaves a page or manifest
  half-written.
- Bound pages, media bytes, explicit token authorization, and retained
  previews; cancellation terminates child processes without deleting
  published/durable user artifacts.

Author, image and visual-review work uses `lib/production/concurrency.py`
`owned_results`: no active-count window, caller-owned adoption, joined cleanup.

## Verification

Run `tests/slides/test_slides.py` (format/export/QA contracts),
`tests/slides/test_slides_patch.py` + `tests/slides/test_slides_theme_ops.py` +
`tests/slides/test_slides_batch_edit.py` (conversational revision),
`tests/slides/test_slides_creative_foundation.py`,
`tests/slides/test_slides_semantic_components.py`, and
`tests/slides/test_slides_portability.py` (font embedding). Add
production-substrate tests when shared stage semantics are affected.
Use `test_slides_visual_repair.py`, `test_visual_repair_recovery.py`, and
`test_pptx_package_qa.py` for adoption, interruption, and native-package gates.
