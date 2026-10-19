# Continuous films: one stage, one clock, explicit handoffs

Use this path for short product films, conceptual explainers and one-take motion
pieces whose meaning depends on an object becoming the next idea. Use the normal
[WORKFLOW.md](WORKFLOW.md) multi-scene path for narration-heavy explainers with
independent footage. `produce_video` remains scene-based: this is a first-class
**authoring path through the existing low-level motion tools**, not an automatic
new director mode. The `production` capability plugin and a project must be
available; do not enable plugins or change policy without authorization.

## Start from something executable

Copy `lib/motion_video/guide/continuous/index.html` and `continuity.json` into a
new project directory. The original 12-second starter carries the same seed
through three editorial beats. Its GSAP runtime is the same pinned, verified
scene-local dependency used by normal scene authors. Stage it from the Tofu
checkout before opening/checking the copied project:

```python
from lib.motion_video._runtime_assets import ensure_gsap
assert ensure_gsap('/absolute/path/to/film'), 'GSAP staging failed'
```

Do not edit the shipped starter in place or add network/CDN fallbacks. Fonts and
other media must likewise be local and render-ready; production typography
should use explicit bundled font files. Read [COMPOSITION_CONTRACT.md](COMPOSITION_CONTRACT.md).
The HTML itself has no autoplay: the renderer seeks its paused timeline.

1. **Design the film, not a list of slides.** Write the audience, central claim,
   duration, aspect ratio, visual metaphor, typography, palette, sound intent,
   and negative constraints. Choose one visual system, not a carousel of styles.
2. **Write the beat sheet and handoffs first.** Each beat has a purpose. At each
   boundary explain which object carries the idea, how it changes, what the
   camera does, and why that change advances the meaning. A colored background
   surviving is not meaningful continuity. Keep a strong opening and a readable
   final hold; vary pace and include intentional rest.
3. **Build one persistent world.** One composition root, stable DOM/SVG nodes,
   one paused GSAP timeline. Animate a nested world/camera wrapper, never the
   root or its background fill. Subject and camera share the same clock. Reuse
   the actual carrier element; do not swap a clone with the same ID. Do not
   accumulate physics state, use wall-clock timers, or trigger irreversible
   `.call()` DOM mutations. Randomness, if needed, must be seeded/precomputed.
4. **Declare `continuity.json` next to `index.html`.** The executable validator
   lives in `lib/motion_video/_continuity.py:validate_continuity_plan`.
5. **Run `motion_video_check(project_dir=...)`.** Existing CLI gates still apply.
   Successful static checks then sample every declared carried boundary in a
   real browser. Inspect the returned contact-sheet paths with the image tools.
   Resolve failures; missing Chromium/assets is unavailable evidence, not a pass.
6. **Render once** with `motion_video_render(project_dir=..., output=...)`.
   Rendering independently rechecks continuity, so skipping the explicit check
   does not bypass the gate. No clip concatenation is needed for this path.

   The renderer rechecks the HTML/contract digest before and after encoding and
   stages the MP4 before replacing the previous output. Keep referenced assets
   immutable during a run: the digest does not snapshot every dependency.
7. **Watch and deliver.** Inspect full-film opening/middle/ending frames, play
   the result, check legibility/pacing/product meaning and sound, then use the
   existing finalize/publication tool. A sampled pass is not artistic acceptance.

## Contract

```json
{
  "version": "motion-continuity-v1",
  "duration_s": 12,
  "beats": [
    {"id": "idea", "start_s": 0, "end_s": 4, "purpose": "Introduce a seed"},
    {"id": "system", "start_s": 4, "end_s": 8, "purpose": "Connect the seed",
     "handoff": {"kind": "transform", "carrier_id": "continuity-seed",
                 "reason": "The same seed moves into the network center"}},
    {"id": "signal", "start_s": 8, "end_s": 12, "purpose": "Resolve the system",
     "handoff": {"kind": "transform", "carrier_id": "continuity-seed",
                 "reason": "The center carries into the final emblem"}}
  ]
}
```

