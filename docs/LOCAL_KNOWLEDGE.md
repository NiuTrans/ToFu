# Local Knowledge Base

The local knowledge base is a standalone product surface, not a tool-setting
form. Users open it from the top bar (or the mobile options sheet), manage a
durable corpus, validate retrieval, and separately decide whether models may
use the read-only `search_knowledge` tool.

## User contract

- Original files, extracted images, text proxies, chunks, and retrieval stay on
  the local Tofu server. Visual enrichment is off by default. Enabling it is an
  explicit consent action that sends image copies to the user's configured
  vision-model provider and stores only the returned description locally.
- Adding a first document enables model access by default. A later upload never
  overrides an explicit disabled choice.
- Disabling model access preserves the corpus and still permits authenticated
  retrieval previews in the workbench.
- Every result includes a stable evidence ID, source name, section, location,
  grounded source excerpt, and any linked original image assets. Document text,
  OCR, images, and generated descriptions are treated as untrusted reference
  data.
- Stored source bytes are content-addressed and retained so a user can rerun the
  latest parser without deleting or uploading the document again.

## Ingestion pipeline

`lib/knowledge/ingest.py` orchestrates parsing strategies:

1. Inspect bytes before trusting the filename suffix.
2. Reject unsafe ZIP/Office packages by member count, expanded size, and
   compression ratio.
3. Dispatch native parsers for standalone raster images, PDF, modern and legacy
   Office documents, OpenDocument, EPUB, RTF, RFC email (including bounded
   searchable attachments), HTML, structured text, source code, and ordinary
   text.
4. Preserve captioned and uncaptioned PDF images, scanned-page renders, Office/
   OpenDocument/EPUB package media, and image email attachments. PDF figures
   retain page and source bounding-box provenance.
5. Use local best-effort OCR for scanned PDFs and standalone images when the
   local OCR stack exists.
6. Return the extraction method, page count, and every truncation/degradation
   warning as document metadata.
7. Structure-aware chunking preserves headings, sheet/table context, line
   locations, and bounded overlap.
8. Persist documents, immutable image assets, textual proxy chunks, asset links,
   and deduplicated search terms as one semantic unit. Storage failure rolls
   back the unit; file candidates are cleaned without racing concurrent writers.

Ingestion is bounded to 50 MB per file, 20 files and 200 MB per HTTP batch by
the management API. A PDF holds one process-wide classic-parser lease across
validation, text, OCR, visual/source persistence, and the atomic repository
commit; the 8 GiB reference admits three such pipelines. OCR stops as soon as
the retained text budget is full. Extracted
text and OCR page limits are configurable through
`TOFU_KNOWLEDGE_MAX_TEXT_CHARS` and `TOFU_KNOWLEDGE_OCR_MAX_PAGES`. OCR and
visual page overrides may lower but cannot exceed the launch-derived classic
PDF page ceiling. Visual work is separately bounded by `TOFU_KNOWLEDGE_VISUAL_MAX_PAGES`,
`TOFU_KNOWLEDGE_MAX_VISUAL_ASSETS`, `TOFU_KNOWLEDGE_MAX_VISUAL_BYTES`,
`TOFU_KNOWLEDGE_MAX_ASSET_BYTES`, and `TOFU_KNOWLEDGE_MAX_IMAGE_PIXELS`.
Defaults preserve 80 OCR pages, 80 visual pages, 160 assets / 160 MiB per
document, 25 MiB per asset, and 40 million decoded pixels per image. Thus the
reference aggregate retains at most 150 MiB of compressed sources plus 480 MiB
of accepted visual candidates before parser-native transient state.

## Large-corpus management

The management surface is bounded independently from retrieval. It never sends
or renders the entire document catalogue:

- The document API returns 30 rows by default (100 maximum), with server-side
  filename filtering, sorting, and page metadata. The client keeps only the
  active page in the DOM.
- Corpus totals and type facets are computed with aggregate queries. Per-file
  asset counts run only for the selected page, using the document/asset index.
- Stable indexes cover owner/time browsing, document dependencies, enrichment
  queues, and the inverted search projection. Catalogue responses and browser
  state stay bounded even when the stored corpus is much larger than one page;
  filtered result counts remain authoritative on the server.
- Types are derived from sniffed canonical formats rather than an untrusted
  upload suffix: PDF, documents, spreadsheets, presentations, images, email,
  ebooks, text/code, and other. The same category keys drive backend filtering
  and frontend facets.
- Parsed-body inspection returns 80 chunks at a time (200 maximum). “Load more”
  appends another bounded page instead of constructing one enormous response or
  DOM subtree.
- Visual-enrichment polling updates status without replacing an unchanged list.
  If a visible row really changed, both the library scroll position and the
  parsed-body scroll position are restored after the targeted render.

## Storage authority

The Sidecar is the only durable authority. It owns normalized, owner-scoped
tables for settings, documents, chunks, assets, chunk/asset links, and inverted
terms. `KnowledgeRepository` injects an explicit owner into every semantic
query or receipted command; routes, workers, and tools do not open a database
driver or send SQL. Create, replace, and delete keep every dependent row in the
same transaction.

