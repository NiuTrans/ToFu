# Evolution workbench

`workspace.ts` lazily composes `studio.ts` as the primary `#/evolution` surface;
the durable program notebook remains explicitly available at `#/evolution/programs`.
`programs/experiment-charts.ts` projects bounded saved measurements through the shared rich-response chart presenter. `evidence-charts.ts` admits complete delivery charts beside exact originals, without transport or timers; its acceptance note is optional for surfaces that show the verdict beside the charts. Studio styles live in `frontend/src/styles/application/26-studio-game.css` (theme-invariant `--studio-*` palette in `00-design-tokens.css`); notebook observations use `26-evolution-observations.css`.
`route-host.ts` owns pending-route epochs, one mounted page and teardown; each
route imports its own graph through the shared document-lifetime module cache.
`studio-entry.ts` alone loads studio styles. Child import failures
use the shell's installed reload authority; no route creates its own guard.
Chat is the conversational entry; `programs/page.ts` links source-bound plans back through the shell conversation loader. The advanced form and detailed notebook reuse the same programme records (`testing/programs.test.cjs`).
The program notebook composes the shared durable program/run API; its
`programs/controller.ts` owns one visible read flight, paging and route fencing.
`programs/current-experiment.ts` projects confirmed execution identity, stable inspection controls and unavailable-read warnings without transport or timers.
The notebook exposes automatic diagnosis policy, source receipts and review-pending
replan hypotheses; archived submitted plans never imply verified value.
`programs/hypothesis-comparison.ts` retains at most two selected snapshots across loaded history pages; owner changes clear them. It compares original claims and exact experiment reviews, reusing the shared explicit source reader without new transport, writes, model calls or cache.
Source-bound semantic lookup history reads no model; its exact quotes, coverage,
original consumption and source paging follow `docs/EXPLORATION_MEMORY.md`.
Incomplete consumption is a visible lower bound across notebook, studio and
episode views; archive counts never assert unseen usage was zero.
`programs/drafts.ts` persists unsent form input and ambiguous creation/append keys under an explicit authenticated owner/application scope through the shared browser-storage capability. Its 64-entry/512 KiB envelope rejects quota, malformed data and overflow without eviction or truncation; it never replays work. Page inputs restore before explicit submission; compare-before-clear ACKs preserve newer edits and successful creation retains the chosen failure policy for the next goal. See `testing/program-drafts.test.cjs`.
`programs/source-reader.ts` adapts the shared outcome reader to explicit notebook delivery/receipt controls. One lazy source page binds the frozen reference task/digest/Unicode length and source kind; semantic original hits explicitly open the matched chunk offset through the shared reader; program change, hidden pages and disposal abort reads. Sources remain separate from review acceptance and evidence consumption. No additional timer, model request or durable cache is introduced.
`programs/hypothesis-assessment.ts` presents demand, feasibility, novelty and net-value judgments independently after verifying the exact hypothesis digest. Two comparison slots retain bounded snapshots; owner changes/disposal fence asynchronous checks. Missing/foreign legacy reviews stay unknown; review success never infers user value.
`programs/limits.ts` presents confirmed cumulative-limit blockers from program headers; server commands own admission, and committed intents retain their original request.
Stable forms own explicit writes and ambiguous-retry keys. Receipt details load
full text only when opened; final delivery is independent of the selected history
page. Its confirmed completed program/terminal receipt survives read failure with a warning; successful absence/mismatch clears it. Controller and page keep one bounded receipt and stable content DOM. Verify with `testing/programs.test.cjs` and the real-server
`tests/evolution/test_evolution_program_browser.py` journey.
`workspace.ts` also composes the goal-only `studio.ts` at `#/evolution/runs`, the lazily loaded auto-research
page (`research/`, `#/evolution/research` — direction composer, pipeline console,
record archive and production workspace; launches research-domain runs through the
registered `_evolutionRunsApi` seam), and the detailed `view.ts` pages;
`conversation.ts` owns the journal projection, bounded unsent drafts and idempotent
message/follow-up writes; `studio.ts` supplies its snapshots without a second timer.
`role-models.ts` owns the actor model inspector; Chat remains the catalog authority.
`studio/goal-run-inspector.ts` keeps a keyed execution-record page and a stable
`studio/outcome-reader.ts` island. Explicit owner GETs retain one original page;
run/visibility/disposal fences abort and reject stale reads. Terminal delivery
never implies review acceptance. See `docs/STAGE_OUTCOMES.md` and
`testing/outcome-reader.test.cjs` for original paging and interrupted-usage checks.
The same reader separates declared artifact snapshots from output/tools, pins their
digest/completeness, and marks incomplete capture; full bytes never imply verified value.
`controller.ts` owns the detailed-page read lifecycle.

