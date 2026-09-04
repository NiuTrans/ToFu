# Production substrate

This domain is the shared execution layer for long-running “prompt to
deliverable” capabilities. It owns job lifecycle, deterministic stage graphs,
checkpoint recovery, deduplication, and shared content/research contracts.
Motion video, slides, long-form reports, research, and paper podcast remain
separate recipes with their own quality rules.

## Ownership

| Concern | Owner |
|---|---|
| Stage graph and checkpoint file | `lib/production/stages.py` |
| Long-job registry, deduplication, retention | `lib/production/runtime.py` |
| Crash-resume manifest and rescan | `lib/production/jobs.py` |
| Media-neutral narrative/source contracts | `lib/production/contracts.py` |
| Time-aware research evidence and gates | `lib/production/research.py` |
| Finite background model/image transport budgets | `lib/production/llm_policy.py`, `lib/production/image_policy.py` |
| Generic task discovery and streaming | `routes/api_v1/tasks.py`, task runtime |
| Capability recipes | `lib/motion_video/`, `lib/slides/`, `lib/longform/`, `lib/research/`, `lib/paper/podcast_engine/` |
| Binary publication | each capability’s artifact/deliverable service |

`lib.production` is the only public Python entry point for shared production
contracts. A capability imports the specific owner module when it needs a
narrow dependency. Historical capability-local copies and import shims are not
part of the architecture.

Both `lib.production` and `lib.motion_video` are lazy compatibility facades.
A focused capability-runtime import establishes the durable task/dedup
authority without importing stage graphs, research, crash-resume scans,
render-chain probes, audio processing, or quality recipes. Those owners resolve
at the explicit recipe/request boundary; adding a package export must not turn
route registration into recipe initialization.

## Execution model

1. The capability validates the request and derives a stable deduplication key.
2. Capabilities with a mandatory local render chain prove it before task
   creation. Slides import Playwright, prepare native Chromium paths, and smoke
   launch Chromium once per worker process; a wrong `.tofu_env.json` authority
   therefore fails at admission instead of after research and page authoring.
3. `ProductionRuntime` atomically joins existing live work or creates one task.
4. The capability writes a minimal owner-scoped job manifest before spawning.
5. Its recipe invokes `run_stages`, or `run_independent_stages` for true
   siblings, with explicit names, gates, retry limits, checkpoint versions,
   freshness windows, and a bounded worker budget.
6. Each successful stage atomically commits one JSON-serializable checkpoint.
7. Capability code publishes a final artifact only after its final quality
   gate passes. Publication is a terminal resumable stage: an unconfirmed write
   blocks success, while its bounded retries never repeat upstream model work.
   The capability then settles the generic task exactly once.
8. Startup rescans manifests still marked running and reconstructs their tasks;
   the stage graph skips only valid complete checkpoints.

Heavy media is referenced by path/artifact identity, never embedded in stage
JSON, task events, or logs. A process exit code alone is not a quality verdict.

