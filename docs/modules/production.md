# Production substrate

This domain is the shared execution layer for long-running “prompt to deliverable” capabilities. It owns job lifecycle, deterministic stage graphs, checkpoint recovery, deduplication, and shared content/research contracts. Motion video, slides, long-form reports, research, and paper podcast remain separate recipes with their own quality rules.

## Ownership

| Concern | Owner |
|---|---|
| Fixed agent-tool definitions, typed nodes and stage compilation | `lib/production/workflows.py`, `contracts/agent_tool_workflow_v1.schema.json` |
| Stage graph and checkpoint file | `lib/production/stages.py` |
| Demand workers and joined pool observations | `lib/production/concurrency.py` |
| Long-job registry, deduplication, retention | `lib/production/runtime.py` |
| Crash-resume manifest and rescan | `lib/production/jobs.py` |
| Media-neutral narrative/source contracts | `lib/production/contracts.py` |
| Time-aware research evidence and gates | `lib/production/research.py` |
| Finite background model/image transport budgets | `lib/production/llm_policy.py`, `lib/production/image_policy.py` |
| Generic task discovery and streaming | `routes/api_v1/tasks.py`, task runtime |
| Capability recipes | `lib/motion_video/`, `lib/slides/`, `lib/longform/`, `lib/research/`, `lib/paper/podcast_engine/` |
| Binary publication | each capability’s artifact/deliverable service |

`lib.production` is the only public Python entry point for shared production contracts. A capability imports the specific owner module when it needs a narrow dependency. Historical capability-local copies and import shims are not part of the architecture.

Both `lib.production` and `lib.motion_video` are lazy compatibility facades. A focused capability-runtime import establishes the durable task/dedup authority without importing stage graphs, research, crash-resume scans, render-chain probes, audio processing, or quality recipes. Those owners resolve at the explicit recipe/request boundary; adding a package export must not turn route registration into recipe initialization.

**Task inventory — Problem:** generic task listing built public rows and compact
WorkUnits for every owned match before discarding rows outside the requested page.
**Decision:** filter each owned runtime snapshot, retain the exact total, apply the
same stable descending `created_at or 0` ordering, and project only the requested
page. Kind fallback, status/metadata filters, optional-runtime failure isolation
and the separate artifact-quality axis remain unchanged. A selected row's
projection failure still propagates. **Alternatives rejected:** an early scan
cutoff loses the exact total and newest rows; another cache adds identity and
invalidation owners; a heap is unnecessary to remove the dominant projection work.
Specs: `tests/tasks_pkg/test_task_list_workspace_filter.py` and the shared
WorkUnit/quality-axis tests. `benchmarks/owned_inventory_reads.py` compares frozen
trees with identical response and fixture digests; its counts and warm CPU clocks
exclude storage, HTTP encoding and startup.
Three alternating fresh-process pairs on the same seeded 1,000/10,000-task
fixtures (80% owned, requested page 50) reduced compact projections from
800/8,000 to 50. Median per-process CPU medians were 1.329→0.285 ms and
17.125→3.735 ms; all complete-response digests agreed. The 10-task control
still projected all eight owned rows (0.023–0.037 ms before, 0.024–0.026 ms
after). These are warm component observations, not application throughput.

**Motion identity — Problem:** topic launches, recovery and asset revisions
overwrote TaskRuntime's `kind` with source/asset choices, breaking the declared
`motion-video` work-unit identity and video download projection.
**Decision:** runtime `kind` stays `motion-video`; `source_kind` describes film
input and `asset_kind` describes a revision's retrieval choice. Manifests write
`source_kind` plus the historical `kind` field for existing disk readers;
recovery prefers `source_kind` and maps old source-valued `kind` without
overwriting runtime identity. Tool input `style.kind` remains unchanged.
The added field measured 26 bytes for `topic`/`paper` and 27 for `scenes` with
the shipped JSON writer; it stays in the one overwritten `job.json`, with no
new file, history or retention policy.
**Alternative rejected:** permitting reserved-field writes or compensating in
each API projection leaves the task carrier's identity inconsistent.
Specs: `test_motion_video_identity.py`, asset-revision and work-unit contracts.

**Initial-film admission — Problem:** HTTP, paper abstracts and Chat tools each
minted tasks, prepared job files, mutated live task fields and submitted workers;
HTTP check/create could race, and preparation failures left pending tasks.
**Decision:** `lib/motion_video/service.py::start_motion_video` owns that sequence
through the existing production claim and TaskRuntime. It scopes dedup keys to
the explicit owner, supplies the complete engine parameters to the claim,
writes sources only for a winning claim and settles preparation/submission
failure through the runtime. HTTP retains request validation and source identity;
paper retains report/source/budget gates and its early lookup before model work;
Chat requests fresh work with storyboard review and explicit visual quality.
Paper force replaces its live index; podcast force bypasses completed cache only.
Initial films use one worker entry; recovery and revision tasks keep their own
domain entry points. Joined starts create no job directory and perform no worker
submission; a new initial film retains the same one directory/source write as
before. No renderer, stage, worker quota or provider transport is added.
**Alternatives rejected:** a generic callback launcher hides domain policy;
keeping three submit sequences lets their cancellation/error ownership diverge.
Specs: `test_motion_video_admission.py`, paper media, engine and API contracts.

**Author-gate unit verification — Problem:** the synthetic neuter case repeated
a tool reply while opening real Chromium for advisory fill, coupling a static
quality assertion to browser teardown and timing out under host I/O pressure.
**Decision:** the paired specification feeds deterministic browser measurements
and renderer reports into the real author/quality logic, then lets the fake model
finish naturally. The same junk HTML must degrade with the static gate intact
and be admitted only when that gate is deliberately disabled. Browser-backed
fill and publication remain covered by their own suites.
**Alternative rejected:** stubbing the final verdict or increasing timeouts
would hide a missing quality gate or leave the irrelevant browser dependency.