The studio (`studio.ts`) is a game screen over real Headless runs: one visible-page
read flight, stable input islands, idempotent submission retries and goal-fenced
writes. `studio/projection.ts` is the single pure projection from confirmed run,
workspace, coordinator, question and swarm snapshots to everything shown: phase,
wait (human | compute | background), stage track, per-role intent/emote, meters (a
running stage's usage is a `≥` lower bound), counts, deliverable and the one
primary action; `studioEvents` turns two views of the same goal into toasts. A
compute park's placeholder result is never a deliverable. Visible copy is state,
never instruction; names and long reasons live in tooltips and accessible labels.
`studio/hud.ts` renders the quest banner, stage track, meters, hotbar and toasts;
`studio/dialogue.ts` the dialogue box (pending questions as choice forms with the
asking role's portrait; a stalled compute read as the executor saying where it is
stuck, with the record and the Router Watchdog hand-off; the coordinator's newest
unseen reply; an open chat log marks that reply read, so the box never repeats it);
`goal-workbench.ts` derives the stall facts and wait label shared by the dialogue
and the inspector, so a parked turn's record reads as the wait, never as a finished
delivery; `studio/windows.ts`
one docked, non-modal window with persistent panes (journal, chat, briefing, menu,
actor sheet), heading focus on open, focus return (to the stage when the opener
cannot take it, e.g. a route-opened briefing) and Escape. The actor sheet shows
the role's portrait, live state and current tool above `role-models.ts`; its
decision action exists only for the role whose question is pending. `studio/glyphs.ts` is
the HUD icon set. Hotkeys (never while typing): 1-5 journal tabs, T chat, J journal,
N new goal, Esc window/menu; WASD/arrows walk the avatar, E interacts, +/-/0 zoom.
The menu's animation pause sets the root's `data-motion`, stilling HUD animations
with the world. Wide screens show one HUD bar; below 1024px the stage track and
meters (`.evo-hud-status`) wrap in their own row under the quest.

`engine/` is the game engine. `loop.ts` advances fixed 60 Hz steps over visible
time and sleeps without callbacks when the world reports no change, waking on one
timer for the next emote/screen keyframe; `camera.ts` fits the world height inside
the HUD frame, pans/zooms/clamps and follows the scene's focus when the room
overflows (portrait); `pointer.ts` recognizes tap, drag-pan, wheel and pinch;
`paint.ts` draws in-world plates, bubbles, emotes, hotspot brackets and the blurred
backdrop; `particles.ts` bounded event bursts; `stage.ts` hosts one scene on one
device-resolution canvas with an accessible button layer mirrored over scene
targets (`.evo-target`) that re-project on any origin/scale change (camera, eased
HUD insets, viewport), held movement keys and teardown. Hidden or offscreen
stages own no callbacks; reduced motion settles every beat at once; a prop slide
to where it already is settles at once; the avatar's floor tap wakes frames only
while it walks.
`office-scene.ts` is the main office scene. Resident poses follow the projected
roles: per-role workstations, waits (the asking role gathers at the front door; a
compute or background wait keeps the executor at its lit desk), emotes, stationary
idle poses, swarm visitors entering and leaving through the front door (roster from
the task's swarm status, capped at eight including departing avatars), and the
per-actor action sequencer: chairs are rolling props with pull, sit, roll-in,
roll-out, stand and push beats; interruption preserves the current foot/seat pose.
The first eight durable material IDs are books: newly added agent links travel from
the executor's desk to the pile beside the bookshelf, and removals retain the
remaining link identities. New assignment journal sequences send the coordinator to
the executor once; research/coordination/delegation tool events route the executor.
The human's avatar is the only character the human steers; pointer activation of
furniture also walks it there. `office-layout.ts` owns the art-aligned hotspots,
stations, choreography points and prop anchors (walkability checked by
`testing/office-scene.test.cjs`). `office-objects.ts` owns the individually
controllable prop registry (chairs, books: id/kind/state/eased slides, no DOM or
timers) and draws the authored rolling chair; `office-navigation.ts` owns
furniture-aware paths with exact endpoints and collision-checked connectors.
`office-motion.ts` owns distance-based gait and eased seat poses;
`office-sprite-geometry.ts` owns side-frame source-row repair and fixed-scale foot
anchors. `office-character.ts` maps live roles to eight distinct character models;
`office-character-art.ts` lazily paints their four-direction walking sheets.
`office-game-frames.ts` is generated alpha metadata for `assets/game-*.webp`;
regenerate/check it with `scripts/generate_evolution_game_frames.py`.
`office-sprites.ts` and `people.png` are the network-failure fallback only.
`room-catalog.ts` owns six fixed rooms, scenery, role routing and the localized job
title (`roleTitleKey`, by room theme) that names free-form swarm roles everywhere; `office-rooms.ts`
owns the room tiles, selected room, member card and roster reconciliation; the
studio swaps the stage scene on selection. Same-role members have distinct briefs.
Empty rooms remain visitable; unavailable roster data stays unknown. Only the
selected department room has a scene. `agent-pod.ts` is that scene: it selects
lazy chair-free `assets/pod-*-clear.webp` scenery from the agent's role and draws
only status-dependent actor/screen details over it; a same-agent pending-to-running
or running-to-terminal change plays one bounded arrive/leave chair beat; initial
snapshots settle immediately. Room thumbnails are packaging derivatives of art.
`assets/office-clear.webp` is shipped chair-free generated scenery;
`assets/office.png` remains its comparison source. `assets/people.png` is a
six-character sprite atlas derived from generated art by
`scripts/generate_evolution_people.py`; `assets/props.png` is the nine-prop
chair/book atlas drawn deterministically by `scripts/generate_evolution_props.py`
(the PROPS bounds in `office-objects.ts` are its printed output, drift-checked
by `tests/evolution/test_evolution_props_generated.py`).
`goal-room.ts` owns the journal: library, plan, execution, review and delivery tabs,
per-goal material forms and the two-step stop; late writes never repaint another
goal. `studio/goal-run-inspector.ts` renders section-scoped inspection (status and
waits, latest work, usage with the compute resource launcher, records with the
`studio/outcome-reader.ts` island, review, deliverable).
`components/` renders evidence and forms with injected API and
navigation ports. The workbench never installs itself on document.body.
The lazy `../evolution.ts` entry alone bridges to the application shell.

Authority: `docs/EVOLUTION_DESIGN.md`, `contracts/evolution_*`, and the shared
typed API. A verdict is not a task state, and a proposed skill is not installed.
Unmeasurable validation requires a separate explicit waiver choice.

Verify controller/lifecycle and decision tests, then the real-server workspace
browser journeys, `npm run check:frontend`, the frame metadata check, and the visual tier.

Pure specifications: `node --test frontend/src/features/evolution/testing/{projection,engine,office-scene,office-rooms,office-pod-motion,goal-room,studio}.test.cjs`.
`tests/evolution/test_evolution_office_browser.py::test_office_runtime_deterministic_render_cost`
uses the local source preview and a bounded 60 Hz virtual clock with real Canvas2D.
It writes CPU callback timings, paint counts and teardown evidence to
`output/evolution-design/runtime/deterministic-render-cost.json`; timings measure
CPU submission, not GPU/display FPS. Production bundle/browser integration is a separate gate.

Independent design preview: run `node_modules/.bin/vite --config frontend/src/features/evolution/testing/vite.config.mjs` and open
`/preview.html` on the printed local URL. This mounts the same page with synthetic
read-only fixtures. Test URL parameters select fixtures: `?scenario=program`
(notebook), `empty`, `failure`, `terminal`, `legacy-failed`, `needs-attention`,
`swarm`, `compute`, `compute-error` and `question`; preview commands cannot call the
production API.

Programme intake reuses `features/skills/selection.ts` and the installed-skills API;
required IDs and research inputs use existing owned drafts. Shared source schemas
and backend admission own guide hashes/tool bindings; the UI never infers passed
checks from selection. Verify Programme catalog, handoff and stale-read scenarios.
