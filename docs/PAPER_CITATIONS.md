# Paper citations (PID-anchored, manubot model)

## Problem

A language model writing a LaTeX paper will invent plausible-looking
references. Once a hallucinated entry sits in a .bib, downstream builds
succeed and the fabrication ships. The citation stack must make fabricated
references architecturally impossible, not merely discouraged.

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
3. `paper_citation_audit` re-resolves every .bib entry against OpenAlex (DOI
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
- `paper_citation_audit {path?}` — read-only; verifies ≤8 entries per call
  within a ~25s budget; excess entries and backend outages degrade to
  `skipped` rows, never to a crash. Exit status is `error` when mismatches,
  not-found, retracted, duplicate or undefined problems exist.

## Bounds

Per call: one 20s HTTP timeout per request; search ≤25 rows; verification ≤8
entries / ≤25s / sequential with a polite delay; .bib ≤4096 entries / 4 MiB;
source walks ≤512 files / ≤8 MiB read / depth ≤5 (hidden and vendor dirs
skipped); tex reads ≤2 MiB. The only module-level mutable state is an
`lru_cache(256)` on OpenAlex work lookups (marked TODO(enterprise)).

## Failure modes

- OpenAlex/Crossref unreachable → search/insert raise
  `BuildError('citation_backend')` (handler: 稍后重试); audit marks rows
  `skipped` and still reports all local findings.
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

Verify with `tests/paper/test_paper_citations.py` (network fully mocked at
`lib.paper.citations.http_get`; mutations run against the real journaled
write tools on tmp roots).
