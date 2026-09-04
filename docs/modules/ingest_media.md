# Ingest, papers, translation, and media
This domain turns uploaded or remote source material into validated text,
durable paper/library state, derived reports, translations, audio, images, and
video artifacts. It owns content-specific pipelines; generic task execution,
storage, and model transport remain separate domains. The enabled `generate_image` schema stays within 400 tokens while preserving incremental source editing, exact saved-reference reuse, project/server and multi-root routing, aspect/resolution choices, and optional sibling SVG tracing.

## Ownership
| Concern | Owner |
|---|---|
| PDF extraction and page policy | `lib/pdf_parser/` |
| File-to-text routing | `lib/file_reader/`, parser packages |
| Paper library and ingestion | `lib/paper/library.py`, `harvest.py`, paper routes |
| Paper reports/review/insight/QA | focused modules under `lib/paper/`; typed browser owners compose once through `frontend/src/features/paper/panel-owners.ts`; retained report/reader presentation lives only in manifest bundle `paper-reader-presenters` |
| Paper podcast and browser media presentation | `lib/paper/podcast_engine/`, `podcast_runtime.py`; the same typed-owner composition boundary; retained podcast/video presentation lives only in manifest bundle `paper-media-presenters` |
| Paper translation | `lib/paper/translate_engine.py`, `translate_runtime.py` |
| General translation | `lib/translate/` |
| Transcription/audio | `lib/transcription/`, audio routes |
| Image generation/editing; shared MIME sniff/fullscreen/download UI | `lib/image_mime.py`; focused image services and tools; `frontend/src/image-viewer-actions.ts` |
| Chat document/video attachments | `lib/media_attachments.py`, `lib/knowledge/`, `routes/api_v1/media.py` |
| Chat video analysis | `lib/video_analysis/`, `routes/api_v1/videos.py` |
| Motion video | `lib/motion_video/`, motion routes/tools |
| Durable artifacts | artifact/storage services, Sidecar domain operations |

## Ingest flow

1. Validate type, size, and source authority before expensive work.
2. Stream or stage bytes through the upload boundary; never trust a filename as a storage path.
3. Select one parser through the file router.
4. Preserve page/section provenance and isolate page-level parser failures.
5. Canonicalize extracted text and metadata.
6. Commit the source row and required derived metadata atomically.
7. Start optional report, translation, podcast, or video work through a declared
   task runtime.

Remote fetches cross the shared URL/egress safety boundary. Parser fallback is
explicit and diagnosed; it must not silently return an empty successful
document.

## Chat attachment state

The owner-scoped Knowledge repository is the single persistence authority for
chat documents/videos: one original, parsed chunks, and derived visuals. The
chat layer has no parallel PDF-text store, video directory, or frame registry.

Scopes are internal `draft`, `attachment` (explicit ref), `library` (global retrieval), and `shared` (both, one source).

Digest reuse changes scope instead of copying bytes. Removing one side of a
`shared` document demotes it to the other scope; source and derived assets are
deleted together only when neither surface retains it.

New Turns carry at most 20 server-resolved refs in `projection.attachments` and at most 20 image refs in `projection.images`, never text, transcripts, frames, originals, or base64. Missing/foreign IDs are rejected. Frozen historical inline images are not rewritten: only the conversation `refs` view replaces eligible duplicate bytes with an authenticated, owner/revision/index-fenced lazy URL; `full`, persistence, and model reconstruction remain unchanged. Native MCP image results are owner-scoped `attachment` documents: each image is capped at 8 MiB, each Turn at 40 MiB, and persistence failure does not fail the tool's text result. Continue/checkpoint-resume preserves existing image refs; regenerate owns a fresh list.
At model-request time, relevant text shares a 96k-character request budget while bounded document visuals/model-aware video frames become request-local blocks. `att_media_<document-id>` and `/api/v1/media/attachments/<id>/source` reopen owner-authorized content without exposing its disk path.

