# Ingest, papers, translation, and media

This domain turns uploaded or remote source material into validated text,
durable paper/library state, derived reports, translations, audio, images, and
video artifacts. It owns content-specific pipelines; generic task execution,
storage, and model transport remain separate domains.

## Ownership

| Concern | Owner |
|---|---|
| PDF extraction and page policy | `lib/pdf_parser/` |
| File-to-text routing | `lib/file_reader/`, parser packages |
| Paper library and ingestion | `lib/paper/library.py`, `harvest.py`, paper routes |
| Paper reports/review/insight/QA | focused modules under `lib/paper/` |
| Paper podcast | `lib/paper/podcast_engine/`, `podcast_runtime.py` |
| Paper translation | `lib/paper/translate_engine.py`, `translate_runtime.py` |
| General translation | `lib/translate/` |
| Transcription/audio | `lib/transcription/`, audio routes |
| Image generation/editing | focused image services and tools |
| Motion video | `lib/motion_video/`, motion routes/tools |
| Durable artifacts | artifact/storage services, Sidecar domain operations |

## Ingest flow

1. Validate type, size, and source authority before expensive work.
2. Stream or stage bytes through the upload boundary; never trust a filename as
   a storage path.
3. Select one parser through the file router.
4. Preserve page/section provenance and isolate page-level parser failures.
5. Canonicalize extracted text and metadata.
6. Commit the source row and required derived metadata atomically.
7. Start optional report, translation, podcast, or video work through a declared
   task runtime.

Remote fetches cross the shared URL/egress safety boundary. Parser fallback is
explicit and diagnosed; it must not silently return an empty successful
document.

## Paper state

The paper library and its derived records are owner-scoped Sidecar state.
Hashes provide content identity and deduplication; public filenames and arXiv
IDs are metadata, not ownership keys. Report, insight, translation, podcast,
and recommendation pipelines refer to the canonical paper record.

Long pipelines checkpoint at semantic stages. A retry resumes from a complete
checkpoint or recomputes the stage; partial output is never published as a
terminal artifact. Abort closes runtime state and prevents later background
callbacks from overwriting a terminal result.

Research-only paper loops freeze their exact post-policy schemas and execution
documents each round. Full report, Q&A, and deepen loops instead freeze one
`PaperToolEpochV2`: an uncapped-by-default provider wire projection and a larger
server-owned executable catalog derived from the same registry pass. An
explicit `tools.schemaBudgetTokens` applies the same model-neutral optional-tool
cost target used by local Tool Search. The fixed `search_tools`/`execute_tools`
pair targets 500 tokens and returns the exact hidden contract before routing a
child through the shared permission, validation, approval, and settlement
pipeline. Unattended `ask_human` is absent; an aborted Q&A loop remains
`aborted` and cannot emit `done`.

Shipped/default paper tool messages use `ToolResultEnvelopeV2`: one result is
at most 8,000 tokens and all results from one logical model round are at most
24,000. Oversized evidence is stored only through the semantic, owner-scoped
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
and artifact publication. Invalid scripts never reach TTS; a partial audio file
never becomes the published podcast.

Ordinary server registration keeps only report, podcast, deepen, QA,
translation, and recommendation task authorities. Engines load on requests;
a startup interruption sweep may load the podcast worker shell, but the LLM
script and TTS/audio stages remain dormant until generation reaches each stage.

## Translation

Translation separates language/direction detection, segmentation, LLM work,
validation, and durable commit. Segment caches key canonical content plus
source/target language and relevant policy. A refusal, wrong-language result,
truncation, or over-generation is a typed invalid result, not cacheable output.

Paper translation and general translation may share primitives but preserve
their own document/provenance projections. Neither writes directly into the
other's store.

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
- Routes parse/project; pipeline and persistence semantics live in services.

## Change routing

| Change | Start here | Verify |
|---|---|---|
| PDF extraction | `lib/pdf_parser/` | page isolation, layout policy, dependency contract |
| Paper metadata/storage | `lib/paper/library.py` + Sidecar operations | owner isolation, hash, atomic upload |
| Report/insight/QA | focused paper engine/runtime | grounding, checkpoint, abort |
| Podcast | `podcast_engine/`, runtime | script validation, audio artifact |
| Translation | `lib/translate/` or paper translator | language identity, refusal, cache |
| Motion video | `lib/motion_video/` | plan, assets, quality gate, render settlement |

## Test map

```bash
pytest -q tests/test_pdf_parser_page_isolation.py \
  tests/test_pymupdf_layout_policy.py
pytest -q tests/test_paper_ingest_persist.py \
  tests/test_paper_upload_atomic.py tests/test_paper_hash_canonical.py
pytest -q tests/test_paper_report_abort.py tests/test_paper_checkpoints.py
pytest -q tests/test_paper_request_policy.py tests/test_paper_full_tools.py \
  tests/test_paper_deepen.py
pytest -q tests/test_paper_podcast_api.py tests/test_paper_podcast_script.py
pytest -q tests/test_translate_identity_invariant.py \
  tests/test_translate_refusal_cache.py
pytest -q tests/test_motion_video_engine.py \
  tests/test_motion_video_gate_verdict.py
```
