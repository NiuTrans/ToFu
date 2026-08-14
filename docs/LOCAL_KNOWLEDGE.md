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
   and FTS rows as one semantic unit. Database failure rolls back the unit;
   file candidates are cleaned without racing concurrent writers.

Ingestion is bounded to 50 MB per file, 20 files and 200 MB per HTTP batch by
the management API. Extracted text and OCR page limits are configurable through
`TOFU_KNOWLEDGE_MAX_TEXT_CHARS` and `TOFU_KNOWLEDGE_OCR_MAX_PAGES`. Visual work
is separately bounded by `TOFU_KNOWLEDGE_VISUAL_MAX_PAGES`,
`TOFU_KNOWLEDGE_MAX_VISUAL_ASSETS`, `TOFU_KNOWLEDGE_MAX_VISUAL_BYTES`,
`TOFU_KNOWLEDGE_MAX_ASSET_BYTES`, and `TOFU_KNOWLEDGE_MAX_IMAGE_PIXELS`.

## Large-corpus management

The management surface is bounded independently from retrieval. It never sends
or renders the entire document catalogue:

- The document API returns 30 rows by default (100 maximum), with server-side
  filename filtering, sorting, and page metadata. The client keeps only the
  active page in the DOM.
- Corpus totals and type facets are computed with aggregate queries. Per-file
  asset counts run only for the selected page, using the document/asset index.
- Stable indexes cover updated time, creation time, type, and case-insensitive
  filename order. This keeps ordinary browsing bounded when the corpus grows to
  hundreds of thousands of files; filtered result counts remain authoritative
  on the server.
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

## Multimodal retrieval

The index uses SQLite FTS5 with normalized word tokens plus CJK bi/tri-grams.
Ranking combines lexical relevance, token coverage, exact compact phrases, and
title/section evidence. Results are diversified across documents and bounded by
both result count and total output characters. SQLite builds without FTS5 use a
bounded lexical fallback.

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

The SQLite corpus is the authority. Model descriptions and any future dense or
ColPali-style visual index are projections that can be rebuilt. This preserves
simple deletion, atomic reindexing, deterministic rollback, and a useful
local-only baseline instead of making an external vector database or VLM a
correctness dependency.

When visual enrichment is enabled, a single daemon worker leases pending assets,
uses the configured `vision` capability pool (including its normal health and
fallback routing), and updates the asset plus its FTS proxy in one transaction.
Expired leases resume after restart; missing vision capacity and failures remain
visible in status rather than silently dropping image evidence.

Model tool availability is fail-closed and conditional on both an enabled flag
and a non-empty corpus. The management preview endpoint intentionally bypasses
only the enabled flag so a user can test evidence before granting model access.

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

## Verification

The focused backend suite covers conditional tool exposure, disabled-state
preservation, content deduplication, CJK retrieval, sparse spreadsheets,
misleading suffixes, DOCX, HTML sanitization, RTF unicode, email attachments,
OpenDocument detection, image magic/limits, captioned and uncaptioned PDF
visuals, atomic asset links and enrichment, v1 migration, concurrent upload and
visual reindex, authenticated asset CRUD, and text-only/vision model behavior.
The browser suite drives upload, indexing, preview retrieval with a real image
thumbnail, both switches through their full cycles, explicit provider consent,
an unsupported binary, deletion confirmation, and desktop/mobile viewport fit.