Documents commit before `ready`. Videos reserve launch-probed worker capacity,
TTL-reclaim scratch, and commit the original before analysis; turns may reference `processing` video. Frames/storyboard/transcript replace atomically; durable status survives worker registry expiry/restart. Transcript and storyboard calls each mint a bounded request-only model-routing v2 group for the attachment owner, hard-pin provider-only dispatch to that group, and dispose it after the background stage; they never borrow a process-global credential.
Historical `pdfTexts`, `videos`, and `/api/videos/<filename>` are read-only compatibility. Classic local PDF extraction shares one process-wide admission budget across public text, direct full-document, and pooled entry points: the 8 GiB reference is 1 child/3 unfinished inputs/512 pages/4 MiB text, distributed mode is 4/16/2,048/16 MiB, and retained images stop at 64/2,048 px. Bounded-prefix results expose limits and truncation; a timed-out running child retains its slot until true settlement and is never duplicated in-process, then personal/distributed idle children retire after 60/600 seconds. Research harvest enforces that parser's 200 MiB PDF ceiling before/while streaming, rolls beyond 1 MiB into lifecycle-bound temporary storage, and never retries deterministic oversize input. Local Knowledge holds that same lease across validation, text, OCR, visual/source persistence, and repository commit; its default 80/80 pages, 160 assets/160 MiB and 50 MiB input cap bound the reference aggregate to 150 MiB compressed sources plus 480 MiB accepted visual candidates, and OCR stops at the text ceiling. VLM PDF transcription uses one launch-probed owner-fair lane with finite source-PDF retention, a lower page-call ceiling, a pre-render page limit, a task deadline, finite 429 attempts, bounded terminal results, and owner-scoped cooperative cancellation; it remains reconstructible attachment work rather than durable document state. Optional knowledge-asset vision descriptions likewise use one launch-probed process lane: personal mode runs 1..2 paid calls, distributed mode runs 8, retained owner IDs are finite, each turn claims only one durable asset, and workers retire after an idle window. Before claiming an asset, each worker verifies the owner's runnable v2 vision routes; its paid description call uses a bounded request-only group under a hard pin and disposes that group in `finally`. This keeps image bytes in the repository instead of a memory queue and prevents one large corpus from starving another owner.

## Paper state

The paper library and derived records are owner-scoped Sidecar state. Normal browser
shelf reads use one fail-open file snapshot, hydrate one owner/id detail, and retain two.
Reader detail omits legacy Babel; metadata/QA writes serialize; hashes stay canonical.
First content retries until ack; typed Report/Review state caps each language and validated reading-position map at 2,048 entries, retains 12 report snapshots, generation-fences same-paper starts, and hands provisional Stop intent to the returned task ID. At most 32 rebuttal drafts of 40,000 characters survive locally. Known papers start by hash: live/cache hits read no body, a true miss projects at most 120,000 owned characters, and only an explicit pre-dispatch source miss permits one bounded browser fallback. Reopen resolves live work plus preferred/fallback cache in one HTTP request; one owner-scoped Sidecar aggregate selects the base and returns only that language's Insight/termfill/checkpoint rows (four queries to one, at most eight offered siblings), without importing generation engines. Stale translation aborts, and cancellation-fenced tasks are never rejoined.

Long pipelines checkpoint at semantic stages. A retry resumes from a complete
checkpoint or recomputes the stage; partial output is never published as a
terminal artifact. Abort closes runtime state and prevents later background
callbacks from overwriting a terminal result. Auto-research overlaps its two independent primary judges behind a two-call ceiling; only material disagreement adds a third call, and final repository publication retries after every model-backed checkpoint. Post-report terminology backfill admits at most 60 gaps in 15-term batches, warms one shared report-prefix cache before bounded fan-out, and leaves any unfilled gap visible. arXiv streaming progress is a non-blocking one-item latest-value slot, so a disconnected consumer cannot accumulate page updates.
Auto-research preserves the submitted direction as its prompt and durable identity, but non-English directions receive one bounded, cached English discovery alias before arXiv search; that translation is accounted under harvest usage. The browser also sends its active language with the start request so downstream artifacts use the language the user selected.

