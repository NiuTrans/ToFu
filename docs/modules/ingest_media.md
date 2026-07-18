# Module Design Doc — Unit 11: Ingest / Media (`pdf_parser/`, `paper/`, `translate/`, `doc_parser`, `file_reader`, `image_gen`, `transcription`)

> Part of the per-module design-doc set (see `docs/ARCHITECTURE.md`). This unit
> is the ingest/media cluster: turning external artefacts (PDF, Office doc,
> audio, a paper, an image prompt) into text/images/reports.
>
> **Grounding:** every line count is `wc -l` on disk 2026-07-11. `list_dir`
> overcounts — all numbers are `wc -l`. Every MISCUT/BIG verdict cites competing
> responsibilities or line ranges; size alone is never the argument.
>
> **The analytical payload:** (1) do these pipelines SHARE an extraction/LLM
> core or each re-invent PDF text extraction + chunk-LLM + image handling? and
> (2) a close read of `paper/report_engine.py` (the single-pass architecture).

---

## 1. Shared ingest core vs re-invented extraction

The risk: `pdf_parser`, `paper`, and `doc_parser` each re-implementing PDF text
extraction / figure clipping / chunk-LLM machinery — duplicated extraction logic
would be a real segmentation defect.

**Verdict: NO duplication — there is ONE PDF core (`lib/pdf_parser/`) and
everything composes it downward. `file_reader` is the single file→text router;
`paper` reuses `pdf_parser` for figures; the LLM plumbing is shared via
`lib/agent_loop` + `lib/llm_dispatch`, not re-invented.** Traced from every
import edge:

### 1a. `pdf_parser/` is the single PDF-extraction source

Grepping every `pdf_parser` import site: only TWO external consumers, both
importing DOWN into the one core, neither re-implementing it:
- `file_reader.py:445` → `from lib.pdf_parser import extract_pdf_text` (the
  generic file-read path).
- `paper/images.py:89-90` → `from lib.pdf_parser._common import PYMUPDF_LOCK` +
  `from lib.pdf_parser.images import detect_and_clip_figures` (the paper report
  reuses the SAME figure-clipping the core provides — it does NOT re-extract).

Inside `pdf_parser/` the layering is clean (a `compaction/`-style split): `core`
orchestrates → `text` (pymupdf4llm extraction) + `images` (figure clip/render) +
`math` + `docling` (opt-in layout) + `vlm` (VLM fallback) + `postprocess`, all
sharing `_common` (the `PYMUPDF_LOCK` + limits). `pool` is the process-pool
offload. `__init__` is a guarded-facade (optional submodules degrade gracefully).
**One PDF text extractor (`text.extract_pdf_text`), one figure clipper
(`images.detect_and_clip_figures`) — no second copy anywhere.**

### 1b. `file_reader` is the single file→text ROUTER (composition, not duplication)

`file_reader.read_local_file` dispatches by extension: image → native VLM upload
(base64 `__screenshot__`), PDF → `pdf_parser.extract_pdf_text`, Office →
`doc_parser.extract_document_text`, else → text with encoding detection. It
IMPLEMENTS none of these — it routes to the specialized extractor. `doc_parser`
(Office) and `pdf_parser` (PDF) are the two leaf extractors; `file_reader`
composes them. That's the correct shape: one router, two down-called extractors,
zero overlap (Office ≠ PDF, different libs). `read_files` (Unit 3) → `file_reader`
for absolute paths → these extractors. Single ingest funnel.

### 1c. The LLM plumbing is SHARED, not re-invented across paper/translate

The paper engines and translate do NOT each hand-roll a tool loop or a stream
retry — they reuse the shared seams (verified):
- **`paper/report_engine`, `qa_engine`, `insight_engine`, `recommend_engine`**
  all drive `lib.agent_loop.run_agent_loop` + `AbortSignal` (the shared
  multi-round tool-loop + abort seam from Unit 1) over `dispatch_stream`. The
  report engine's docstring + code confirm `run_agent_loop(...)` with
  `_REPORT_TOOLS`. So the four paper engines share ONE agentic-loop
  implementation, differing only in prompt + tool set + finalization.