Long-form report starts atomically join identical live work. Their depth profile
fixes 3/5/8 unique headings. Research checkpoints expire after six hours;
section checkpoints bind every prompt input, and assembly checkpoints bind
every Markdown input. One section batch constructs its immutable report/evidence/
instruction prefix once and appends only the heading-specific task; the prompt-layout
revision invalidates older section checkpoints. This preserves independent quality
gates and resume while exposing the full research packet as a provider-cache prefix
instead of diverging before it. A conversation-scoped report settles only after its
artifact ID is confirmed. Independent judge/section calls use the launch-probed
1..2 personal per-job fan-out (distributed 4, hard ceiling 8); task admission
bounds the process-wide multiplier. Section calls overlap without increasing
their logical count, so N sequential model waits become a work-conserving
bounded batch. Reconstructible enrichment uses strict billing-stop admission,
a launch-probed finite 429 allowance, and immediate yield to known shared
contention; durable background production instead caps actual upstream 429s at
4..8 on personal machines (distributed 16, hard ceiling 64) with two hard-error
slot attempts, while interactive chat retains its separate retry policy. Slide
outlines, page authors, and QA repairs use it. Independent pages overlap behind the
1..2 personal LLM fan-out and checkpoint immediately by exact prompt/model/
round policy plus bounded YAML hash and zero-LLM validation; only authored
pages are reusable, so a restart or one corrupt/changed page does not repay for
the rest of the deck and a transient fallback gets another chance.
Slide publication is strict at semantic boundaries: every page must pass the
author contract after one missing-page retry, every page must render a preview,
and deterministic layout QA must clear all remaining overflow/collision
findings. Fallback YAML remains only as a valid retry diagnostic; it cannot
produce a successful PPTX. Visible text rejects internal planning phrases and
research IDs, while chart values absent from the page evidence are returned to
the author instead of shipping invented benchmark scores. Generated-asset or
optional VLM outages remain explicit `artifact_quality` degradation evidence.
Page-author prompts place the immutable deck/theme/text-style/grounding/design-
bible/PPTD contract before page number, brief, assets, and sources. Each page
remains independently hashed, cached, repaired, and checkpointed; an input
version change invalidates pages authored under an older prompt layout. One
frozen batch context reads and formats the theme, bible, and cheatsheet once;
cache preflight and bounded page workers share it without retaining full prompts.

Slides and topic-driven motion video accept `creative_mode=director|standard`.
`standard` is the one-plan A/B control. `director` sequentially drafts two
contrasting plans, screens each through the existing deterministic
fact/current-state gate, then spends at most one bounded critic call to select
the stronger valid candidate. The selected plan records candidate count,
winning lens, reason, fallback scores, critic scores, and usage; failed critics
fall back to a deterministic richness/diversity score. Creative mode is part
of dedup and outline/script checkpoint identity, so switching an experiment
arm cannot reuse the other arm's plan. Direct legacy/internal calls that omit
the field retain one-plan behavior, while public production defaults to
`director`.

A background slide task mints one bounded model-routing-v2 group from the
explicit task owner and carries its provider pin into every worker thread.
Outline, page authoring, image generation, edit, and visual QA therefore use
the initiating user's authorized paid or local routes instead of process-global
credentials. The route group is disposed when the stage graph settles. A
candidate that cannot initialize is retained as bounded degradation evidence
and skipped; only the runnable authorized candidates enter dispatch fallback.
The high-level producer tool returns one accepted-task receipt. Once accepted,
root chat cleanly ends that model turn; the generic task stream/panel owns all
subsequent progress and terminal quality. The model does not poll the task in a
second loop or infer completion from intermediate files.

PPTD pages may additionally declare semantic `metric`, `quote`, `comparison`,
`timeline`, `process`, and `code` components. Parsing expands them once into
the existing editable text/shape/line primitives before validation, preview,
repair, and PPTX export; no second renderer model or flattened page is created.
Native category charts support bar, column, line, pie, area, doughnut, and
radar in both SVG preview and OOXML export. Deck plans carry an explicit visual
modality, visual anchor, and next-page handoff so independent page authors
receive global continuity rather than layout-only hints.

Public production PPTX export is portable-font strict. Every effective family
and style slot is collected from actual text/table runs, common audited CJK
aliases are canonicalized to the embedded face's real family name, and the
artifact records used, embedded, and missing families. Any missing family
fails the export gate instead of silently relying on Office substitution.
Two-point arrows are native connectors with exact endpoints; semantic process
chevrons carry an explicit adjustment shared by browser preview and OOXML.
For decks of at least four pages, the outline gate also requires at least four
layout archetypes and four visual modalities before page authoring begins.