**Verified film revision — Problem:** revision workers duplicated the final
audio-selection path and wrote into the published film before checking success
or cancellation. A partial codec write could therefore replace a valid film;
an `ok` result without a new file could falsely reuse the previous deliverable.
**Decision:** `lib/motion_video/media_publication.py` owns copy/mux/mix selection,
byte receipts, media observation and atomic replacement for initial films and
revisions. `revision_media.py` owns one private candidate directory per active
revision. The shared timeline/media gates check dimensions, finite frame rate
and duration, and requested audio before publication; byte receipts bind the
probe and publication to the same file. Failed or cancelled attempts retain the
last published film and clean only their own candidate. Worker task settlement,
initial film stages/checkpoints and domain quality gates keep their existing
owners. Scene, narration and subtitle inputs remain separately owned preparation
state; this boundary does not make those earlier edits transactional.
Each active revision adds one reconstructible candidate file in its own
directory beside the published film; active disk use therefore follows output
size and accepted concurrent work, without a capacity admission limit. The
three worker specifications measure one directory while probing and zero after
normal settlement, with the synthetic candidate's byte size recorded in JUnit.
Cleanup errors are logged without replacing the original task outcome. Abrupt
process termination or denied cleanup can leave an orphan directory; there is
no automatic orphan sweep in this boundary. An operator may remove a named
`.revision-media-*` directory only after verifying its worker has ended; never
remove the parent job or a sibling's live candidate to reclaim this scratch.
**Alternatives rejected:** backing up after a failed in-place write exposes
partial media first; copying publication logic into each revision repeats the
gap; serializing all revision tasks is unnecessary for candidate ownership.
Specs: `test_motion_video_revision_publication.py`,
`test_motion_video_workflow.py`, `test_motion_video_edit.py`.

