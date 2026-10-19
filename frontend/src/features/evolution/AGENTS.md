# Evolution workbench

`workspace.ts` lazily composes `studio.ts` as the primary `#/evolution` surface;
the durable program notebook remains explicitly available at `#/evolution/programs`.
`programs/experiment-charts.ts` projects bounded saved measurements through the shared rich-response chart presenter. `evidence-charts.ts` admits complete delivery charts beside exact originals, without transport or timers. Studio styles live in `frontend/src/styles/application/26-studio-*.css`; notebook observations use `26-evolution-observations.css`.
`studio/command-center.ts` owns the persistent conversational briefing and
receipt-backed stage HUD, without transport or timers.
`26-studio-command-center.css` owns the responsive mission/scene composition.
Compact inspection disclosures preserve focus and expansion for the selected
goal; the full inspector retains source-reading authority. Real-browser theme,
contrast and draft checks live in `test_evolution_studio_browser.py`. Intake lives beside the
scene, never in an entry modal; explicit dispatch retains idempotent retry keys.
`route-host.ts` owns pending-route epochs, one mounted page and teardown; each
route imports its own graph through the shared document-lifetime module cache.
`studio-entry.ts` alone installs studio controls/theme. Child import failures
use the shell's installed reload authority; no route creates its own guard.
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
`controller.ts` owns the detailed-page read lifecycle. The studio owns one
visible-page read flight, stable input islands, idempotent submission retries and
real task role markers; it reserves a mission header, room rail, scene and tool shelf. Goal selection
and management actions live in the menu drawer; the task heading opens details.
`goal-room.ts` owns furniture navigation, per-goal shelves
and material forms; late writes never repaint another goal. `office-scene.ts` owns the bounded Canvas animation,
actor buttons and teardown, per-role workstations, activity
emotes, stationary idle poses, the swarm visitor door (roster
from the task's swarm status, capped at eight including departing avatars), and the
per-actor action sequencer: chairs are rolling props with pull, sit, roll-in,
roll-out, stand and push beats; interruption preserves the current foot/seat pose.
Only visible-time frames advance beats; hidden/offscreen scenes own no callbacks. The first eight durable material IDs
are books: newly added agent links travel from the executor's desk to the
shelf, and removals retain the remaining link identities. New assignment
journal sequences send the coordinator to the executor once.
`office-objects.ts` owns the individually controllable
prop registry (chairs, books: id/kind/state/eased slides,
no DOM or timers) and draws the authored rolling chair; `office-navigation.ts` owns
furniture-aware paths with exact endpoints and collision-checked connectors.
`office-motion.ts` owns distance-based gait and eased seat poses;
`office-sprite-geometry.ts` owns side-frame source-row repair and fixed-scale foot anchors.
`office-canvas.ts` owns capped backing storage, dirty paint signatures and constant-space
counters, not a ticker or task runtime. `office-character.ts` maps live roles to eight distinct
character models; `office-character-art.ts` lazily paints their four-direction
walking sheets in both room canvases. `office-game-frames.ts` is generated alpha
metadata for `assets/game-*.webp`; regenerate/check it with
`scripts/generate_evolution_game_frames.py`. `office-sprites.ts` and `people.png`
are the network-failure fallback only. `room-catalog.ts` owns six fixed rooms, scenery and role routing; `office-rooms.ts`
owns the always-visible directory, selected room and roster reconciliation.
Same-role members have distinct briefs inside their department room. Empty rooms
remain visitable; unavailable roster data stays unknown. Only the selected pod
has a renderer. Desktop uses a room rail; narrow screens use a labelled Rooms
button and the same directory in a drawer, while the member brief stays visible
in the tool shelf. Room thumbnails are packaging derivatives of authored art.
`assets/office-clear.webp` is shipped chair-free generated scenery;
`assets/office.png` remains its comparison source. `assets/people.png` is a
six-character sprite atlas derived from generated art by
`scripts/generate_evolution_people.py` (roles own sprites 0-2, visitors cycle 3-5);
`assets/props.png` is the nine-prop chair/book atlas drawn deterministically
in the office woodwork palette by `scripts/generate_evolution_props.py`
(the PROPS bounds in `office-objects.ts` are its printed output, drift-checked
by `tests/evolution/test_evolution_props_generated.py`).
`assets/pod-*-clear.webp` are lazy, chair-free 4:3 room backgrounds for the rostered
sub-agent offices. `agent-pod.ts` selects one from the agent's actual role and
draws only status-dependent actor/monitor details over it; the books in room
scenery are ordinary furniture, not a goal-material count. The selected WebP
loads on first entry to a pod, with a procedural fallback if unavailable. A
same-agent pending-to-running or running-to-terminal status change drives one
bounded arrival/leave chair sequence; initial snapshots settle immediately.
`ui-components.ts` registers the pinned, studio-only Web Awesome controls and scoped
theme; `studio.ts` owns drawer routing and viewport teardown.
`components/` renders evidence and forms with injected API and
navigation ports. The workbench never installs itself on document.body.
The lazy `../evolution.ts` entry alone bridges to the application shell.

Authority: `docs/EVOLUTION_DESIGN.md`, `contracts/evolution_*`, and the shared
typed API. A verdict is not a task state, and a proposed skill is not installed.
Unmeasurable validation requires a separate explicit waiver choice.

Verify controller/lifecycle and decision tests, then the real-server workspace
browser journeys, `npm run check:frontend`, the frame metadata check, and the visual tier.

Pure runtime/scene/room specifications: `node --test frontend/src/features/evolution/testing/office-{runtime,scene,rooms}.test.cjs`.
`tests/evolution/test_evolution_office_browser.py::test_office_runtime_deterministic_render_cost`
uses the local source preview and a bounded 60 Hz virtual clock with real Canvas2D.
It writes CPU callback timings, paint counts and teardown evidence to
`output/evolution-design/runtime/deterministic-render-cost.json`; timings measure
CPU submission, not GPU/display FPS. Production bundle/browser integration is a separate gate.

Independent design preview: run `node_modules/.bin/vite --config frontend/src/features/evolution/testing/vite.config.mjs` and open
`/preview.html` on the printed local URL. This mounts the same
page with synthetic read-only fixtures and the fixed light frosted theme.
Select the notebook with `?scenario=program`. Tests select empty/error fixtures with `?scenario=empty` / `?scenario=failure`; preview commands cannot call the production API.