- **`paper/llm_stream._stream_llm_sse`** is a thin 63-line `dispatch_stream`
  wrapper (queue+thread → SSE) reused by paper QA/translate.
- **`translate/engine`** uses `dispatch_stream`/`smart_chat` directly (with its
  own aggressive truncation/no-op/wrong-language retry policy — a translate-
  specific concern, not a re-implementation of dispatch).
- **`transcription`** reuses the `llm_dispatch` SLOT POOL (not a vendor branch) —
  it picks a transcription-capable slot and issues the multipart POST / inline
  chat itself, because the chat JSON+SSE shape can't carry audio. Capability-on-slot,
  not `if provider==`. **This is the correct reuse** — it shares slot selection,
  specializes only the wire shape.

**Conclusion:** the pipelines share the PDF core, the file router, the agent
loop, and the dispatch/slot layer. Each specializes only its OWN concern (paper =
report structure, translate = truncation-robust chunk translation, transcription =
audio wire shape, image_gen = image API). No extraction or LLM-loop duplication.

### 1d. The one duplication-shaped thing that ISN'T (image handling)

Image handling appears in three places — `pdf_parser/images` (extract figures
FROM a pdf), `paper/images` (inject figures INTO a report + title backfill),
`file_reader._read_image`/`_compress_image` (read an image file FOR the VLM),
`image_gen` (GENERATE an image). These are FOUR different verbs on images
(extract / inject / read-for-vlm / generate), not one operation done four times.
`paper/images` even reuses `pdf_parser/images` for the extract step (§1a). No
consolidation warranted.

---

## 2. Close read: `paper/report_engine.py` (owner-authored single-pass architecture)

The owner authored the single-pass architecture + EN/ZH terminology work and
asked for a plain verdict on its segmentation. I read `_run_report_task` in full.

**Verdict: the segmentation is RIGHT. `report_engine.py` (690) is correctly
bounded as the report WORKER, and the surrounding split (`report_runtime`,
`prompts`, `images`, `tools`, `citation_audit`, `terminology_audit`,
`insight_engine`) is a clean, deliberate decomposition — not a monolith and not
over-fragmented.** Specifics, grounded in the code:

- **It is ONE cohesive concern:** drive the tool-calling report loop
  (`run_agent_loop`), stream events, resolve the authoritative body, enrich with
  figures, persist, then optionally run the gated insight pass. Every helper it
  imports is a genuinely separate concern correctly living elsewhere: the task
  store/dedup (`report_runtime`), the prompts + tool list (`prompts`), figure
  extract/inject + title backfill (`images`), tool execution (`tools`), and the
  two zero-LLM audits (`citation_audit`, `terminology_audit`). The engine
  ORCHESTRATES them; it doesn't inline them.
- **The single-pass invariants are load-bearing and correctly localized here.**
  The `_terminal['content']` capture (adopt the terminal no-tool round's CLEAN
  returned content as the authoritative body, because streamed deltas can double
  on a mid-stream retry) and `_begin_tool_round`'s interim-draft discard
  (`delta_reset`) are the two anti-double-render guards. They belong exactly
  here (in the loop that produces the body) — they cannot be extracted without
  the loop. This is the right home.
- **The EN/ZH terminology work sits at the right seam.** `terminology_audit`
  (the zero-LLM self-containment gate) is imported lazily and attaches a
  `terminologyAudit` card to `report_meta` — it NEVER mutates the body, so it
  can't perturb the double-render/terminal-round logic. Same discipline as
  `citation_audit`. Both are correctly OUTSIDE `report_engine` (their own
  modules) and only touched at the meta-assembly point. That separation is
  exactly right: the audits are additive diagnostics, not part of the generation
  pass.
