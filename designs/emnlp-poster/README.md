# ToFu · EMNLP 2026 poster

An English A0 portrait poster (841 × 1189 mm) about Tofu's current agent
harness. The PDF is the print master. The manuscript supplies the title,
author roster, affiliations, mascot and paper-reader screenshot; current
implementation contracts supply the technical discussion.

## Editorial and visual decision

**Problem.** A slogan-heavy product page does not give conference visitors
concrete reasons to try a research harness. Historical benchmark gains do not
explain newer engineering mechanisms, and omitting institutional marks hides
the collaboration behind the work.

**Decision.** Use a formal white and blue institutional masthead, large sans-serif
title, real paper-workspace screenshot, and the current production CONTEXT
renderer. Connect these to context reduction, optional programmatic tool use,
and TofuDB's native context reconstruction and certified continuation. Give
both the mechanism and its tradeoff. The code sample uses JetBrains Mono.

**Alternatives considered.** A leaderboard would repeat old results without
causal evidence. A feature comparison would imply unverified competitor gaps.
An abstract architecture-only poster would hide the usable research workspace.
A complete unannotated screenshot would make technical details unreadable.
The CONTEXT component therefore uses public synthetic data, explicitly labeled.

The official marks are Northeastern University (China), the NiuTrans Research
GitHub organization, and YourProvider LongCat. `asset-sources.json` records exact
sources and hashes. The NiuTrans mark is the organization's official avatar;
the adjacent English name is editable type. Marks retain their original colors.
They are affiliation assets, not newly created or AI-redrawn logos.

## Source and deliverables

- `layout.py`: copy, physical composition and diagrams.
- `build_poster.py`: shared vector scene, PDF/SVG export and print preflight.
- `capture_inspector.py`: actual production CONTEXT renderer with a synthetic
  receipt. Its source hashes and values are recorded alongside the PNG.
- `verify_exports.py`: independent Chromium SVG render, text bounds and QR
  decode from the final PDF at 300 dpi.
- `evidence.json`: claim-to-source mapping and scientific boundaries.
- `asset-sources.json`: institutional marks and screenshot provenance.
- `speaker-notes.txt`: English introduction and technical discussion notes.
- `assets/`: official marks, supplied images and licensed fonts. JetBrains Mono
  has its own OFL notice; Inter/Newsreader notices are retained.
- `output/tofu-poster-A0.pdf`: one-page print master.
- `output/tofu-poster-editable.svg`: editable text and vector diagrams, with
  fonts and raster assets embedded. Install bundled fonts if an editor ignores
  SVG web fonts; compare with the PDF after editing.
- `output/tofu-poster-preview.png`: actual PDF preview.
- `output/tofu-poster-A4-proof.pdf`: reduced contact proof.
- `output/preflight.json`, `svg-preflight.json`, `qr-verification.json`: checks.
- `output/proof-*.png`: close views of the exported PDF.

## Reproduction

Use a dedicated Python environment with `requirements.txt`. Rendering does not
open an application database, call a model, or require network access.

```sh
python build_poster.py --output-dir output
```

Independent verification additionally uses Playwright, Chromium and zxing-cpp:

```sh
python verify_exports.py --browser /path/to/chromium --output-dir output
```

The checked-in Inspector PNG makes UI recapture optional. To refresh it, point
at a Tofu checkout with its existing esbuild dependency and a compatible Node:

```sh
python capture_inspector.py --repo /path/to/tofu \
  --node /path/to/node --browser /path/to/chromium
```

`PLAYWRIGHT_NODEJS_PATH` can select the same compatible Node for Playwright.
The capture bundles production renderer functions, icons and disclosure helpers;
only the framing container and example receipt belong to the poster. It starts
a temporary static server, reads no private sessions and shuts the server down.

## Scientific and print scope

The 8/128/2,048-turn example reports logical header work from the documented
fixtures. It is neither end-to-end agent speed nor proof of general database
superiority. Same-epoch certification, edit invalidation, first/reopen scans and
output materialization remain explicit. Local code programs require opt-in.
Request receipts are append/decision records, not provider billing or proof that
a model consumed a particular source. The paper-reader screenshot is historical;
the poster labels it, and current technical claims have separate sources.

Inspect the final PDF and independent SVG render. The exports are RGB, not a
certified PDF/X or CMYK proof. No physical printer proof has been performed.