Original sources and image assets live below
`<data>/knowledge-files/<owner_user_id>/{sources,assets}`. Stored names are
validated immutable basenames, and every rollback candidate has a unique path,
so one concurrent request cannot delete another request's committed file.
The removed application-owned auxiliary database is intentionally not imported:
it did not carry trustworthy owner identity. Parser and search projections can
instead be rebuilt from the retained source bytes.

## Multimodal retrieval

The backend-neutral inverted index stores normalized word tokens plus CJK
bi/tri-grams.
 Individual search terms are bounded to
128 characters; longer uninterrupted runs (base64 blobs, watermarks, embedded
URLs) are dropped at ingest rather than failing the whole transaction.
 Candidate selection is index-backed and ranks chunks matching
more distinct query terms before applying a hard response bound; it never scans
or serializes the complete corpus into an application process. Application
reranking combines token coverage, exact compact phrases, deterministic intent
expansion, and title/section evidence. Results are diversified across documents
and bounded by both result count and total output characters.

Each image is represented by two deliberately separate layers:

1. The original immutable pixel asset is authoritative visual evidence.
2. A rebuildable text proxy combines filename, caption, page context, local
   OCR, and (when consented) a factual vision-model description.

This is a late-fusion design: normal lexical retrieval ranks the proxy alongside
ordinary text chunks, then the top linked originals (bounded to three per tool
call) are attached to the model response. A vision-capable chat model receives
real `image_url` blocks. A text-only model receives the same grounded excerpt,
OCR/caption/description text, and an explicit notice that visual verification is
unavailable. The system never represents generated prose as the original image.

Normalized Sidecar rows and original files are authoritative. Model descriptions
and any future dense or ColPali-style visual index are rebuildable projections.
This preserves simple deletion, atomic reindexing, deterministic rollback, and
a useful local-only baseline instead of making an external vector database or
VLM a correctness dependency.

When visual enrichment is enabled, a single daemon worker leases pending assets,
uses the configured `vision` capability pool (including its normal health and
routing), and updates the asset, proxy chunk, and inverted terms in one
transaction. Expired leases resume after restart; missing vision capacity and
failures remain visible in status rather than silently dropping image evidence.

Model tool availability is fail-closed and conditional on both an enabled flag
and a non-empty corpus. The management preview endpoint intentionally bypasses
only the enabled flag so a user can test evidence before granting model access.

## Chat attachment projection

Chat uploads share this store (scope `attachment`/`draft`). Projection into a
model request is bounded and honest about partial coverage:

- The per-attachment excerpt budget scales with the chat model's context
  window (12% of the window in estimated characters, floored at 48k and
  clamped at 240k via `lib.media_attachments.document_text_budget`); one
  request-wide allowance (2×) is shared fairly across all attachments.
- When the full text does not fit, the attachment header states exactly how
  many of the document's characters were injected, whether the excerpt was
  chosen by relevance search or — annotated explicitly when search matched
  nothing — from the document head, and instructs the model to page for more.
- `read_files` resolves the stable `att_media_<id>` ref as a virtual
  line-paged text file (owner-scoped; legacy `att_txt_<hash>` refs scan the
  task's stored messages). Whole reads above one bounded page return a
  continuation hint with the exact `start_line` to resume from, so a long
  document is a sliding window away instead of being truncated forever.
- Tool-round titles and per-file result projections render the user's exact
  original filename, marked as an uploaded attachment — never the raw ref.

## Management API

- `GET /api/v1/knowledge` — status, bounded documents, totals, type facets,
  limits, and supported formats; accepts `page`, `page_size`, `query`,
  `category`, and `sort`
- `GET /api/v1/knowledge/activity` — index-backed lightweight enrichment counts
  for polling; it does not rescan or return the document catalogue
- `POST /api/v1/knowledge/settings` — independently control model retrieval and
  consent-gated visual enrichment
- `POST /api/v1/knowledge/documents` — upload and index one batch
- `POST /api/v1/knowledge/search` — authenticated retrieval preview
- `GET /api/v1/knowledge/assets/:id` — authenticated original or thumbnail image
- `GET /api/v1/knowledge/documents/:id/content` — bounded parsed chunks; accepts
  `offset` and `limit`
- `POST /api/v1/knowledge/documents/:id/reindex` — rerun the current parser
- `DELETE /api/v1/knowledge/documents/:id` — remove index and stored source

All endpoints require the normal API authentication policy.

Upload, reindex, and delete return bounded metadata projections only; parsed
body text is always read back through the bounded content endpoint. Composer
attachment previews fetch it on demand instead of holding client-side copies.

## Verification

The focused backend suite covers conditional tool exposure, disabled-state
preservation, content deduplication, CJK retrieval, inverted-candidate ranking,
bounded parsed-body reads, owner isolation, sparse spreadsheets,
misleading suffixes, DOCX, HTML sanitization, RTF unicode, email attachments,
OpenDocument detection, image magic/limits, captioned and uncaptioned PDF
visuals, atomic asset links and enrichment, concurrent upload and
visual reindex, authenticated asset CRUD, and text-only/vision model behavior.
The browser suite drives upload, indexing, preview retrieval with a real image
thumbnail, both switches through their full cycles, explicit provider consent,
an unsupported binary, deletion confirmation, and desktop/mobile viewport fit.