- **The one honest note (not a defect):** `_run_report_task` is a single ~430-line
  function with many nested closures (`_dispatch`, `_accumulate_usage`,
  `_begin_tool_round`, `_execute_tool`). It reads long, but the closures share
  the per-round mutable state (`_round`, `_terminal`, `full_content`,
  `_usage_total`) by necessity — that shared state is WHY they're closures and
  not module functions (extracting them would just re-thread the same 6 variables
  as params). It's cohesive-long, not miscut. If anything is ever pulled out, it
  would be Review-Mode finalization (`finalize_review_body`/`smarten_quotes` calls)
  — but those already live in `review.py` and are only CALLED here. **No split
  recommended for report_engine; its segmentation is correct as-is.**

The wider `paper/` package (25 modules) follows this pattern consistently: each
of the four engines (report/qa/recommend/insight) has a `{engine, runtime,
prompts}` triple + shared `images`/`arxiv`/`tools`/`injection_guard`/audits. That
is a deliberate, correct decomposition (the 3089-LOC `routes/paper.py` monolith
was split into this package — see `paper/__init__` docstring).

---

## 3. Module inventory (real `wc -l`, size verdict, status, tests)

### 3.1 `pdf_parser/` (2079 LOC, 10 files) — clean core

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `images.py` | 580 | **BIG** | HOT | via paper/pdf e2e |
| `vlm.py` | 322 | OK | live (opt-in) | via vlm e2e |
| `docling.py` | 248 | OK | live (opt-in) | via docling e2e |
| `text.py` | 244 | OK | HOT | via pdf e2e |
| `_common.py` | 171 | OK | HOT | — |
| `pool.py` | 139 | OK | live | via pool e2e |
| `math.py` | 138 | OK | HOT | via pdf e2e |
| `core.py` | 129 | OK (orchestrator) | HOT | via pdf e2e |
| `postprocess.py` | 78 | OK | HOT | via pdf e2e |
| `__init__.py` | 30 | OK (guarded facade) | — | — |

`images.py` — BIG (580), the figure detect/clip/render engine (bbox clustering,
caption association, page render). One cohesive concern (getting figures out of a
PDF); the size is intrinsic to the geometry. BIG-but-right; defer.

### 3.2 `paper/` (7059 LOC, 25 modules) — reference decomposition

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `insight_engine.py` | 985 | **BIG** | live (opt-in) | `test_paper_insight_agentic`, `test_paper_insight_cache_merge` |
| `review.py` | 900 | **BIG** | HOT (Review Mode) | `test_paper_review*` |
| `report_engine.py` | 690 | OK (correct — §2) | HOT | `test_paper_report_dedup`, `test_paper_date_anchor` |
| `images.py` | 632 | **BIG** | HOT | via report e2e |
| `recommend_engine.py` | 557 | **BIG** | live | `test_frontend_recommend_*` |
| `prompts.py` | 471 | OK (prompt data) | HOT | via report e2e |
| `qa_context.py` | 281 | OK | HOT | `test_frontend_paper_qa` |
| `arxiv.py` | 276 | OK | HOT | `test_frontend_paper_arxiv_title_math` |
| `terminology_audit.py` | 272 | OK | HOT | `test_paper_terminology_audit` |
| `tools.py` | 235 | OK | HOT | via report e2e |
| `injection_guard.py` | 226 | OK | HOT | via report e2e |
| `qa_engine.py` | 202 | OK | HOT | `test_frontend_paper_qa` |
| `__init__.py` | 192 | OK (facade) | — | — |
| `insight_prompts.py` | 181 | OK | live | via insight e2e |
| `translate_engine.py` | 159 | OK | live | via paper-translate e2e |
| `report_runtime.py` | 127 | OK | HOT | via report e2e |
| `recommend_runtime.py` | 101 | OK | live | via recommend e2e |
| `citation_audit.py` | 100 | OK | HOT | `test_paper_citation_audit` |
| `translate_runtime.py` | 95 | OK | live | via paper-translate e2e |
| `qa_runtime.py` | 95 | OK | HOT | via qa e2e |
| `recommend_task.py` | 84 | OK | live | via recommend e2e |
| `_task_store.py` | 85 | OK | HOT | via runtime e2e |
| `llm_stream.py` | 63 | OK | HOT | via qa e2e |
| `library.py` | 55 | OK | HOT | via library e2e |
| `hashing.py` | 51 | leaf | HOT | via report e2e |

