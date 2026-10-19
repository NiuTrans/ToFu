# Evolution office game art

## Scene-first command center

**Problem.** Entering the studio through a notebook intake form hid the room
and made live execution look like a disconnected second application.
**Decision.** `#/evolution` opens the interactive studio directly. The durable
program notebook remains at `#/evolution/programs`, accessible from the menu.
The persistent conversational briefing shares the scene's mission desk, with
optional model settings collapsed and an explicit dispatch action. Retry
prepares that same input island; it never launches work automatically. A
receipt-backed stage route in the task panel and one bounded journal excerpt
connect the world to the existing goal API. Recorded stages are not labelled successful without a
verdict. Room/library/delivery actions retain their goal-scoped owners.
**Alternatives rejected.** A compulsory setup modal still delays entering the
world; simulated progress, decorative agent counts and a second polling loop
would separate presentation from backend authority. A new game runtime is
unnecessary for this layout change: the existing collision-aware Canvas2D
scene retains its visible-time lifecycle and on-demand room loading.

The command-center stylesheet is studio-only and uses shared design tokens.
Scene focus hides the mission desk, never pauses or cancels backend work.
Briefing focus follows the drawer's deferred native trigger restoration through
one cancellable, zero-delay handoff; disposal or another drawer clears it.
On narrow screens the mission precedes the world; drawers preserve focus
return and room navigation. The HUD owns four fixed stage nodes and a human-wait
notice, while the task inspector owns the single bounded journal excerpt. The
scene has a plain theme-derived frame rather than a blurred artwork echo. There
is no new timer, transport or retained history.
Verify with `testing/studio.test.cjs`, `testing/route-host.test.cjs`, the studio
browser journey, and the deterministic office render-cost measurement.
On this host, its 3-role/8-visitor, 16-second virtual workload records 964
callbacks at 0.20 ms p95 CPU submission time (not display FPS), with one
maximum pending frame, zero idle/hidden callbacks and zero canvas backing
pixels after disposal. These are regression measurements, not hardware-independent
frame-rate guarantees; the new HUD adds no scheduler or transport.

The retained production art is in `frontend/src/features/evolution/assets/`.
The eight `game-<role>.webp` files are independent 4×4 walking sheets; rows
are front, left, right, back, and columns are walk, stand, walk, stand.
`game-chair.webp` is one transparent chair prop. `pod-*-clear.webp` are
chair-free room backgrounds. All were made with the built-in Image Gen tool
for this project, then packaged as WebP without compositing outside art.

The full-screen visual concept was generated from the previous browser
screenshot with this brief: preserve the recognizable three-desk office,
library, lounge and meeting area; use richly lit, cohesive pixel art and a
compact forest-green task ribbon and bottom room dock; show distinct role
silhouettes; leave the playable floor legible. It informed the HUD and art
direction, but is **not** used as a runtime scene because its baked actors
would contradict live backend positions.

## Production prompt set

Shared character prompt: “Production game sprite sheet, genuinely transparent
background, strict 4 columns × 4 rows with clear gutters. Rows: front/down,
screen-left, screen-right, back/up. Columns: walk left foot, neutral stand,
walk right foot, neutral stand. Same unique character/outfit in all cells,
consistent scale and foot baseline. Full body, warm richly textured crisp
pixel art and dark outline matching the Tofu office reference. No labels,
floor, props outside the body, extra figures.” Character briefs:

| Role | Visual brief |
| --- | --- |
| Executor | Tan man, tousled brown hair, cream rolled-sleeve shirt, teal utility vest, leather messenger strap and headset. |
| Reviewer | Tall dark-skinned woman, pinned hair, silver spectacles, ivory review coat, burgundy shirt and clipboard. |
| Coordinator | Medium-skinned woman, wavy black hair, royal-blue tailored blazer, cream blouse and planning tablet. |
| Researcher | Fair-skinned woman, copper hair, olive field coat, wide tan field hat, satchel and boots. |
| Engineer | Brown-skinned man, curly hair, charcoal hoodie and utility vest, headphones at neck, cargo trousers and teal tech sleeve. |
| Writer | Fair-skinned woman, auburn bob, burgundy beret, green cardigan, long scarf, skirt and notebook. |
| Analyst | East Asian adult, silver undercut, round gold glasses, slate-blue tunic, dark trousers and data folio. |
| Generalist | Older dark-skinned man, salt-and-pepper hair and beard, terracotta overshirt, olive trousers and belt pouch. |

Chair prompt: “One dark forest-green upholstered five-wheel office chair,
viewed from slightly above and behind, facing the desk, with warm light,
cohesive dark pixel outline, rich crisp hand-painted pixel texture and genuine
transparent margins. No person, floor, desk or duplicate.”

