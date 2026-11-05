# Evolution office game art

## Game screen

**Problem.** The studio read as a web page wrapped around a picture: a navigation
column, a framed canvas card with a caption, a task panel of accordions and a
text toolbar, plus explanatory sentences everywhere ("click empty floor to
move", "pausing animation only freezes the room"). It also misreported real
state: a run whose executor had parked on a registered compute job showed
"recording results" (the snapshot overlay relabeled the consumed terminal as
`settling`), and park's placeholder text appeared as the deliverable.
**Decision.** The route is one full-bleed game screen. A canvas world fills it;
walnut-and-brass HUD chrome floats over it (quest banner, four-stage track,
token/tool/time meters, room tiles, hotbar with one context action, toasts, an
RPG dialogue box); parchment journal windows dock beside the world while the
camera reframes the room. Everything visible comes from one pure projection
(`studio/projection.ts`) of confirmed snapshots, so a compute park reads as
"waiting on compute" with its job id, a stalled watcher as a red alert, a
pending question as the asking role walking to the front door plus a choice
dialogue, and a running stage's usage as a `≥` lower bound. Visible copy is
state; names, long reasons and key hints live in tooltips and accessible
labels. No surface repeats another: an open chat log marks the coordinator's
line read so the dialogue box never echoes it, the actor sheet (portrait, live
state, current tool, model) offers a decision only to the role that asked, and
free-form swarm roles are named by a localized job title from their room theme.
The backend snapshot overlay now returns the stored run once its
terminal is in the receipts (`lib/evolution/runs.py::_terminal_snapshot`).
A small purpose-built engine (`engine/`) supplies what a game screen needs and
the old renderer lacked: a fixed-step loop over visible time that sleeps with no
callbacks and wakes on one timer for the next keyframe, a camera that fits the
room height inside the HUD frame with pan, wheel/pinch zoom and focus-follow on
portrait screens, pointer hit testing on the canvas, in-world labels/emotes
drawn crisply in screen space, short particle bursts for real events, and a
stage that swaps scenes (main office, department rooms) and mirrors every
target as an accessible button. The human gets an avatar (the only character
they steer: click, WASD/arrows, E to use the nearest furniture); residents are
never moved by the human, because their poses are backend state.
**Alternatives rejected.** Restyling the web layout kept the panels and prose
that made it feel like a form. Phaser/Pixi would add a WebGL context lifecycle,
a second scene/input system and a large dependency without helping the real
problems (state projection, art-aligned layout, accessibility); 11 actors over a
painted room do not need GPU batching. A continuously running 60 FPS loop would
burn CPU while a goal waits for hours; the keyframe sleep keeps idle cost at
zero. Simulated progress bars, wandering idle agents or a player-steerable
executor would separate presentation from backend authority.

Studio styles live in `26-studio-game.css` and consume the theme-invariant
`--studio-*` palette in `00-design-tokens.css`: the painted scene never changes
with the chat theme, so neither does the chrome drawn over it. Shared `.ui-*`
controls inside windows are themed by scoping the tokens they read. The HUD is
one bar on wide screens; below 1024px the stage track and meters wrap in their
own row under the quest, because a single row at those widths either covered
the track with the meters or shrank the quest to its emblem. Verify with
`testing/{projection,engine,office-scene,goal-room,office-rooms,studio}.test.cjs`,
the studio browser journeys and the deterministic office render-cost measurement.

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

**Decision.** Keep Canvas2D and make its engine seams explicit.
`office-motion.ts` owns pure distance gait and pose easing;
`office-navigation.ts` owns constrained routes; `office-sprite-geometry.ts`
interprets authored bounds; `engine/loop.ts` and `engine/stage.ts` own the clock,
backing store, sleep/wake decisions and teardown; `office-layout.ts` owns the
art-aligned points (the earlier scene stacked "shelf" books on the reviewer's
rug because its points predated the current art).
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
- One stage canvas for the mounted studio, sized to the viewport at the device
  pixel ratio so in-world labels stay crisp (raster memory is viewport × DPR² ×
  4 bytes, e.g. about 20 MiB at 1440×900 and DPR 2; released on disposal).
  Simulation advances fixed 1/60 s steps of visible time; a stalled frame
  replays at most 250 ms. The world reports its next change: continuous frames
  only while something moves, otherwise one timer to the next emote/screen
  keyframe (160–900 ms periods, only while a role is live), otherwise no
  callbacks. The accessible target layer rewrites only when its signature
  changes. Metrics expose frames, steps, sleeps, timers, paints, resizes and
  backing pixels.
- Assets load by visible role and selected room; fallback people decode only
  after an authored sheet fails. At most three leads plus eight visitors are
  resident, **including departing visitors**. Latest bounded roster entries
  wait for a free slot instead of leaking old avatars during churn. Material
  props remain capped at eight and appear only for durable material records.
- Stationary actors schedule no continuous frames; a working actor's emote is
  the only wake source, at its keyframe period. No idle patrol, moonwalking or synthetic task progress. Hidden
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
  frontend/src/features/evolution/testing/office-rooms.test.cjs \
  frontend/src/features/evolution/testing/engine.test.cjs
node frontend/src/features/evolution/testing/office-motion.bench.cjs
```

A Node v24.14.1 run on the development host measured 100,000 eleven-actor gait
updates in 44.916 ms (0.449 µs/update) and 5,000 constrained door-to-seat routes
in 422.984 ms (84.597 µs/route). These are CPU microbenchmarks, not GPU, decode, frame-time or
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