Shared production research retains 12 rows per query lane and reuses discovery titles; survey/harvest use one
owner-scoped arXiv-indexed projection, metadata-only for harvest; survey uses one
body plus one batched report query, capped at 6,000 chars without auxiliary JSON.
Survey gates cap 12 clusters/40 matrix rows/20 gaps/40 ids per entry and ground 20 unknown ids in one request with visible truncation. Standalone research keeps
3..12 ideas, 20 seeds and 2,000-char directions with exact manifest identity.
Task affinity/memo caps prior probes at 20; a serial rubric warms its prefix before launch-probed 1..2 bounded judge workers.
Research Foundry actions are ordinary `research-action` production tasks but are deliberately not crash-replayed after an external write may have started: an unknown third-party MCP tool cannot be assumed idempotent. The owner must inspect its external state and explicitly start a new action. Workspace CAS then prevents the result of a long action from overwriting edits made while it ran. Tool output excerpts/digests and provider artifact references settle into the versioned program; task streams remain bounded progress projections.
Slide image generation has its own launch-probed 1..2 personal fan-out
(distributed/hard ceiling 4), 4..8 personal upstream-429 allowance
(distributed 16, hard ceiling 64), and two total hard-error attempts. A deck
admits at most six generated images and 20 caller images; individual images
are capped at 20 MiB and caller/remote channels at 192 MiB. Downloads and local
copies stream to atomic files. Generated and remote caches bind semantic input,
path, byte size, and SHA-256; URL-derived remote filenames prevent restart
collisions, and stale reconstructible files are reclaimed. Independent page
VLM reviews use the same 1..2 LLM fan-out; exact prompt/model/token/pixel
digests reuse only validated structured findings, while a changed page
invalidates that page plus the deck contact sheet. Visual QA rejects previews
above 16 MiB before base64 allocation. Preview rendering bounds geometry,
pages, and PNG bytes before Chromium allocation/publication, checks abort
between pages, and waits on actual fonts/images/layout instead of adding a
fixed 400 ms sleep to every page.

Independent podcast segments and motion-video scenes use a separate 1..2 personal TTS
fan-out (distributed 4, hard ceiling 8), because audio-byte residency and
provider capacity are not LLM token budgets. Motion narration persists a
versioned text/timing/settings/file-hash manifest immediately after synthesis,
so only an exact bounded checkpoint can suppress paid TTS on resume.
Motion scene-author prompts put the shared frame, hard requirements,
composition contract, craft guide, and skeleton before scene identity,
duration, narration, assets, and frame packet. Each scene retains its own
bounded agent loop, quality gate, draft recovery, and template degradation;
prompt prefix reuse never makes one scene depend on another's output. One
frozen, lazily prepared film context reads and formats that prefix once for
initial authors and visual-QA repairs; a fully resumed film does not prepare it.
Missing scene authors overlap behind a work-conserving window capped at the
smaller of the launch-probed text and image budgets and a hard two-worker
ceiling. Shared craft/font stores prewarm on the caller thread; an incomplete
first install forces one worker. Results pass static gates and the no-regression
commit on the caller thread before another scene is admitted. Abort or fatal
commit failure stops queued admission, while active drafts remain recoverable.
The selected motion blueprint already travels in each frame packet. The deeper
optional craft catalog is installed and indexed only for scenes that explicitly
set `allow_craft_browse`; ordinary production scenes perform neither operation.
Storyboards also preserve bounded renderer-neutral `media_queries` and a visual
modality. The deterministic film plan records ordered renderer candidates
(HyperFrames is the installed/resumable adapter; Remotion, Motion Canvas, and
Manim are optional future lanes) and injects the request into the scene frame
packet. At most one stock request per scene is materialised into lifecycle-bound
scene storage. Pexels photo/video retrieval is optional behind
`PEXELS_API_KEY`, uses only trusted HTTPS Pexels media hosts, accepts at most
16 MiB per image or 32 MiB per video, and records provider/creator/page links in
`media_attribution.json` plus a user-downloadable text ledger, and the silent
source card visibly adds `Media: Pexels.com`. Provider absence,
web-capture absence, bad MIME, oversize data, or outage is an explicit quality
finding rather than an invented path. No credential enters a task, checkpoint,
log, or attribution record.
Motion visual QA stores at most one successful result per scene behind a 64 KiB
file cap. Exact contact-sheet pixels, prompt/theme, model, and dispatch settings
must match before a resume skips its VLM call; changed pixels miss, outages are
not cached, and cached major/blocker findings still enter the author repair.
The screenshot remains required evidence and is not bypassed by this cache.