Room-edit prompt, applied individually to each of the five original pod
backgrounds: “Remove only the dark wheeled chair at the central workstation
and fill its space with the matching rug and desk underside. Preserve the
room-specific specialty objects, furniture positions, perspective, lighting
and pixel texture. No person, chair, UI or added text.” The original
`pod-*.webp` remain art comparisons; runtime selects the `*-clear` variants.

`scripts/generate_evolution_game_frames.py` measures each sheet’s alpha and
emits `office-game-frames.ts`; `--check` detects drift. The art is loaded by
role on demand. The earlier `people.png` atlas is loaded only if an authored
sheet fails, so there is no double decode on the normal path. The chair is
one reusable prop in the main office and all pods. No external paid asset pack
is distributed with Tofu.

## Room directory and layout concept

The six `room-thumb-*.webp` files are 160×120 packaging derivatives of the
matching authored room backgrounds, encoded at WebP quality 76. They do not
load the full room image into each navigation row. Their combined transfer
size is 18,764 bytes.

The layout concept was generated with the built-in Image Gen tool, using the
previous browser screenshot as a composition reference. Prompt: complete
1536×1024 Chinese studio screen; preserve the warm office art and furniture
geometry; persistent illustrated six-room rail (main, research, engineering,
review, writing, general); forest green #142e28, ivory #f4ead4, brass #baa47c;
mission title and role status above an intact 4:3 scene; dedicated furniture
tool shelf below; coordinator chat and new goal in the rail. All UI remains
code-native. No invented metrics, tasks or people in empty rooms.

The implementation deliberately keeps the whole 4:3 scene within its reserved
region, uses the existing outline icon family, and displays actual backend
counts/unknown marks rather than illustrative concept values. The concept is a
layout reference, never a runtime background.

## Runtime movement and rendering contract

**Problem.** The retained source in this worktree is `office-scene.ts` and
`agent-pod.ts`, not a separate Phaser/Pixi scene. Native inspection of all eight
`game-*.webp` character sheets found the same authored anomaly: side rows are
`left, left, RIGHT, left` and `right, right, LEFT, right`. Uniform row lookup
therefore reversed the actor on the third walking frame. Wall-clock gait,
per-frame alpha-height scaling, grid-center-only arrival, instantaneous torso
cropping, and hidden backup timers compounded the apparent sliding and snapping.
The pod renderer also alternated walking arm frames while a worker was seated.

**Decision.** Keep the bounded Canvas2D renderer and make its engine seams
explicit. `office-motion.ts` owns pure distance gait and pose easing;
`office-navigation.ts` owns constrained routes; `office-sprite-geometry.ts`
interprets authored bounds; `office-canvas.ts` owns dirty paint suppression,
backing-store limits, teardown, and constant-space counters.
`office-pod-motion.ts` owns selected-room actor transitions. No task state is
created by these modules; DOM labels and accessible actor buttons remain real
status projections.

- Source lookup swaps **only column 2 of the two side rows**. It never mirrors
  asymmetric costumes and never edits the generated alpha bounds. Gait cycles
  `[1, 0, 3, 2]` per 52 world pixels at 95 pixels/second, consumes leftover
  distance at route corners, and becomes neutral on arrival. Zero elapsed time
  freezes the current pose rather than advancing gait.
- Each model uses one reference scale (front neutral height). Its alpha-bound
  bottom is the world foot anchor; changing silhouette height cannot stretch
  the actor. Main-room paths use checked orthogonal endpoint connectors and
  arrive at the **exact** reachable target, never a nearby cell center or a
  diagonal corner cut. Unreachable destinations cannot execute a sit/pick step.
- Seating is approach → pull chair → exact foot anchor → turn → eased sit →
  roll in; departure reverses that sequence. Turning takes 100 ms, lowering or
  raising takes 260 ms, and rolling takes 400 ms. Cancellation retains the
  current actor/chair/pose instead of resetting to the workstation. Newly
  entered actions cannot consume the preceding action's elapsed time. The
  rear-facing chair back draws in front of the lowered sitter.
- **Art limitation:** these sheets do not contain authored sitting frames.
  Seating continuously lowers and crops the standing pose behind the backrest;
  it is not a new skeletal animation or a claim of anatomically complete leg
  articulation. Dedicated sit/stand sheets would improve this further without
  replacing the renderer. The selected-room actor uses the same gait/pose math
  and orthogonal desk approach, and no longer performs a fake working fidget.