- Versioned JSON, at most 64 KiB; 2–24 contiguous beats covering exactly the
  composition duration, at most 180 seconds; each beat is at least 0.25 seconds.
  IDs are short DOM-safe identifiers; reasons/purposes are 1–400 characters.
- Each incoming boundary explicitly declares **transform**, **hold**, or **cut**.
  `transform` means measurable **screen-space position/size change** across the
  sampling window. Pure rotation, recoloring or an out-and-back move is not this
  metric: use `hold` for an intentionally anchored carrier and judge its visual
  change from the frames. `hold` still requires identity/visibility/repeatability.
- `carrier_id` is a unique DOM/SVG element ID, not an arbitrary CSS selector.
  Composition roots, canvas interiors and near-full-screen backgrounds cannot
  serve as carriers. A `cut` requires a reason and must not claim a carrier.
- Cuts are disclosed separately, never counted as carried successes. An all-cut
  plan fails for insufficient continuity evidence. No sidecar means ordinary
  scene behavior (`not_requested`), **not** a perfect continuity score.

## What the browser actually checks

For each carried boundary, capture five moments around the handoff (up to
±0.2 seconds, including ±1/60 second), then revisit the first and last moments
in reverse-seek order. The same shared temporal sampler handles absolute-time
seeking, real video frames, font/image readiness, runtime errors and cleanup.

Checks: the **same DOM object** survives all samples, remains visible and
screen-readable (opacity ≥0.1, clipped screen area ≥0.1% and ≤85%), does not jump
more than 12% of the frame diagonal or 0.35 log-scale units between immediate
boundary samples, and reproduces identical screenshot bytes at revisited times.
`transform` additionally needs at least 1% diagonal travel or 0.05 log-scale
change over the wider window. These thresholds are diagnostics, not physics.

Results say `sampled_pass`, `failed`, `invalid_contract`, `unavailable`, or
`aborted`; they include per-boundary errors, sample times and contact sheets.
The sheets are atomically replaced at `.tofu-continuity/boundary-NNN.png`, at
most 23 retained files per project. Receipts include image hashes because a
later check can replace these previews. Browser work shares the caller's
remaining check/render budget; unlimited legacy CLI mode still gives the new
continuity preflight a finite 300-second budget. Missing evidence blocks an
opted-in film's render rather than silently downgrading it to a normal scene.

Safe filesystem locking/no-follow primitives are required; unsupported platforms
return infrastructure-unavailable rather than falling back to unsafe writes.
**Limits:** only declared boundaries are sampled, not every frame. DOM identity
is not semantic identity. Bounding boxes do not prove unobstructed pixels,
mask/clip-path coverage or focal meaning. Undeclared cuts can evade the checks.
A timeline that freezes the entire picture can pass an explicitly declared hold.
The checker cannot judge originality, pacing, brand accuracy, typography or
sound. Inspect the images and the final MP4; do not turn this into an inflated
“cinematic quality” score.


For 2D compositions, use `gsap.config({ force3D: false })`, as the starter does.
Chromium's composited 3D layers can re-rasterize edges differently after reverse
seeking despite identical CSS state. The strict pixel-repeatability check can
flag that noise; inspect the evidence rather than silently skipping the check.
## Scope and independence

This path reuses HyperFrames, GSAP, the temporal sampler and current motion tool
owners. It introduces no renderer, agent loop, job scheduler, paid dependency
or default-enabled plugin. It does not add temporal shutter/motion blur, audio
composition, a persistent scene graph across separately rendered clips, or
automatic continuous direction inside `produce_video`.

The contract, checks and starter are original Tofu code. No OneTake source,
examples, scripts or assets are vendored. Its source-available noncommercial
license is not a permissive commercial dependency license.
