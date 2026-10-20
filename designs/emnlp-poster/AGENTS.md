# Poster source map

- `build_poster.py` owns the shared millimetre-coordinate vector scene and its
  PDF/SVG exporters. Edit the source, then regenerate; `output/` is disposable.
- `evidence.json` maps claims to inspected implementation and manuscript hashes.
  Keep the scientific scope and source evidence aligned when copy changes.
- `assets/` contains the author's vector mascot and licensed font instances.
  The PDF is the print authority; check its render, not just the source.
- Verification: run `build_poster.py`, inspect `output/preflight.json`, then
  visually inspect the PDF PNG and an independently rendered SVG. Recheck the
  QR destination and decoded code if either the link or QR geometry changes.
