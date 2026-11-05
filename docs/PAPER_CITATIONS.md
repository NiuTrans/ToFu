# Paper citations (PID-anchored, manubot model)

## Problem

A language model writing a LaTeX paper will invent plausible-looking
references. Once a hallucinated entry sits in a .bib, downstream builds
succeed and the fabrication ships. The citation stack resolves identifiers
before insertion and exposes missing verification. Metadata resolution
establishes neither scientific quality nor whether a cited paper supports a
particular manuscript claim.

## Decision

Citations enter the manuscript only from persistent identifiers (PIDs)
resolved against public scholarly indexes — the manubot model:

1. `paper_cite_search` queries OpenAlex (250M works) and returns normalized
   candidates with DOI, venue, year, citation count and retraction flag.
2. `paper_cite_insert` takes a PID (DOI / OpenAlex ID / arXiv ID), fetches the
   real BibTeX by Crossref content negotiation
   (`/works/{doi}/transform/application/x-bibtex`, with `doi.org` Accept
   negotiation for DataCite DOIs such as arXiv), synthesizes a minimal entry
   from the OpenAlex record when no BibTeX source answers, appends the entry
   to the project .bib (deduplicated by key AND DOI), and inserts `\cite{key}`
   at a unique text anchor. Both mutations go through
   `lib/project_mod/write_tools`, so they are journaled and undoable like
   every other agent edit.
3. `paper_citation_audit` pages through parsed .bib entries against OpenAlex (DOI
   preferred, else title search with a 0.85 similarity floor), comparing title
   similarity, year (±1) and first-author surname, and flags
   verified/mismatch/not_found/retracted/skipped plus duplicate keys,
   undefined citations and unused entries.

OpenAlex + Crossref were chosen because both are free, keyed, proxy-friendly
JSON/BibTeX APIs that cover journal articles, preprints and books; OpenAlex
additionally carries retraction flags and citation counts, and indexes arXiv
via the DataCite DOI `10.48550/arXiv.<id>`. GROBID (PDF-reference parsing) is
deliberately deferred: it answers a different question (extracting references
from an existing PDF), needs a heavyweight service, and adds nothing to the
insert-path integrity guarantee.

## Tool contracts

- `paper_cite_search {query, limit?}` — read-only; ≤25 candidates; 20s HTTP
  bound. Empty query → `citation_query`; backend failure →
  `BuildError('citation_backend')`.
- `paper_cite_insert {doi|openalex_id|arxiv, key?, cite_file?, tex_file?,
  anchor?, anchor_position?, path?}` — WRITE tool (approval-gated in the
  registry). Keys must match `^[A-Za-z0-9:_./-]{1,64}$`; anchors are 4–300
  chars and must occur exactly once; an existing `\cite{key}` at the anchor
  makes the call an idempotent no-op. The verdict reports whether any
  `\bibliography`/`\addbibresource` wires the target .bib into the TeX.
- `paper_citation_audit {path?, cursor?}` — read-only; verifies ≤8 entries per
  call within a 25s network budget. Follow `next_cursor` with the same `path`
  until the end; `retry_cursor` revisits a page containing unverified rows.
  Entries not started before the deadline remain on the next page. Exit
  status is `error` when mismatches, not-found, retracted, duplicate or
  undefined problems exist. Cursor selection never certifies prior pages.

## Resumable coverage

**Problem:** repeatedly auditing a larger bibliography always checked its first
eight entries, while unreadable or oversized local sources could disappear from
the report. Old search-cache results also concealed changed metadata.

**Decision:** `citation_audit_pages.py` reads one bounded complete source set and
binds page positions to the exact file bytes, relative paths, selected main file
and resolved project root. Every page includes `source_digest`, its half-open
`coverage.start/end`, `next_cursor` and `retry_cursor`. Re-read source identity
after network verification; source changes refuse the result and require a
fresh audit. Missing/unreadable sources and file/depth/byte/entry overflow fail
explicitly before presenting complete coverage. Hidden/vendor directories stay
outside the declared source walk; symlinked directories cannot certify coverage.

The cursor is a position, not evidence or authorization. It carries no claimed
earlier verdicts. Retain the ordinary tool receipts and combine contiguous pages
with the same source digest, resolving every skipped page and finding. Only a
single page that starts at zero and covers every entry without skipped rows can
report complete coverage by itself. Reaching the last page never establishes
that earlier pages passed. No new durable store, background worker or agent loop
is introduced.

Verification refreshes OpenAlex metadata instead of borrowing the search/insert
cache. Each HTTP request borrows the remaining call-local deadline, and polite
delays cannot extend it. Malformed JSON payloads remain unavailable evidence.
Timeouts are transport bounds; cancellation/timeout settlement remains with the
existing tool runtime.

**Alternatives:** an unbound offset can mix edited bibliographies; trusting
caller-supplied cumulative counts invents coverage; increasing the per-call
network ceiling delays interactive tools. A second audit task/store would
duplicate the shared tool-receipt lifecycle. Source-bound pages preserve the
existing invocation and ownership boundaries.

## Bounds

Per call: one 20s HTTP timeout per request; search ≤25 rows; verification ≤8
entries / 25s deadline / sequential with a polite delay; .bib ≤4096 entries /
4 MiB; source walks ≤512 files / ≤8 MiB per snapshot / depth ≤5 (hidden and
vendor dirs skipped); tex reads ≤2 MiB. Each successful audit reads two bounded
source snapshots and returns at most eight verification rows. Search/insert
retain an `lru_cache(256)` on OpenAlex lookups (marked TODO(enterprise)); audit
deadlines use a call-local ContextVar that is restored on every exit.

## Failure modes

- OpenAlex/Crossref unreachable → search/insert raise
  `BuildError('citation_backend')` (handler: 稍后重试); audit marks rows
  `skipped` and still reports all local findings.
- Any skipped row, pending page or nonzero starting position makes this call's
  whole-document coverage incomplete. Tool `done` remains execution completion;
  compilation cannot validate unverified references. An empty bibliography
  reports that no entries were externally verified. Verify the coverage and
  paging suites alongside the citation stack tests.
- Crossref 404 → doi.org negotiation → OpenAlex synthesis; if the PID is
  unknown everywhere, `citation_not_found` (handler steers back to search).
- Ambiguous/missing anchor, invalid key, non-.bib/.tex targets, path
  traversal, and .bib caps are typed `BuildError`s before any file I/O, so a
  refused call never mutates the project.

## Enterprise seam

All state is per-call or derived from the owner's document root resolved via
`_resolve_document(conv_id, owner_user_id, path)` — the same ownership check
as paper builds; there is no global rate-limit state or shared cache beyond
the bounded per-process lookup cache. Multi-tenant evolution: replace the
process cache with per-owner TTL caches and move politeness delays into a
per-owner request scheduler; the handler already threads `owner_user_id`
through every entry point.

Verify with `tests/paper/test_paper_citations.py`, `test_paper_citation_coverage.py`
and `test_paper_citation_paging.py` (network fully mocked at
`lib.paper.citations.http_get`; mutations run against the real journaled
write tools on tmp roots).