`insight_engine.py` — BIG (985), the gated second-pass (research → synthesize →
ground → self-ref guard → score-gate → persist). Cohesive (one pipeline) but the
biggest paper file; the grounding + self-ref + rubric-scoring clusters are
separable. BIG-but-cohesive, opt-in path; defer. `review.py` (900) — Review Mode
(venue registry + review prompts + `finalize_review_body`/`smarten_quotes`/
scorecard-separator body finalization). Bundles the venue data + the submittable-
body finalizer — a split (venue registry vs body finalizer) is plausible; defer.
`images.py` (632) + `recommend_engine.py` (557) — BIG but each one concern.

**The paper package is a reference decomposition** (the `{engine,runtime,prompts}`
triple pattern per capability). No miscut; the BIG files are cohesive.

### 3.3 `translate/` (2673 LOC, 12 files)

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `engine.py` | 633 | **BIG** | HOT | via translate e2e |
| `incremental.py` | 521 | **BIG** | HOT | `test_incremental_translate` |
| `runtime.py` | 463 | OK | HOT | via translate e2e |
| `commit.py` | 277 | OK | HOT | via translate e2e |
| `dedup.py` | 197 | OK | HOT | via translate e2e |
| `pptx.py` | 137 | OK | live | via translate e2e |
| `inflight.py` | 120 | OK | HOT | via translate e2e |
| `__init__.py` | 114 | OK (facade) | — | — |
| `notranslate.py` | 110 | OK | HOT | via translate e2e |
| `prompt.py` | 52 | OK | HOT | via translate e2e |
| `status.py` | 40 | leaf | HOT | via translate e2e |
| `constants.py` | 9 | leaf | HOT | — |

