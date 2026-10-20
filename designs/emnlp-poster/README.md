# Tofu · EMNLP 2026 poster

An English A0 portrait poster about the current Tofu system, built for a
research audience. The manuscript supplies the title, authors, affiliations,
and original mascot. Current implementation contracts supply the technical story.

**Problem.** A benchmark-centered poster would give little insight into why a
researcher should try another agent harness, and historical scores do not
establish the cause of an improvement.

**Decision.** Lead with “Use the agent. Study the harness.” Show three mechanisms:
cache-aware context decisions, programmatic tool execution, and TofuDB's
certified continuation frontier. Give TofuDB the largest figure, including its
maintenance cost and the continued validity of SQLite. Close with a concrete
research workflow and a verified project QR code.

**Alternatives considered.** A feature checklist would obscure the mechanisms;
a leaderboard would repeat the paper; a database-only poster would lose the
research workbench; an exhaustive architecture graph would be difficult to read
at a poster session. No competitor capability is claimed to be absent.

## Files

- `output/tofu-poster-A0.pdf`: **print master**, one page, 841 × 1189 mm.
- `output/tofu-poster-preview.png`: lightweight preview of the actual PDF.
- `output/tofu-poster-detail.png`: larger PDF render for close inspection.
- `output/tofu-poster-A4-proof.pdf`: reduced contact proof; not the print master.
- `output/tofu-poster-editable.svg`: text and diagrams remain editable; fonts are
  embedded with `@font-face`. Editors that ignore embedded web fonts can install
  the TTF files in `assets/`. The PDF remains the visual reference.
- `output/poster-text.txt`: accessible plain-text companion.
- `output/scene.json`: exact shared vector scene, in millimetres and points.
- `output/preflight.json`: checks of the exported PDF, not only its source.
- `speaker-notes.txt`: English pitch and substantive discussion answers.
- `evidence.json`: source hashes, claim boundaries, and design references.

## Build

Python 3.12 was used. In a dedicated environment:

```sh
python -m pip install -r requirements.txt
python build_poster.py --output-dir output
```

The build is local and network-free. It never opens a Tofu application database.
Text wraps with the actual bundled font metrics. PDF and SVG share coordinates,
line breaks, colors, and drawings. The mascot is imported from the author's PDF
as vector artwork; the QR code is generated from the verified project URL.

The bundled static font instances have distinct PostScript identities, so PDF
libraries cannot collapse different weights onto one face. To refresh these
assets from the repository's fonts, install fontTools with WOFF2 support and run
`prepare_fonts.py --font-source ../../frontend/src/styles/fonts`. This optional
preparation step is not needed for an ordinary poster rebuild.

Change copy and layout in `make_scene()` inside `build_poster.py`, then rebuild.
Review the actual PDF render after edits. All dimensions in the scene are
millimetres; all text sizes are points.

## Scientific scope

`evidence.json` records the inspected working-file hashes because the shared
checkout contained concurrent changes; a Git HEAD alone would not identify the
complete technical reference. Runtime code was not modified, and historical
benchmarks or database experiments were not rerun for this design task.

The prefix diagram shows **header verification work**, after a storage-minted
certificate, within one authority-open epoch. An initial certification scans the
full prefix. Later reads verify the terminal boundary and new suffix. Earlier
semantic changes invalidate the certificate; restart requires revalidation.
Materializing selected output still costs work proportional to that output.
The diagram does not promise constant-time history reconstruction, free recovery,
universal speedups over SQL, or better task success.

The three context-layer bars are schematic, not measured token counts. The
closing workflow is a suggested demonstration, not a newly measured user study.
“Inspect” refers to transmitted requests and recorded execution evidence, not a
claim to recover a model's private internal reasoning.

## Print and editing notes

The [EMNLP 2026 specification](https://2026.emnlp.org/poster-printing/) requires
A0 portrait PDF. The print master uses embedded fonts and vector diagrams.
Body copy is mostly 24–29 pt; references and author-role notes are smaller.

Export checks cover page count, physical size, font embedding, missing glyphs,
text boundaries, pairwise text overlap, and raster-image count. The PNG previews
are rendered from the exported PDF. These checks do not certify PDF/X, CMYK
conversion, a printer's bleed requirements, or physical color reproduction.
No physical print proof has been made. Print at **100% / actual size**, using
the A0 master. The A4 file is for a desk review.

The design reviewed
[K-Dense's poster workflow](https://github.com/K-Dense-AI/scientific-agent-skills/blob/main/skills/latex-posters/SKILL.md)
and
[Academic Poster Generation](https://github.com/ZLHe0/Skill-ML-Conference-Poster-Creation/blob/main/SKILL.md)
for physical typography, editable figures and export inspection. The composition,
copy, diagrams, and rendering code are original to this task; no external poster
template, generation service, or image model was used.

## Production environment findings

These were environment/integration limitations, not defects in Tofu runtime:

1. `mtskills --version` and an independent `node --version` failed because the
   default Node binary requires newer glibc/libstdc++. A compatible pre-existing
   Node binary reached SkillHub SSO, but the authentication step did not finish.
   Public author-owned skill repositories supplied the design references.
   SkillHub catalog coverage therefore remains incomplete; the poster is unaffected.
2. Deferred-tool discovery returned `unsupported call` for the exposed search
   tool. The existing execution tool's documented metadata interface located the
   installed plugin-search capability. That read-only search succeeded. No new
   plugin was installed and no design was uploaded.
3. The shared environment's PyMuPDF wheel required a newer glibc. A separate local
   dependency directory used compatible PyMuPDF 1.24.11 and ReportLab 4.4.4.
   A first unconstrained Pillow dependency attempted a source build without JPEG
   headers; retaining the already-working Pillow and pinning compatible
   dependencies resolved rendering without modifying the application environment.
4. Shell downloads of optional new fonts failed through the host network path.
   The poster instead uses Tofu's existing Inter and Newsreader fonts, preserving
   the product typography and avoiding a new network dependency. Their static
   instances and licenses are included.

The first export caught an unused unembedded Helvetica resource and missing
glyphs in a font subset. The generator now sets its initial embedded font
explicitly and validates glyph coverage before export; vector arrows and
supported author markers replace the unsupported glyphs.
Independent SVG/PDF rendering also exposed an exporter alias: variable-font
instancing had retained identical internal PostScript names across weights.
`prepare_fonts.py` assigns distinct names, and the build now refuses aliased
fonts and checks that all used faces survive PDF export.