- One canvas per mounted scene; DPR is capped at 2 and world backing scale at
  1.5 (at most 1440×1080 / 1,555,200 pixels). Motion paint cadence is at most
  30 Hz; identical signatures do not clear or repaint. Resizing unchanged
  backing dimensions does not reset the context. Image decode explicitly
  invalidates the signature. Main-room DOM actor attributes also use dirty
  signatures. Metrics expose requests, paints, skips, resizes, backing pixels;
  main-room metrics additionally expose pending frames and actor count.
- Assets load by visible role and selected room; fallback people decode only
  after an authored sheet fails. At most three leads plus eight visitors are
  resident, **including departing visitors**. Latest bounded roster entries
  wait for a free slot instead of leaking old avatars during churn. Material
  props remain capped at eight and appear only for durable material records.
- Stationary actors—including working actors—schedule **zero** animation
  callbacks. No idle patrol, moonwalking or synthetic task progress. Hidden
  scenes stop their clock and resume from the same pose without catching up.
  Reduced motion resolves an explicit action once and cancels callbacks; it
  does not strand a half-seated actor. Disposal is idempotent, cancels clocks
  and observers/listeners, clears image callbacks, rejects late inputs and
  releases canvas backing pixels.

**Alternatives considered.** Pixi would add a WebGL renderer, context-loss
lifecycle and texture-upload path; Phaser would add that plus an independent
scene/input/ticker system. Neither fixes the atlas direction error, foot
geometry or action sequencing, and 11 actors do not justify either dependency.
A worker/offscreen renderer would add synchronization and transfer cost before
there is evidence that this bounded draw workload needs it. The pure motion
and surface boundaries remain replaceable if real browser profiling later
shows rasterization—not state/asset correctness—is the bottleneck.

### Repeatable verification and performance evidence

No large assets or browser are required for these commands:

```sh
node --test frontend/src/features/evolution/testing/office-motion.test.cjs \
  frontend/src/features/evolution/testing/office-pod-motion.test.cjs \
  frontend/src/features/evolution/testing/office-runtime.test.cjs \
  frontend/src/features/evolution/testing/office-scene.test.cjs \
  frontend/src/features/evolution/testing/office-rooms.test.cjs
node frontend/src/features/evolution/testing/office-motion.bench.cjs
```

A Node v24.14.1 run on the development host measured 100,000 eleven-actor gait
updates in 44.916 ms (0.449 µs/update), 5,000 constrained door-to-seat routes in
422.984 ms (84.597 µs/route), and 100,000 unchanged paint checks in 1.386 ms.
Including warmup, the surface recorded 100,100 requests, **one paint and
100,099 skips**. These are CPU microbenchmarks, not GPU, decode, frame-time or
end-to-end browser claims; timings are reported, not unstable CI thresholds.
Deterministic tests enforce operation counts, constant-distance gait, source
direction, foot geometry, exact endpoints, interrupted poses, hidden/reduced
motion and late teardown behavior. Real-browser visual review at compact and
wide sizes remains necessary for the authored chair/torso composition.

### Integrated browser verification (2026-09-24)

The integrated `ebbfdbca9` implementation passed the full studio and office
browser suites against a local application server and Chromium: **27 tests**,
including the persistent mission panel at 390 and 1536 pixels, role rooms,
chair transitions, reduced motion, keyboard use, disposal and render cost.
`npm run check:frontend` and `npm run verify:frontend` also passed against the
integrated source. Run the browser gate with:

```sh
TOFU_API_MAJOR=4 python3 -m pytest -q -m visual \
  tests/evolution/test_evolution_studio_browser.py \
  tests/evolution/test_evolution_office_browser.py
```

The deterministic render-cost fixture exercises three leads plus eight visitors
through work/sit and stand/wait at virtual 60 Hz, then applies 100 unchanged
snapshots. The observed 964 callbacks covered 16,066.7 ms of simulated time;
callback CPU mean was 0.0455 ms, P95 0.2000 ms and maximum 1.3000 ms. Peak pending
callbacks remained one; the 100 unchanged updates added zero paints; hidden and
disposed scenes had zero pending callbacks; disposal released all backing pixels.
This measures JavaScript/Canvas submission cost, **not GPU completion or display
FPS**. Raw values are regenerated in
`output/evolution-design/runtime/deterministic-render-cost.json`; timings vary
with the host and are not portable performance guarantees.

Reviewed desktop and mobile mission screenshots are retained locally at
`designs/ui-gallery/previews/evolution-mission-1536.png` and
`designs/ui-gallery/previews/evolution-mission-390.png`. These use deterministic
failed-run fixtures, not a claim that a live user's goal succeeded. The mobile
layout places actionable task information before the room; the desktop keeps
the room and scrollable mission panel side by side.