A completed auto-research direction opens an owner-scoped Research Foundry program while frozen survey/novelty artifacts remain read-only evidence. `contracts/research_program_v1.schema.json` is the authority for its falsification protocol, at most 24 exact capability bindings, 32 evidence-bearing runs, 32 claim/evidence records, full conference-paper sections, 24-file/384-KiB LaTeX source tree, figures/tables, and compile/publication receipts. `GET /api/v1/research/workspace` returns revision zero; `PUT` and manuscript scaffolding use compare-and-swap, so stale writers receive 409. Direction hash, language, and user form the key. Submission readiness is derived from protocol, artifact-backed passing runs, supported claims, complete source, and a compile receipt whose source digest is still current—never stored as a percentage.
The action line starts `research-action` jobs through the generic task API for `experiment`, `analyze`, `manuscript`, `compile`, and `publish`. They reuse the guarded Paper Agent loop and shared dispatch, but freeze a narrower least-authority epoch: local search/code tools are action-specific, and an MCP tool is executable only when its exact namespaced name and schema hash are saved under a relevant capability; drift revokes execution until the owner reviews and saves the binding again. Catalog suggestions inspect live name/description/schema metadata and grant no authority. Experiment, analysis, compile, and publish require an explicit start confirmation; compile requires `manuscript.compile`, publish requires `publication.push`. No server name is privileged, so private LLM/HOPE/Overleaf services and third-party providers follow the same contract. Model prose cannot assert a passing run, compile, or publication: deterministic settlement requires a successful call receipt from that action. Source ZIP export is pure and deterministic. A compiler or publication platform remains an external bound tool rather than a second manuscript authority.
Research-only paper loops freeze their exact post-policy schemas and execution
documents each round. Full report, Q&A, and deepen loops instead freeze one
`PaperToolEpochV2`: an uncapped-by-default provider wire projection and a larger
server-owned executable catalog derived from the same registry pass. An
explicit `tools.schemaBudgetTokens` applies the same model-neutral optional-tool
cost target used by local Tool Search. The fixed `search_tools`/`execute_tools`
pair targets 600 tokens and returns the exact hidden contract before routing a
child through the shared permission, validation, approval, and settlement
pipeline. Q&A starts prefer a canonical hash; cold source reads cap at 1,000,000 characters and repeat starts use a launch-probed 600-second owner+hash TTL/LRU after a zero-text authorization check. Its prompts share one 60,000-character report/paper relevance budget; ten recent messages share 24,000 characters (8,000 each), while CJK bigrams preserve Chinese tail retrieval. Before selection, the complete 232-code-point Unicode Cc/Cf neutralization runs through a C-level translate/range plan and semantic-superset literal gates skip irrelevant directive regexes; the four Unicode ASCII-case exceptions keep full matching. Report/Q&A/Deepen/Insight/Recommend/Survey/Ideate share one finite-progress policy: three identical call+world rounds halt before a fourth duplicate tool execution, and independent token/dispatch envelopes reserve a final tool-less synthesis call even without usage metadata; synchronous abort/halt cannot publish partial artifacts. Unattended `ask_human` and registry-declared attended-confirmation tools are absent because no headless task can mint their one-use receipts; ordinary writes remain explicitly auto-applied and audited. An aborted Q&A loop remains
`aborted` and cannot emit `done`.
The fully resident search runtime keeps the required paper wire floor at or below 4,000 tokens without removing direct research, file, or artifact authority.

Shipped/default paper tools budget through an internal
`ToolResultEnvelopeV2`, then put only its sparse semantic projection in the
model message and retain the bounded evidence sidecar on the round. One result
is at most 8,000 model-visible tokens and all results from one logical model
round are at most 24,000. Oversized evidence is stored only through the semantic, owner-scoped
artifact repository and continued with `read_tool_artifact` or
`search_tool_artifact`; production paper application data never exposes a disk
path or SQLite detail. Batched local file reads reuse the shared per-file
projection contract, so every requested path remains represented even when a
large first file exhausts preview space; paper adapters consume the same
request-local sidecar before persisting their round state. Continuation tools
join a non-empty paper epoch only for
a positive owner, while owner-less compatibility calls stay bounded and return
no invented recovery handle. The registered `tool_result_v2` experiment may
explicitly select the bounded legacy adapter for its control arm. Such a run is
request-local: it neither reads nor overwrites the canonical report/deepen
cache, and its model+config fingerprint cannot join another arm's live task.

The reading experience projects one canonical experience manifest from report
state. Section anchors are proposed by the model but resolved deterministically
against the stored document. Cost/progress comes from recorded task/model usage,
not a client estimate. “Deepen” is an explicit bounded task that starts from a
resolved section; it does not hide a second report inside the first response.
Localized metadata uses language-qualified semantic keys rather than duplicating
the source record.

