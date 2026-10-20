# ToFu · EMNLP 2026 poster

English A0 portrait (841 × 1189 mm). The PDF is the print master. The manuscript
supplies the title, authors, affiliations and mascot; current repository
contracts and recorded measurements supply the technical content.

## Design decision

**Problem.** Feature descriptions and large unannotated UI screenshots did not
explain the harness's technical contribution. A conference poster needs a
legible mechanism, concrete examples and evidence that can be discussed.

**Decision.** Use the finished SWE-agent, SWE-smith and SEED posters as visual
references, recorded in `community-references.md`. Organize the page around one
original runtime figure, two technical examples and a continuous TofuDB section
with a four-arm data table. The white/green section structure has semantic blue
(context) and orange (execution) accents. Official affiliation marks remain in
their original colors. All scientific labels and table values are editable.

**Alternatives considered.** A leaderboard-centered poster would explain little
about current mechanisms. A feature checklist would imply unsupported competitor
gaps. The previous screenshot-heavy composition left the main technical argument
in captions. A prose-heavy three-column template would make the demo difficult
to scan. The retained composition uses both diagrams and scoped numerical data.

## Source map

- `layout.py`: visible copy, physical layout, diagrams and shared QR geometry.
- `poster-data.json`: exact displayed measurements, conditions and source paths.
- `build_poster.py`: vector Scene, PDF/SVG export, print preflight and PDF previews.
- `verify_exports.py`: independent Chromium SVG render, bounds and PDF QR decode.
- `evidence.json`: claim-to-source hashes and limits of the measurements.
- `asset-sources.json`: official institutional assets and retained source images.
- `community-references.md`: actual poster references and design decisions.
- `speaker-notes.txt`: short English pitch and answers to likely questions.
- `assets/`: official marks, author mascot and licensed fonts. The paper screenshot
  and Inspector capture are retained source assets, unused in this composition.
- `capture_inspector.py`: optional production UI capture using synthetic data;
  unrelated to the current export and never authorized to read private records.

## Reproduction and editing

Use a dedicated Python environment with `requirements.txt`, then:

```sh
python build_poster.py --output-dir output
python verify_exports.py --browser /path/to/chromium --output-dir output
```

Independent verification also needs Playwright and zxing-cpp. No model,
application server, user database or network call is needed for rendering.

Edit `layout.py` and `poster-data.json`, regenerate, and inspect the actual PDF
preview. `tofu-poster-editable.svg` contains editable text and vector figures,
with embedded fonts/assets. Install bundled fonts if an editor ignores SVG web
fonts and compare the edited result against the PDF. Do not use the PNG as a
print master. `tofu-poster-A4-proof.pdf` is a reduced contact proof only.

## Scientific scope

The cache-cost table is an observed pair of adjacent requests, not a causal
compaction ablation. Program filtering is labeled illustrative; local JavaScript
requires explicit opt-in. Request receipts explain dispatch decisions, not
provider billing or proof of model consumption.

The TofuDB table uses the later four-arm semantic controls, including a native
loss on escaped full reads. Rust/Python and engine effects are not isolated.
Full means a warm-authority full context read, not a cold start. No benchmark
was rerun for this poster. The three-header figure concerns same-epoch logical
validation work; first certification, edit invalidation, reopened reads and
complete output materialization remain separately accounted for.

Exports are RGB. Physical printer color proof and PDF/X certification have not
been performed. The source has no dependency on a public hosting deployment.
