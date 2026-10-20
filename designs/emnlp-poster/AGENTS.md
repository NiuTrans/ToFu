# Poster source map

- `layout.py` owns copy and physical composition. `build_poster.py` owns the
  millimetre-coordinate scene, PDF/SVG exporters and print preflight. Edit these
  sources, then regenerate; `output/` is disposable.
- `evidence.json` maps claims to inspected implementation and manuscript hashes.
  Keep the scientific scope and source evidence aligned when copy changes.
- `assets/` contains the author's mascot, official institutional marks, licensed
  fonts, supplied paper screenshot and current Inspector component capture.
  `asset-sources.json` and the capture's provenance record their origins.
  `capture_inspector.py` renders production UI with explicitly synthetic data;
  it must never read live private conversations or call a model.
  The PDF is the print authority; check its render, not just the source.
- Verification: run `build_poster.py`, inspect `output/preflight.json`, then
  visually inspect the PDF PNG and an independently rendered SVG. Recheck the
  QR destination and decoded code if either the link or QR geometry changes.