Podcast generation separates structured spoken script, deterministic formula,
figure, and terminology validation, optional critic repair, TTS, audio assembly,
and artifact publication. Script draft/revision/critic calls propagate task cancellation and use the finite production 429 budget. Short/full scripts admit at most 24/64 segments and 160 synthesis chunks; segment calls use a launch-probed 1..2 personal fan-out (distributed 4, hard ceiling 8), stop new admission after failure/abort, and retain at most 32 MiB per part / 192 MiB before ordered assembly. Motion-video topic/report scripts and scene authors likewise use two hard-error slot attempts plus the finite 429 budget; task-backed calls propagate cancellation through request waits and transient backoff. Director mode adds exactly one contrasting draft and at most one bounded critic after both deterministic gates; standard remains the one-call control, and mode changes checkpoint/dedup identity. Motion narration reuses the TTS fan-out for at most 16 scenes / 64 chunks, rejects scenes over 60 seconds or incompatible provider WAV parameters, removes failed partials, and binds text/timing/settings plus bounded file hashes in the immediately persisted resume manifest. Motion stock preflight admits at most one request per scene, 16 MiB per image or 32 MiB per video, localises bytes inside the job lifecycle, and writes bounded public attribution without credentials. Slide outlines/authors/QA use the finite text-model policy; independent pages overlap behind the 1..2 personal fan-out and only exact authored YAML with bounded size/hash plus zero-LLM validation resumes. Image preflight has a separate 1..2 personal fan-out, two hard attempts, finite 429s, six-image/20-MiB bounds, while caller and streamed remote images are capped at 20 items/40 URLs and 192 MiB per channel with exact crash caches. Invalid scripts never reach TTS; a partial audio file never becomes the published podcast or narrated video.

Ordinary server registration keeps only report, podcast, deepen, QA,
translation, and recommendation task authorities. Engines load on requests;
a startup interruption sweep may load the podcast worker shell, but the LLM
script and TTS/audio stages remain dormant until generation reaches each stage.
The arXiv fetch/search routes retain only a lightweight typed query failure and
patchable call seams; XML/Atom parsing, search activation, and HTTP title lookup
load on the first real request. Review route registration resolves venue/prompt
metadata but leaves deterministic review text processing and the shared
language-detection cascade dormant.

## Translation

Translation separates language/direction detection, segmentation, LLM work,
validation, and durable commit. Segment caches key canonical content plus
source/target language and relevant policy. A refusal, wrong-language result,
truncation, or over-generation is a typed invalid result, not cacheable output.

Settled-turn enrichment excludes translated/target-language/protected/cache hits and batches 16 segments or 6,000 source characters per call. The authoritative whole-turn translation commits and the user-visible task settles first; reconstructible segment enrichment is then re-admitted behind the owner's existing bounded translation queue with no independent durable lifecycle. It receives one shared 15-second wall budget, one upstream-429 attempt per call, and immediate-only shared-contention admission. Saturation may drop it; only a provider response that damages placeholder order/ownership may fall back to isolated validation, while dispatch/capacity failures skip the optional batch without fan-out and can never change the whole-turn task from success to failure.
General, PPTX, settled-turn whole-output, and paper translation share one owner-fair lane (personal 1..2 workers/4..32 pending; distributed 16/128; idle retirement 60/600 seconds). The worker value FIFO-caps active MT/LLM calls across that lane, synchronous send work, and incremental accumulators; the queue value also caps provider waiters, with retryable fail-fast saturation and no local redispatch. Cache/identity/protected fast paths take no slot. Tasks remain `pending` until entry and queued abort frees capacity. A dispatch allows 4..8 actual rate-limit responses in personal mode (16 distributed, hard ceiling 64); capacity polls are free, exhaustion stops the workflow and later paper chunks. Non-terminal incremental previews additionally yield before transport to an already-active shared-contention gate; their call count and one-429 circuit live on the Task/Turn across idle accumulator retirement, while terminal reasoning, final/send translations, and interactive Agent dispatch retain their ordinary user-cancellable wait policy.
Each LLM translation candidate also enters request-local strict billing-stop admission. A recorded key-wide 402 or matching model quota stop wins over a persistent manual-ON override for this optional work only; it cannot be last-resort promoted or bypassed through `smart_chat`'s direct default-key fallback. Healthy sibling models/providers remain usable, while a wholly rejected pool terminates before an upstream request, 300-ms cooldown poll, outer backoff, or per-segment fallback fan-out and projects as retryable `no_slot`/503. Attended Agent calls retain the ordinary Settings user-supremacy contract.

Paper and general translation share validation/cache/MT primitives but keep distinct provenance stores. Paper work packs 8,000-character semantic slices under 1,000,000-character, 128-slice, and two-hour ceilings; validated output stops at 2,000,000 characters/4,000,000 UTF-8 bytes, chunks retain only progress plus replay deltas, the browser renders the complete artifact once, and terminal polling carries either its done event or its caught-up snapshot rather than both.
Automatic-model work may reuse cache/MT; `force` skips cache reads, while an explicit model skips MT/cache reads and stays strictly pinned. LLM slices fail closed on empty/no-op/wrong-language/truncated/over-generated output.
A rejected slice, abort, deadline, or unconfirmed owner-repository write fails the whole task; partial/error placeholders are never successful artifacts. Canonical reports require a non-empty body and confirmed write, request-local experiment arms explicitly suppress that write, and podcast `script_only`/`done` rows require the same confirmation. Measured cost evidence stays in `CHANGELOG.md`.