Human outline/storyboard review registers the owner-bound guidance waiter before
publishing its card and passes the recipe's cancellation probe into the wait.
Cancellation, admission rejection, or an unavailable response never approves
production or writes a successful gate checkpoint; publication failure releases
the waiter. See [execution modes](../TASK_EXECUTION_MODES.md#publish-only-answerable-human-requests).

## Agent-based tools

**Problem:** a semantic tool can contain model work without being a free-form
delegated agent. Unclassified recipe callbacks obscure node boundaries, permit
malformed outputs to reach later stages, and make graph changes invisible to
checkpoint reuse. Ending Chat as soon as one producer accepts work also drops
later actions in the same human request.

**Decision:** `AgentToolWorkflow` is an authored sequence or independent sibling
batch of `WorkflowNode(Stage(...), execution, output_schema)` declarations.
Its JSON descriptor follows `agent_tool_workflow_v1.schema.json`. Definition,
callback bindings and all output schemas are validated before the first node.
Output must be finite JSON and pass its schema before the existing domain gate.
Models may generate content, evaluate it or propose changes inside a node;
they cannot supply callbacks, change the next node, omit gates or declare the
artifact delivered. Input admission, explicit owner/model/tool permissions and
final artifact acceptance retain their existing domain owners.

`run_workflow` compiles to `run_stages` or `run_independent_stages`; it owns no
model loop, executor, task, timer or durable status. Model nodes reuse existing
dispatch/invocation and cancellation ports. Independent work starts on demand;
data-dependent fan-out is constructed by trusted recipe code from validated
upstream data. Descriptions and workflow-start events are provenance, never
execution or scientific-success evidence.

Each checkpoint carries a contract fingerprint over workflow identity, revision,
node schema/policy and the preceding sequence. A changed node invalidates its
dependent suffix. Independent siblings have separate fingerprints. Legacy
checkpoints retain their existing semantic version/TTL checks and must pass the
new structural and existing domain gates before adopting a fingerprint; adoption
preserves the original timestamp and does not replay model or publication work.
An adoption write failure leaves the original checkpoint intact and refuses
progress. The existing job owner still owns immutable input/workdir identity.

Retention measurement: a synthetic ten-node graph adds 970 bytes to the existing
JSON checkpoint file (one 64-hex contract field per node), with no extra files or
checkpoint rows and unchanged checkpoint timestamps. Its compact UTF-8 workflow
descriptor is 1,679 bytes. Workflow-start events share the existing bounded task
event retention; they do not copy artifacts, model messages or lifetime history.
This measures serialized metadata, not rendering throughput or model quality.

Current adopters are the complete slides and film graphs, research deliverable
stages, and long-form planning, independent sections and assembly. Film production
includes storyboard, human review, narration, timeline, composition, rendering,
concatenation, subtitles, muxing, final verification and publication. The topic
research/script/timeline recipe remains a nested planning workflow; its receipt
alone is not film delivery.
Semantic `produce_*` tools remain separately discoverable in normal Chat.
Acceptance returns the existing task handle and allows Chat to complete other
requested actions. Production continues after the reply and reports its actual
quality through the existing WorkUnit. Explicit dependency waits retain their
separate suspension policy. No mode switch or repeated model polling is required.

**Alternatives considered:** a free `spawn_agent` prompt would leave graph and
acceptance to the model; a second workflow engine would duplicate recovery and
authority; metadata without runtime validation would not prevent invalid
handoffs; forcing a Chat stop on every accepted task loses compound requests.
Structural validation does not establish visual quality, factual correctness,
or research capability growth; existing quality/evidence owners still do that.

### Film workflow and publication

**Problem:** the film worker kept rendering, assembly and delivery inside one
imperative function after the declared storyboard workflow. Scene-level media
checks did not validate the final muxed film, and assembly could overwrite an
existing published file before a failure or cancellation was observed.

**Decision:** `lib/motion_video/_film_workflow.py` declares eleven typed nodes
over the same stage executor. `engine.py` retains job settlement and revision
entry points. Model calls, browser/codec subprocesses, joined scene workers,
human approvals, quality degradation and no-regression scene commits retain
their existing domain owners. Each boundary records finite JSON artifacts;
binary data stays in files. Film nodes explicitly re-enter their callbacks on
recovery: existing storyboard, approval, TTS, composition and media caches decide
what remains valid. A completed orchestration receipt cannot skip checking a
mutable media file or cancellation. Valid narration and rendered scenes are
reused without repeating paid generation.

Assembly writes `final.pending.mp4`. Verification checks nonempty bytes, final
resolution, frame rate, complete timeline duration and required audio, and binds
that probe to a SHA-256 receipt. Publication rechecks the bytes and cancellation
before atomically replacing `final.mp4`; only afterward can the job owner report
delivery. Failures before publication preserve the old film. Cancelled jobs
persist `aborted`, so startup recovery does not restart them as unfinished work.
The silent baseline stays available for later subtitle revisions. This protects
the final MP4 publication boundary; companion files are not a multi-file atomic
transaction. Scene-edit operations retain their existing revision contracts.

Retention: one overwritten `film_state.json` contains eleven current stage
receipts, with no per-attempt history or new task store. It is retained with the
job. The one-scene/eight-second offline fixture measured 20,768 bytes for this
file (including path references and quality data); this is metadata evidence,
not a real-video size or throughput estimate. At most one pending final MP4
exists and is removed on failure/cancellation;
it becomes the final file on success. Deliberately silent jobs now retain their
silent baseline instead of moving it away: the extra durable media bytes equal
that baseline's size. Narrated jobs already retained it.
An installed-FFmpeg smoke test encoded an eight-second 320×180 color fixture:
the silent baseline was 8,302 bytes and the narrated final was 17,564 bytes.
This checks real codec/concat/mux/probe behavior, not browser capture, visual
quality or representative video compression.

**Alternatives considered:** a second video executor would duplicate lifecycle
and cancellation; wrapping the whole engine in one opaque node would still hide
the delivery boundaries; trusting completed stage receipts would skip mutable
artifact checks; checking after replacing the published file cannot preserve the
previous deliverable on failure. Video-specific validation belongs in nodes,
not in a separate agent execution mechanism.

Verification: `tests/motion_video/test_motion_video_workflow.py` exercises the
real worker and stage executor with codec/TTS seams, including invalid final
media, lost audio, missing output, checkpoint-write failure, cancellation,
post-verification byte changes, and reuse of validated narration/scene renders.

Verification: `tests/production/test_agent_tool_workflows.py`, checkpoint
revalidation and consumer recipe suites. The `agent_tool_continuation` transport
replay drives real Chat, tool dispatch, producer admission, a later file-write
round and terminal settlement; expensive rendering/worker launch are isolated.


## Execution model

1. The capability validates the request and derives a stable deduplication key.
2. Capabilities with a mandatory local render chain prove it before task creation. Slides import Playwright, prepare native Chromium paths, and smoke launch Chromium once per worker process; a wrong `.tofu_env.json` authority therefore fails at admission instead of after research and page authoring.
3. `ProductionRuntime` atomically joins existing live work or creates one task.
4. The capability writes a minimal owner-scoped job manifest before spawning.
5. Its recipe invokes `run_stages`, or `run_independent_stages` for true siblings, with explicit names, gates, retry limits, checkpoint versions, freshness windows, and owned demand workers.
6. Each successful stage atomically commits one JSON-serializable checkpoint.
7. Capability code publishes a final artifact only after its final quality gate passes. Publication is a terminal resumable stage: an unconfirmed write blocks success, while its bounded retries never repeat upstream model work. The capability then settles the generic task exactly once.
8. Startup rescans manifests still marked running and reconstructs their tasks; the stage graph skips only valid complete checkpoints.

Heavy media is referenced by path/artifact identity, never embedded in stage JSON, task events, or logs. A process exit code alone is not a quality verdict.

Long-form report starts atomically join identical live work. Their depth profile fixes 3/5/8 unique headings; a start naming any other `depth` (or a non-string `lang`) is refused as a field-named `BadRequest` before a task exists, as are the slide starter's unsupported `size`, over-long `topic`/`style`/`model` and unknown `creative_mode`. Research checkpoints expire after six hours; section checkpoints bind every prompt input, and assembly checkpoints bind every Markdown input. One section batch constructs its immutable report/evidence/instruction prefix once and appends only the heading-specific task; the prompt-layout revision invalidates older section checkpoints. This preserves independent quality gates and resume while exposing the full research packet as a provider-cache prefix instead of diverging before it. A conversation-scoped report settles only after its artifact ID is confirmed. Independent judge/section calls use the launch-probed 1..2 personal per-job fan-out (distributed 4, hard ceiling 8); task admission bounds the process-wide multiplier. Section calls overlap without increasing their logical count, so N sequential model waits become a work-conserving bounded batch. Reconstructible enrichment uses strict billing-stop admission, a launch-probed finite 429 allowance, and immediate yield to known shared contention; durable background production instead caps actual upstream 429s at 4..8 on personal machines (distributed 16, hard ceiling 64) with two hard-error slot attempts, while interactive chat retains its separate retry policy. Slide outlines, page authors, and QA repairs use it. Independent pages overlap behind the 1..2 personal LLM fan-out and checkpoint immediately by exact prompt/model/round policy plus bounded YAML hash and zero-LLM validation; only authored pages are reusable, so a restart or one corrupt/changed page does not repay for the rest of the deck and a transient fallback gets another chance.
Slide publication is strict at semantic boundaries: every page must pass the author contract after one missing-page retry, every page must render a preview, and deterministic layout QA must clear all remaining overflow/collision findings. Fallback YAML remains only as a valid retry diagnostic; it cannot produce a successful PPTX. Visible text rejects internal planning phrases and research IDs, while chart values absent from the page evidence are returned to the author instead of shipping invented benchmark scores. Generated-asset or optional VLM outages remain explicit `artifact_quality` degradation evidence. Page-author prompts place the immutable deck/theme/text-style/grounding/design-bible/PPTD contract before page number, brief, assets, and sources. Each page remains independently hashed, cached, repaired, and checkpointed; an input version change invalidates pages authored under an older prompt layout. One frozen batch context reads and formats the theme, bible, and cheatsheet once; cache preflight and owned page workers share it without retaining full prompts.

Slides accept an optional bounded `material` input (≤ 20000 chars) alongside the short `topic`. Problem: a deck that must reflect the user's own work (an interview/work retrospective, a project report) could only be driven by a ≤4000-char `topic` plus public web research, so a full curated evidence brief was rejected by `normalise_slide_topic` and the pipeline would have invented or web-substituted the numbers. Decision: `normalise_slide_material` bounds the new input, `start_slides_job` persists it in the manifest and includes it in the dedup key, `build_deck_from_topic` carries it into the recipe context, the research stage attaches it as `supplied_material`, and `_build_outline_prompt` injects it as an AUTHORITATIVE block before the audience/evidence guidance, instructing the author to reproduce its facts, figures, dates and attributions faithfully, never to replace them with web-card or remembered values, and to keep team-result attribution. Omitting `material` leaves the previous topic-only behavior byte-identical. Alternatives rejected: raising the `topic` limit (would change its short-title semantics and dedup identity for every existing deck) and letting callers paste a long brief into `topic` (rejected by the existing bound). Verify with `tests/slides/test_slides_material_input.py`.

Slides and topic-driven motion video accept `creative_mode=director|standard`. `standard` is the one-plan A/B control. `director` sequentially drafts two contrasting plans, screens each through the existing deterministic fact/current-state gate, then spends at most one bounded critic call to select the stronger valid candidate. The selected plan records candidate count, winning lens, reason, fallback scores, critic scores, and usage; failed critics fall back to a deterministic richness/diversity score. Creative mode is part of dedup and outline/script checkpoint identity, so switching an experiment arm cannot reuse the other arm's plan. Direct legacy/internal calls that omit the field retain one-plan behavior, while public production defaults to `director`.

A background slide task mints one bounded model-routing-v2 group from the explicit task owner and carries its provider pin into every worker thread. Outline, page authoring, image generation, edit, and visual QA therefore use the initiating user's authorized paid or local routes instead of process-global credentials. The route group is disposed when the stage graph settles. A candidate that cannot initialize is retained as bounded degradation evidence and skipped; only the runnable authorized candidates enter dispatch fallback. The high-level producer tool returns one accepted-task receipt. Once accepted, root chat cleanly ends that model turn; the generic task stream/panel owns all subsequent progress and terminal quality. The model does not poll the task in a second loop or infer completion from intermediate files.

PPTD pages may additionally declare semantic `metric`, `quote`, `comparison`, `timeline`, `process`, and `code` components. Parsing expands them once into the existing editable text/shape/line primitives before validation, preview, repair, and PPTX export; no second renderer model or flattened page is created. Native category charts support bar, column, line, pie, area, doughnut, and radar in both SVG preview and OOXML export. Deck plans carry an explicit visual modality, visual anchor, and next-page handoff so independent page authors receive global continuity rather than layout-only hints.

Public production PPTX export is portable-font strict. Every effective family and style slot is collected from actual text/table runs and native chart labels, common audited CJK aliases are canonicalized to the embedded face's real family name, and the artifact records used, embedded, and missing families. Any missing family fails the export gate instead of silently relying on Office substitution. Two-point arrows are native connectors with exact endpoints; semantic process chevrons carry an explicit adjustment shared by browser preview and OOXML. For decks of at least four pages, the outline gate also requires at least four layout archetypes and four visual modalities before page authoring begins.

Shared production research retains 12 rows per query lane and reuses discovery titles; survey/harvest use one owner-scoped arXiv-indexed projection, metadata-only for harvest; survey uses one body plus one batched report query, capped at 6,000 chars without auxiliary JSON. Survey gates cap 12 clusters/40 matrix rows/20 gaps/40 ids per entry and ground 20 unknown ids in one request with visible truncation. Standalone research keeps 3..12 ideas, 20 seeds and 2,000-char directions with exact manifest identity. Task affinity/memo caps prior probes at 20; a serial rubric warms its prefix before launch-probed 1..2 bounded judge workers.
Research Foundry actions are ordinary `research-action` production tasks but are deliberately not crash-replayed after an external write may have started: an unknown third-party MCP tool cannot be assumed idempotent. The owner must inspect its external state and explicitly start a new action. Workspace CAS then prevents the result of a long action from overwriting edits made while it ran. Tool output excerpts/digests and provider artifact references settle into the versioned program; task streams remain bounded progress projections.
Slide image generation submits every independent image on demand (the fixed `TOFU_PRODUCTION_IMAGE_FANOUT` hint of 4 only keeps idle workers warm), with a fixed upstream-429 allowance of 8 personal / 16 distributed (hard ceiling 64) and two total hard-error attempts. A deck admits at most six generated images and 20 caller images; individual images are capped at 20 MiB and caller/remote channels at 192 MiB. Downloads and local copies stream to atomic files. Generated and remote caches bind semantic input, path, byte size, and SHA-256; URL-derived remote filenames prevent restart collisions, and stale reconstructible files are reclaimed. Independent page VLM reviews also start on demand; exact prompt/model/token/pixel digests reuse only validated structured findings, while a changed page invalidates that page plus the deck contact sheet. Visual QA rejects previews above 16 MiB before base64 allocation. Preview rendering bounds geometry, pages, and PNG bytes before Chromium allocation/publication, checks abort between pages, and waits on actual fonts/images/layout instead of adding a fixed 400 ms sleep to every page.

Podcast and motion TTS use their configured worker values for idle reuse only; eligible segment synthesis starts on demand. Audio validation and provider errors remain distinct from worker admission. Motion narration persists a versioned text/timing/settings/file-hash manifest immediately after synthesis, so only an exact bounded checkpoint can suppress paid TTS on resume. Motion scene-author prompts put the shared frame, hard requirements, composition contract, craft guide, and skeleton before scene identity, duration, narration, assets, and frame packet. Each scene retains its own bounded agent loop, quality gate, draft recovery, and template degradation; prompt prefix reuse never makes one scene depend on another's output. One frozen, lazily prepared film context reads and formats that prefix once for initial authors and visual-QA repairs; a fully resumed film does not prepare it. Missing scene authors start on demand, including when optional dependency prewarm fails. Font and craft first-install writes serialize by physical destination in their source owners; unrelated scene work has no worker window. Static gates and no-regression adoption remain on the caller. Abort or fatal commit failure stops unpublished work and drains owned authors; active drafts remain recoverable. The selected motion blueprint already travels in each frame packet. The deeper optional craft catalog is installed and indexed only for scenes that explicitly set `allow_craft_browse`; ordinary production scenes perform neither operation. Storyboards also preserve bounded renderer-neutral `media_queries` and a visual modality. The deterministic film plan records ordered renderer candidates (HyperFrames is the installed/resumable adapter; Remotion, Motion Canvas, and Manim are optional future lanes) and injects the request into the scene frame packet. At most one stock request per scene is materialised into lifecycle-bound scene storage. Pexels photo/video retrieval is optional behind `PEXELS_API_KEY`, uses only trusted HTTPS Pexels media hosts, accepts at most 16 MiB per image or 32 MiB per video, and records provider/creator/page links in `media_attribution.json` plus a user-downloadable text ledger, and the silent source card visibly adds `Media: Pexels.com`. Provider absence, web-capture absence, bad MIME, oversize data, or outage is an explicit quality finding rather than an invented path. No credential enters a task, checkpoint, log, or attribution record.
Motion visual QA stores at most one successful result per scene behind a 64 KiB file cap. Exact contact-sheet pixels, prompt/theme, model, and dispatch settings must match before a resume skips its VLM call; changed pixels miss, outages are not cached, and cached major/blocker findings still enter the author repair. The screenshot remains required evidence and is not bypassed by this cache.

### Measured visual repair and native export

**Problem:** animation-relative screenshots can inspect the wrong video time;
an unverified slide repair can replace a valid page, and a partially exported
PPTX can appear successful while missing required objects.
**Decision:** motion QA samples absolute seconds of the declared render
duration with callbacks enabled, including finite-repeat animations and local
video footage (clip start/source offset respected). Missing timelines, failed
seeks, runtime errors and unavailable assets invalidate the evidence. Capture
uses one browser, at most 12 timepoints/eight videos, 9 million source/sheet
pixels, 16 MiB per screenshot and 1 MiB of composition HTML. Abort checks run
between samples; contact-sheet replacement is atomic.

Slide visual repairs seed from the current YAML. Candidates are parsed by
the same PPTD page parser, measured only on the affected page, rendered into
private scratch and reviewed again. A strict improvement in blocker/major
counts is required. Changed decks receive a fresh coherence review; an outage
or increased whole-deck severity rolls the candidate batch back. Known
major/blocker findings block the visual stage. Optional review outages remain
explicit quality degradation. One versioned, digest-fenced recovery journal
restores original YAML/preview pairs before a restarted review. Backups are
capped at 192 MiB per job, 512 KiB per page and 16 MiB per preview; verified
completion, rollback and recovery reclaim only the transaction's scratch.

Every native PPTX export, including revisions, writes a private candidate,
rejects missing/failed objects, then checks ZIP/CRC, bounded safe XML, content
types, relationship targets, ordered slide/shape identity, native chart axes
and editable-object counts before atomically replacing the deliverable.
Package QA admits at most 256 MiB packed/512 MiB unpacked, 10,000 parts,
8 MiB per XML part and 32 MiB aggregate XML. It fetches no external links.
Native-package checks do not constitute PowerPoint/WPS rendering evidence.
On a 20-page/200-text-box synthetic deck, native export took 0.466 s and
package QA's median over five runs was 21.56 ms (52,692-byte package). These
changes add no resident worker or unbounded cache.

**Alternatives considered:** animation percentages were rejected because
timeline length differs from render duration; accepting a repair on successful
render alone was rejected because it proves neither improved layout nor
appearance; a browser-only or CRC-only export check misses native OOXML
relationship/chart defects. Office rendering remains separate validation.

### Author guidance and numeric fidelity

**Problem:** slicing general and scenario design guidance after concatenation
erased the entire scenario from bounded author/edit prompts. Chart validation
also treated small measurements as structural labels and lost numeric signs.
**Decision:** `design_bible_text` reserves part of the existing character budget
for each source, redistributes unused space, and marks clipped excerpts.
Chart series values always require matching page-brief/source numbers, including
zero, small integers, negatives, grouped thousands and scientific notation.
Author checkpoints invalidate their suffix when this evidence policy changes;
cached pages still pass current validation before reuse.
**Alternatives considered:** increasing every prompt would hide starvation and
grow all page calls; exempting page-count-sized values would still admit invented
experimental or career results. Numeric membership is a fidelity check, not proof
that the source establishes the claim, its unit, or its causal interpretation.
Verify `test_bible_prompt_budget.py` and `test_slide_evidence.py` alongside the
design-system and slide author/cache suites. No new retained files are added.

### Evidence-aware slide review and audience guidance

**Problem:** pixel-only review lacks the source needed to detect changed units,
overstated research conclusions or misattributed work. Subject keywords can also
misroute an engineering interview into a general technology presentation.
**Decision:** the existing outline/author/visual-review graph carries bounded
scenario guidance. `career-interview` owns two themes and a contribution-first
guide; academic guidance prioritizes source figures, experimental conditions,
uncertainty and limitations. Explicit interview/work-organization intent wins
the deterministic scenario hint; the outline model still selects the final
scenario. Supplied template requirements outrank aesthetic defaults.

Research/interview body-page evidence imagery must be supplied; absent originals
use native explanatory diagrams rather than generated research/work artifacts.
The shared reviewer receives a transient projection from
`lib/slides/review_context.py`: effective owner-approved plan, user request and
page-local cited excerpts. Plan text is labeled as intent, not corroboration.
Deck contact-sheet review receives the story sequence. Original and repaired
pages use identical evidence context. The shared QA prompt, input digest and
dispatch use the same 12,000-character evidence ceiling, with explicit truncation.
Changed evidence invalidates cached reviews even when the pixels are identical;
outline/visual checkpoint versions invalidate dependent stage suffixes.
Known major/blocker findings still block publication, and unavailable reviewers
retain the existing explicit degraded-quality behavior.

**Alternatives considered:** a separate content-review agent/stage would duplicate
invocation, recovery and repair ownership; a screenshot-only critic cannot compare
sources; source-number matching alone cannot verify units, causality or personal
attribution. This is a model-assisted consistency review of supplied excerpts,
not verification of unseen original papers or PowerPoint/WPS rendering.
Evidence context adds no model calls or persisted files/fields: existing review
cache entries retain the same 64-hex input digest and bounded findings. Measure
prompt size separately from product quality; these tests do not establish a
blind preference win. Verify audience, evidence review/cache and repair suites
alongside shared visual-QA, design-system and production workflow contracts.

### Immutable font downloads

**Problem:** floating Fontsource `@latest` URLs drifted from audited hashes;
the academic `swiss-lab` theme consequently failed strict Noto Serif SC
embedding during a real export. **Decision:** pin the six Fontsource CDN
assets to exact releases (Noto 5.3.1, Liter/Oranienbaum 5.3.0), after verifying
WOFF2 parsing, weight, embedding permissions, CJK coverage where applicable,
byte size and SHA-256. Refresh the two changed Noto Serif pins; preserve hash
rejection and mandatory embedding. Fontsource's
[CDN contract](https://fontsource.org/docs/getting-started/cdn) recommends
immutable full-semver URLs.
**Alternatives considered:** accepting changed bytes silently weakens asset
integrity; substituting fonts hides a portable-export failure. Registry tests
reject floating Fontsource URLs, and the existing hash-rejection and strict
export tests remain required.

Measured source bytes on 2026-10-27: all six assets total 5,420,996 bytes;
the replaced Noto Serif pair totals 3,068,008 bytes, 3,200 more than before.
Existing content-addressed storage can retain both revisions; a populated
store gains at most that new pair's source bytes, excluding alias copies and
deck-local/embedded derivatives. This changes no retention owner or policy,
adds no resident worker, and never reclaims published deck assets.

### Chart geometry and label portability

**Problem:** horizontal previews reused chart width for category-row spacing,
clipping later values on wide plots. Chart labels also fell back to the host
font, and native chart-only decks escaped the used-font embedding check.
**Decision:** horizontal rows use the plot height and respect legend space;
legend placement accounts for full-width glyphs and anchors labels inside
the right edge instead of clipping Chinese units.
`pptd.chart_font_family` selects the canonical body face for SVG labels,
native chart text defaults (Latin, East Asian and complex scripts), and font
subsetting. Source category/series labels and the numeric alphabet needed by
Office-generated ticks enter the existing regular-font subset; missing fonts
still fail strict export. The existing chart data and native editability remain.
**Alternatives considered:** changing the sample to a column chart hides the
renderer defect; relying on installed CJK fonts leaves machine-dependent output.
Verify wide/tall and multi-series geometry, canonical label fonts, native XML,
chart-only missing-font rejection, and real browser samples. Package checks do
not replace Office rendering. No cache, worker or persisted metadata is added;
embedded glyphs follow existing chart text plus the fixed tick alphabet.
An inspected synthetic academic page with one chart and five text boxes embeds
120 glyphs in 27,504 font bytes (55,001-byte PPTX); the eight-text-box interview
sample embeds 124 glyphs in 28,968 font bytes (50,097-byte PPTX). Both pass real
Chromium layout/label containment and native-package checks. These samples do
not establish native Office rendering or model-generated deck quality.

## Stage contract

A `Stage` declares:

- a stable semantic name;
- `run(context) -> artifact`;
- an optional deterministic `gate(context, artifact) -> errors`;
- a bounded retry count;
- whether it is resumable;
- an optional freshness TTL and semantic checkpoint version.

When a stage is absent, stale, version-mismatched, corrupt, or fails its gate, that stage and its dependent suffix are recomputed. Upstream checkpoints that remain valid are preserved. Cancellation is checked between stages and inside long-running capability operations.

`run_independent_stages` is reserved for siblings that read frozen upstream
inputs and never one another. Every eligible sibling starts on demand, and each
passing result commits on the caller thread. The legacy `max_workers` argument
sizes idle reuse only. Failure/abort stops further submission, cancels unstarted
work and drains running siblings; passing checkpoints remain reusable. A failed
checkpoint cannot reappear in a later sibling commit. Research queries use the
same owned executor. User authorization budgets, quality gates and genuine
thread-start failures retain their classified errors.

Problem: checkpoint version/TTL alone accepted deleted or invalid output files,
and a final-stage Stop could still publish success. Decision: run the same
deterministic gate before reusing each serial or independent checkpoint, and
observe cancellation after production and validation. A known gate rejection
invalidates that stage and its dependents. A validator exception preserves the
checkpoint and reports unavailable verification without replaying its producer.
Alternatives: trusting cached success loses artifact evidence; treating an I/O
failure as invalid content can duplicate expensive or external work. This adds
no checkpoint fields or durable rows. Fault specifications:
`tests/production/test_checkpoint_revalidation.py`.

Problem: a worker-count window queued independent sections and made progress
depend on unrelated siblings. Decision: reuse the shared demand executor,
with live pool snapshots aggregated until physical retirement and no per-job
metric labels. Alternatives: larger windows still queue, detached workers lose
checkpoint/cleanup ownership, and ignoring errors loses recovery evidence.
The standalone 64-stage rendezvous completed 64 caller-thread checkpoints in
51.97 ms with idle hint 1 and no retained threads. This measures acceptance and
cleanup, not provider throughput or whole-process RSS. Concurrent-pool and
fault-injection tests also verify observation retirement and recovery.

## Capability boundary

The substrate contains a primitive only when multiple independent recipes need the same semantics. It does not own:

- video timelines, scene authoring, rendering, subtitles, or muxing;
- slide layout or deck rendering;
- podcast script, TTS, or audio joining rules;
- long-form outline, prose, or document export rules;
- capability-specific binary formats or user-facing artifact projections.

Sharing a filename or similar code shape is not enough to move behavior into the substrate. The shared contract must have the same lifecycle, failure, and recovery semantics.

## User experience and failure semantics

- An explicit `workUnitRef` renders a visible deliverable card outside settled
  chat Process and model-round disclosures. Its production steps and original
  request remain inspectable inside the card. Slides and video share the same
  TypeScript owner, `frontend/src/conversation/ui/work-unit/`; the retained
  `ui/workunit_tracker.js` supplies application ports only.
- A pending outline/storyboard review is the first actionable content. Polling
  preserves that guidance identity's draft, cursor and focus; duplicate cards
  share a single submission. An accepted response/stop request never settles
  the task locally. Read errors retain the last confirmed state with retry.
- Preview selection addresses the exact page/scene; AI change requests append
  to the existing composer draft without sending. Published video playback and
  scene reads start on explicit intent. Re-render acceptance exposes its child
  task, with parent refresh only after authoritative child settlement.
- Repeating an identical request joins visible in-flight work instead of silently spending twice.
- Progress is expressed as stable task events and semantic stages, not a fabricated timer.
- Abort prevents later callbacks from publishing or changing terminal state.
- A gate failure reports the stage and actionable findings; partial output is retained for diagnosis/resume but never presented as the finished product.
- A restart resumes from durable manifests/checkpoints when the capability declares recovery; it never claims process-memory work survived.
- A finished artifact stays revisable after a restart: scene revision (regen/edit) and page preview/file serving resolve their parent job from the same durable manifest anchor when the live runtime no longer holds it.
- Conversational edits are bounded revisions, not silent regeneration: an instruction-driven rewrite seeds from the current artifact, a deterministic no-regression commit keeps the better of old/new, and an unchanged result finishes honestly (`applied: false`) instead of burning a re-render.
- Deterministic revision owns field-level changes: `patch_slides` / `restyle_slides` / `set_narration` run zero-LLM ops with validation + rollback, the LLM rewrite stays the fallback for structural changes, every edit step snapshots for `revert_*`, and chat-initiated decks pause once at an interactive outline gate (`pendingGate` in the workUnit projection) answered through the shared human-response endpoint. The in-chat editor overlay (`openSlidesEditor`) and the chat tool call the SAME patch engine — three owner-scoped endpoints (`pages/<n>.html` / `pages/<n>/elements` / `pages/<n>/patch`) are its HTTP shape.
- Motion revision adds three deterministic ops beyond scene rewrite: `set_narration` (TTS must fit the frozen frame), `set_subtitle_style` (job-level, persisted to `subtitle_style.json` so every later re-burn honors it), and `swap_asset` (stock / generated / text-to-video clip — a stock miss for a video-kind query tries ONE bounded t2v clip before becoming a finding, configured via `TOFU_T2V_*`). Chat-initiated films pause once at a storyboard gate whose decision persists (`storyboard_gate.json`), so a crash-respawned job never re-asks.
- Missing artifact bytes, malformed manifests, and corrupt checkpoints are explicit failures, not empty success responses.

Problem: a background producer was presented as a finished, collapsed tool
receipt, obscuring both human decisions and the evolving artifact. Decision:
keep one durable acceptance card and project the existing WorkUnit into it;
place partial/failed quality evidence next to previews and retain the common
task/human-response/edit endpoints. Alternatives rejected: exposing the raw
workflow as the primary UI; adding a separate execution or frontend lifecycle;
opening a separate app for every producer.

Mounted resource ownership: duplicate cards coalesce reads by task and owner;
only live mounted work polls (adaptive 2–5 seconds), and a hidden tab pauses
polling. Terminal work has no poll timer. One demand-scoped observer ignores
ordinary token/progress mutations and retires with the final mounted card;
videos release their source when removed. Focused tests measure three mounted
copies as one read, no additional reads over ten simulated terminal seconds,
and zero retained task/view/request/timer/observer owners after removal.
This measures UI residency and request counts, not provider throughput or RSS.

## Invariants

- One shared stage implementation and one long-job lifecycle implementation.
- Dedup claim/create/register is atomic with respect to competing submissions.
- Dedup retention follows the resolved task-registry target unless a recipe lowers it; it cannot re-widen the shared policy, while active keys may exceed the target and are never evicted merely to satisfy it.
- Checkpoints are atomic, complete, JSON-serializable semantic boundaries.
- A changed upstream contract invalidates its dependent checkpoint suffix.
- Generic task routes discover runtimes by their declared `kind`; route code does not maintain a parallel hard-coded lifecycle.
- Runtime discovery does not initialize recipe, render, research, or recovery modules.
- Owner identity and artifact provenance survive every handoff.
- Recipe-specific quality rules do not leak into the horizontal substrate.

## Change routing

| Change | Start here | Verify |
|---|---|---|
| Stage/recovery semantics | `lib/production/stages.py` | resume, suffix invalidation, gate, abort |
| Dedup/retention/task lifecycle | `lib/production/runtime.py` | concurrent claim, terminal pruning, restored identity and owner checks |
| Restart behavior | `lib/production/jobs.py` | corrupt manifest, idempotent rescan, respawn failure |
| Shared content contracts | `contracts.py`, `research.py` | every consuming recipe and finite validation |
| One media recipe | its capability package | quality gate, artifact publication, task settlement |
| Generic task projection | `routes/api_v1/tasks.py` | discovery, events, stream, abort |
| Deliverable card and revision intents | `frontend/src/conversation/ui/work-unit/` | draft retention, authoritative settlement, owner fencing, mounted teardown, browser layouts |

## Test map

```bash
pytest -q tests/production/test_production_substrate.py tests/production/test_production_runtime.py tests/server/test_task_registry_discovery.py
pytest -q tests/app_factory/test_production_lifecycle.py tests/startup/test_production_startup_boundary.py
pytest -q tests/motion_video/test_motion_video_p4.py tests/motion_video/test_motion_video_p5.py tests/paper/test_paper_podcast_api.py tests/longform/test_longform_p7.py
pytest -q tests/motion_video/test_motion_video_edit.py tests/production/test_work_unit_projection.py
node --test tests/work-unit.test.cjs tests/motion-card.test.cjs tests/slides-editor.test.cjs
pytest -q tests/visual/test_visual_work_unit_cards.py tests/visual/test_visual_multimedia_receipts.py
pytest -q tests/slides/test_slides.py tests/slides/test_slides_creative_foundation.py tests/slides/test_slides_research_foundation.py tests/visual/test_visual_qa.py tests/slides/test_slides_semantic_components.py
pytest -q tests/slides/test_slides_patch.py tests/slides/test_slides_theme_ops.py tests/slides/test_slides_batch_edit.py tests/slides/test_slides_versions.py tests/slides/test_slides_templates.py tests/slides/test_slides_outline_gate.py tests/slides/test_slides_editor_api.py
pytest -q tests/production/test_production_creative_director.py
pytest -q tests/slides/test_slides_visual_repair.py tests/slides/test_visual_repair_recovery.py tests/slides/test_pptx_package_qa.py tests/motion_video/test_temporal_qa_bounds.py
pytest -q tests/visual/test_motion_temporal_capture.py
```

Stage runners accept caller-declared `abort_exceptions` and propagate them
unchanged. Research supplies the shared `AbortedError`, including model budget
admission denial; the capability-neutral substrate imports no model code. Problem: generic stage retries caught a policy stop
and retried it, finally replacing its typed cause with `StageFailed`. Decision:
stop serial retries and queued sibling admission on a typed abort, retaining
completed checkpoints and joining already admitted siblings. Alternative
rejected: treating policy refusal as a transient provider error. Ordinary
exceptions and quality-gate failures keep their bounded retry policy. Verify
`tests/production/test_stage_budget_abort.py` and the substrate suite.

Restored job claims use the same atomic identity lock as ordinary starts. A live
job with that identity joins only for its exact owner; unrelated live registry
counts do not refuse recovery. Worker-start failures and durable publication
failures remain actual task errors.


Media worker admission — Problem: fixed page/scene/TTS/render windows queued independent work, and failed dependency prewarm serialized entire scenes. Decision: `concurrency.owned_results` submits on demand, yields ready outcomes for caller-owned adoption, cancels unstarted work after an observed stop, and joins published work even when the consumer raises. Asset failures remain findings; quality, rollback, owner routing and byte validation remain. Alternatives: larger windows retain waiting; detached results lose cleanup; parallel shared-file writes can expose partial fonts/craft. Destination-owned install locks preserve those writes without a task-count gate. Measurement: 64 jobs with idle hint 1 settled in 17.83 ms with zero retained threads. Four real Chromium jobs overlapped with idle hint 1, verified PNG pixels and closed all live owned processes in 1194.63 ms. Their sampled summed process RSS was 1,841,147,904 bytes (shared pages counted in each process); this is acceptance/cleanup evidence, not a throughput or memory-bound claim.

Research/paper worker admission — Problem: local judge/chunk/segment windows and serial cache warm-up delayed independent work. Decision: evaluation, idea scoring, terminology chunks and podcast TTS use joined demand delivery; each job receives its own copied caller context so budgets, affinity and attribution remain shared without child context mutations leaking back. Results retain input order and authorized model reservations remain atomic. The obsolete `TOFU_PRODUCTION_LLM_FANOUT` knob and generated defaults are removed. Alternatives: larger quotas retain waiting; a shared mutable Context cannot run concurrently; dropping authorization ports permits unauthorized provider sends. Shared-budget, context-isolation, rendezvous and failure tests guard these boundaries.

Podcast rendezvous measurement: eight actual PCM synthesis jobs with idle hint 1 completed with ordered measured segment boundaries and zero retained workers; this verifies delivery/timing ownership rather than provider throughput.