## Stage contract

A `Stage` declares:

- a stable semantic name;
- `run(context) -> artifact`;
- an optional deterministic `gate(context, artifact) -> errors`;
- a bounded retry count;
- whether it is resumable;
- an optional freshness TTL and semantic checkpoint version.

When a stage is absent, stale, version-mismatched, corrupt, or fails its gate,
that stage and its dependent suffix are recomputed. Upstream checkpoints that
remain valid are preserved. Cancellation is checked between stages and inside
long-running capability operations.

`run_independent_stages` is reserved for siblings that read a frozen upstream
context and never one another. It admits at most the declared worker budget,
commits each passing sibling separately on the caller thread, and invalidates
only that sibling plus named downstream dependents. Once a failure or abort is
observed, queued siblings are not admitted; already-running successes retain
their checkpoints for a cheaper retry.

## Capability boundary

The substrate contains a primitive only when multiple independent recipes need
the same semantics. It does not own:

- video timelines, scene authoring, rendering, subtitles, or muxing;
- slide layout or deck rendering;
- podcast script, TTS, or audio joining rules;
- long-form outline, prose, or document export rules;
- capability-specific binary formats or user-facing artifact projections.

Sharing a filename or similar code shape is not enough to move behavior into
the substrate. The shared contract must have the same lifecycle, failure, and
recovery semantics.

## User experience and failure semantics

- Repeating an identical request joins visible in-flight work instead of
  silently spending twice.
- Progress is expressed as stable task events and semantic stages, not a
  fabricated timer.
- Abort prevents later callbacks from publishing or changing terminal state.
- A gate failure reports the stage and actionable findings; partial output is
  retained for diagnosis/resume but never presented as the finished product.
- A restart resumes from durable manifests/checkpoints when the capability
  declares recovery; it never claims process-memory work survived.
- Missing artifact bytes, malformed manifests, and corrupt checkpoints are
  explicit failures, not empty success responses.

## Invariants

- One shared stage implementation and one long-job lifecycle implementation.
- Dedup claim/create/register is atomic with respect to competing submissions.
- Dedup retention follows the resolved task-registry target unless a recipe lowers it; it cannot re-widen the shared policy, while active keys may exceed the target and are never evicted merely to satisfy it.
- Checkpoints are atomic, complete, JSON-serializable semantic boundaries.
- A changed upstream contract invalidates its dependent checkpoint suffix.
- Generic task routes discover runtimes by their declared `kind`; route code
  does not maintain a parallel hard-coded lifecycle.
- Runtime discovery does not initialize recipe, render, research, or recovery
  modules.
- Owner identity and artifact provenance survive every handoff.
- Recipe-specific quality rules do not leak into the horizontal substrate.

## Change routing

| Change | Start here | Verify |
|---|---|---|
| Stage/recovery semantics | `lib/production/stages.py` | resume, suffix invalidation, gate, abort |
| Dedup/retention/task lifecycle | `lib/production/runtime.py` | concurrent claim, terminal pruning, capacity |
| Restart behavior | `lib/production/jobs.py` | corrupt manifest, idempotent rescan, respawn failure |
| Shared content contracts | `contracts.py`, `research.py` | every consuming recipe and finite validation |
| One media recipe | its capability package | quality gate, artifact publication, task settlement |
| Generic task projection | `routes/api_v1/tasks.py` | discovery, events, stream, abort |

## Test map

```bash
pytest -q tests/test_production_substrate.py tests/test_production_runtime.py
pytest -q tests/test_task_registry_discovery.py tests/test_production_lifecycle.py \
  tests/test_production_startup_boundary.py
pytest -q tests/test_motion_video_p4.py tests/test_motion_video_p5.py
pytest -q tests/test_paper_podcast_api.py tests/test_longform_p7.py
pytest -q tests/test_slides.py tests/test_slides_creative_foundation.py \
  tests/test_slides_research_foundation.py tests/test_visual_qa.py \
  tests/test_slides_semantic_components.py
pytest -q tests/test_production_creative_director.py
```