General-translation route registration retains only constants, pure prompt
transforms, the typed refusal identity, and the shared task authority. The
`lib.translate` and `lib.translate.runtime` compatibility facades resolve the
LLM/MT engine, worker, incremental commit path, and PPTX translator only when
that operation starts. Adding a package-level export must preserve this focused
import boundary; route discovery is not authority to initialize an engine.

## Media artifacts

Binary results are artifact references with MIME type, size, owner, lifecycle,
and provenance. Text companions are retained where the UI or accessibility
contract requires them. Audio/video clocks begin when meaningful processing
starts, not at unrelated page load or queue creation.

Motion-video quality gates operate on declared creative plans, timelines,
asset briefs, subtitle geometry, and render verdicts. A render process is not
successful merely because it exited zero; required output and quality
contracts must pass.

## Failure semantics

- Unsupported/oversized/malformed input: reject before task creation.
- Page parser failure: preserve page identity and surface diagnostics; follow
  the declared isolation/fallback policy.
- Duplicate source: return/reuse canonical identity without duplicating state.
- Invalid model-derived content: fail the stage and retain the last complete
  checkpoint.
- Abort: stop publication and settle the task once.
- Artifact storage failure: terminal persistence error; do not return a dangling
  URL or phantom success.

## Invariants

- One PDF extraction authority and one file-router decision point.
- Owner identity is explicit for source rows, derived rows, and artifacts.
- Shipped/default Paper tool results obey 8k/24k model-visible budgets without
  exemptions; an explicit legacy experiment control remains bounded and
  request-local.
- A full-paper wire budget never removes a capability from its owner-scoped
  Tool Search execution catalog.
- Report/deepen in-flight reuse requires an exact model+config fingerprint;
  long-agent policy requests never read or mutate canonical derived caches.
- Source commit and required metadata are atomic.
- Derived work records source hash/version and stage provenance.
- Checkpoints are complete, idempotent semantic boundaries.
- Invalid/refused/truncated output is never cached as success.
- Binary payloads stay out of JSON transcripts and ordinary logs.
- Chat metadata is owner-resolved and model projection is request-local/bounded;
  client fields and request-specific base64 are never persisted authority.
- Routes parse/project; pipeline and persistence semantics live in services.

## Change routing

| Change | Start here | Verify |
|---|---|---|
| PDF extraction | `lib/pdf_parser/` | page isolation, layout policy, dependency contract |
| Paper metadata/storage | `lib/paper/library.py` + Sidecar operations | owner isolation, hash, atomic upload |
| Report/insight/QA | focused paper engine/runtime | grounding, checkpoint, abort |
| Podcast | `podcast_engine/`, runtime | script validation, audio artifact |
| Translation | `lib/translate/` or paper translator | language identity, refusal, cache |
| Chat document/video attachment | `lib/media_attachments.py` + `lib/knowledge/` | owner scope, digest reuse, bounded projection, one delete lifecycle |
| Chat video analysis | `lib/video_analysis/` | durable source-before-work, atomic evidence commit, restart/status fallback |
| Motion video | `lib/motion_video/` | plan, assets, quality gate, render settlement |

## Test map

```bash
pytest -q tests/test_pdf_parser_page_isolation.py \
  tests/test_pymupdf_layout_policy.py
pytest -q tests/test_paper_ingest_persist.py \
  tests/test_paper_upload_atomic.py tests/test_paper_hash_canonical.py
pytest -q tests/test_paper_report_abort.py tests/test_paper_checkpoints.py
pytest -q tests/test_paper_arxiv_startup_boundary.py \
  tests/test_text_lang_startup_boundary.py
pytest -q tests/test_paper_request_policy.py tests/test_paper_full_tools.py \
  tests/test_paper_deepen.py
pytest -q tests/test_paper_podcast_api.py tests/test_paper_podcast_script.py
pytest -q tests/test_translate_identity_invariant.py tests/test_translate_refusal_cache.py tests/test_translate_startup_boundary.py tests/test_translate_dispatch_deadline.py
pytest -q tests/test_knowledge_data_layer.py tests/test_knowledge_enrichment_budget.py tests/test_knowledge_pdf_resource_budget.py tests/test_video_analysis.py tests/test_frontend_authoritative_composer.py
pytest -q tests/test_motion_video_engine.py \
  tests/test_motion_video_gate_verdict.py
```