`engine.py` — BIG (633), the single-chunk translate engine: cache → MT-provider →
LLM with the aggressive retry loop (empty/truncated/no-op/wrong-language-flip
detection + model exclusion + slot penalization). It's ONE concern (translate one
chunk robustly) but dense with failure-mode detectors. The detectors could be a
`translate_quality.py` (each `_is_truncated`/`_is_noop`/`_is_flip` check), leaving
the retry loop in `engine`. Cited by concern; classified BIG, defer (the checks
are tightly coupled to the retry loop's exclusion state). `incremental.py` (521)
— the incremental/diff re-translate; BIG but cohesive.

### 3.4 Media singles

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `image_gen.py` | 1124 | **BIG** | HOT | via image-gen e2e |
| `doc_parser.py` | 748 | **BIG** | HOT | via file-read e2e |
| `transcription.py` | 743 | OK | HOT | `test_audio_transcribe` |
| `file_reader.py` | 544 | **BIG** | HOT | via read_files e2e |

`image_gen.py` — **BIG (1124), bundles 2 provider families + the tool paths.**
Gemini (async submit+poll) + OpenAI (sync one-shot) generation, plus the
image-EDIT path, plus dispatch/cooldown cycling. The two provider families are a
clean split seam (`image_gen_gemini.py` + `image_gen_openai.py` sharing a
dispatch shell) — but note it's provider-shape-specialized, not a vendor `if`
(each family has a genuinely different wire protocol). Split candidate.

`doc_parser.py` — **BIG (748), bundles the Office formats.** docx / xlsx / pptx /
legacy doc-xls-ppt extraction, each a different library. A split by format-family
is possible; cohesive-ish (one "extract Office text" concern with per-format
branches). Classified BIG, defer.

`transcription.py` — OK (743) despite size. One cohesive concern (audio→text via
the slot pool) with two wire mechanisms (multipart endpoint + inline chat) + the
silence gate + hallucination flag + duration probe. It's long because the audio
domain has many best-effort guards (WAV level probe, cps flag), each small and
single-purpose. The capability-on-slot design (no vendor branches) is exemplary.
Cohesive; do NOT split.

`file_reader.py` — **BIG (544), bundles read + inspect.** `read_local_file` (the
router, §1b) + `inspect_image_file` (the crop/rotate/zoom re-render, ~250 lines) +
`_compress_image`. The `inspect_image` re-render is a separable concern from the
file-read router. Split candidate: `image_inspect.py`.

---

## 4. Dependencies (in / out)

**Inbound:** `routes/paper.py` (thin) + `routes/api_v1/paper.py` → the `paper/`
runtimes/engines. `routes/upload.py` + `project_mod/read_tools` (Unit 3) →
`file_reader`. `routes/translate.py` + `tasks_pkg/auto_translate` (Unit 1) →
`translate/`. `routes/api_v1/*` transcription route → `transcription`. Image-gen
tool (Unit 3 handler) → `image_gen`.

**Key internal edges (the shared core, §1):**
- `file_reader` → `pdf_parser.extract_pdf_text` + `doc_parser.extract_document_text`.
- `paper/images` → `pdf_parser.images.detect_and_clip_figures` + `_common.PYMUPDF_LOCK`.
- all four `paper/*_engine` → `lib.agent_loop.run_agent_loop` + `lib.llm_dispatch.dispatch_stream`.
- `translate/engine` → `lib.llm_dispatch` + `lib.translate_cache` + `lib.mt_provider`.
- `transcription` → `lib.llm_dispatch.factory.get_dispatcher` (slot pool) + `http_client`.
- `paper/insight_engine` → `lib.agent_core.personal_scope` (headless fail-closed, §3.7).

**Outbound:** `pymupdf4llm`/`docling` (PDF), `python-docx`/`openpyxl`/`python-pptx`
(Office), `PIL` (image), `lib/llm_dispatch` + `lib/agent_loop` (all LLM),
`lib/database` (paper library + report cache + translate cache).
**No back-edges up into routes.**

---

## 5. Invariants (must not be broken by a refactor)

1. **`pdf_parser` is the single PDF-extraction source** — text via
   `text.extract_pdf_text`, figures via `images.detect_and_clip_figures`. Never
   re-implement PDF extraction in `paper` or `file_reader`; call the core.
2. **`file_reader.read_local_file` is the single file→text router** — new file
   types add a branch here that delegates to a specialized extractor, never
   inline a new parser.
3. **The paper report's authoritative body is the terminal no-tool round's
   RETURNED content** (`_terminal['content']`), NOT the streamed deltas — a
   mid-stream retry doubles the deltas. `_begin_tool_round` discards a tool
   round's interim draft (`delta_reset`). Do not "simplify" to accumulate deltas.
4. **The paper audits (citation/terminology) attach to `report_meta` ONLY,
   never mutate the body** — so they can't perturb the double-render guards.
5. **Paper engines share `run_agent_loop`** (the Unit-1 abort seam) — don't fork
   a per-engine tool loop.
6. **Transcription is capability-on-slot, not vendor-branch** (`transcription`
   vs `audio_chat` caps select the wire shape) — no `if provider ==`.
7. **The transcription silence gate short-circuits BEFORE dispatch** (a measurably
   silent WAV returns empty with no billed call) — kills the silence→hallucination
   path at the source.
8. **Translate refuses to CACHE a known-truncated/no-op/flipped result** — caching
   a partial pins it forever; a fresh request must be able to re-translate.
9. **`paper/insight_engine` is headless-fail-closed** (`personal_scope`) — a BYO
   caller's analysis never gets the operator's library/memories.
10. **Review Mode is text-only** (no figures, straight→smart quotes, scorecard
    below the non-submittable separator) — a review is a decision document.

---

## 6. Known debt (grounded)

- **`image_gen.py` (1124) bundles Gemini + OpenAI generation + edit + dispatch**
  (§3.4) — a clean provider-family split.
- **`file_reader.py` (544) bundles the file router + the inspect_image re-render**
  (§3.4) — a clean split (`image_inspect.py`).
- **`doc_parser.py` (748)** bundles the Office format extractors — split-by-format
  possible, cohesive, defer.
- **`translate/engine.py` (633)** — the quality detectors could split from the
  retry loop, but they're coupled via exclusion state; defer.
- `paper/insight_engine` (985), `review.py` (900), `paper/images` (632) — BIG but
  cohesive.
- CLAUDE.md maps `doc_parser`/`file_reader`/`image_gen`/`transcription` as bare
  names; they're 544–1124-line modules worth their own line. Minor doc-drift.
- **No extraction or LLM-loop duplication** — the thing this unit was tasked to
  find is absent.

---

## 7. Segmentation verdict (this unit)

**Correctly bounded — leave as-is:**
The entire `pdf_parser/` package (clean core), the entire `paper/` package
(reference decomposition — INCLUDING `report_engine.py`, verdict §2), most of
`translate/` (runtime/commit/dedup/inflight/notranslate/prompt/status/pptx),
`transcription.py` (cohesive despite size), `paper/{prompts,arxiv,tools,
injection_guard,citation_audit,terminology_audit,*_runtime}`.

**Miscut — should split (priority order):**
1. **`image_gen.py` (1124) → `image_gen_gemini.py` + `image_gen_openai.py`** +
   a shared dispatch shell. The two provider families have genuinely different
   wire protocols (async submit+poll vs sync one-shot); the split matches that.
2. **`file_reader.py` (544) → extract `image_inspect.py`** (the crop/rotate/zoom
   `inspect_image_file` + `_compress_image`), leaving `read_local_file` as the
   router. Behind the read_files + inspect_image e2e.

**Big but optional (defer unless touched):**
`doc_parser.py` (748, split-by-Office-format), `translate/engine.py` (633,
quality-detectors vs retry-loop), `paper/insight_engine.py` (985),
`paper/review.py` (900, venue-registry vs body-finalizer), `paper/images.py` (632),
`pdf_parser/images.py` (580), `translate/incremental.py` (521),
`recommend_engine.py` (557).

**Do NOT split:** `report_engine.py` (§2 — correct as-is), `transcription.py`
(cohesive best-effort guards), the `pdf_parser` core modules, `paper/prompts`.

---

## 8. Comparison to Units 1–10 (the running thesis)

- **The feared cross-pipeline duplication is ABSENT** — one PDF core, one file
  router, one shared agent loop, one dispatch/slot layer, each pipeline
  specializing only its own concern. Same clean-composition outcome as Unit 3's
  tool seam and Unit 5's token authority.
- **`paper/` is a SEVENTH reference-quality package** (with `swarm/`,
  `token_counter/`, `compaction/`, `billing/`, `agent_core/`, `daily_report/`) —
  and `report_engine.py`'s segmentation, on close read, is genuinely right: a
  cohesive worker with its single-pass anti-double-render invariants correctly
  localized and its audits correctly externalized.
- **The miscuts here are all provider/format-family or read-vs-inspect splits**
  (`image_gen`, `file_reader`, `doc_parser`) — the same concern-boundary species
  as Unit 8's `oauth/codex` and Unit 9's `self_update`, NOT the many-concerns
  giants of Units 1–2. The ingest cluster is well-organized; its debt is modest.
- **The domain-robustness guards** (translate's truncation/flip detectors,
  transcription's silence gate + hallucination flag, the paper double-render
  guard) are this unit's real complexity — correctness machinery, not structural
  debt, echoing Unit 5's "the risks are invariants, not segmentation" finding.

---

*Next unit: Unit 12 (Integrations / API surface — `routes/`, `compat/`, `feishu/`,
`mcp/`, `desktop_*`, `openapi`) — the LAST unit.*
