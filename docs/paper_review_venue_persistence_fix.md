# Paper Review #3 — venue-persistence fix (re-appliable fold-in spec)

**Status:** NOT shipped at HEAD. Tracked by board epic `pt_4daa2c3d3eaf460f`.
**Owner-of-record:** whoever lands the Epic E `static/js/paper/report.js` cut.

## Why this file exists

The venue-persistence fix (the "already generated but re-prompts Generate" bug)
currently lives ONLY in an **untracked** `static/js/paper/report.js` — a file the
in-flight Epic E `paper-reader.js` decomposition owns and is actively
regenerating while paused mid-cut. If Epic E re-cuts or overwrites that untracked
file before it lands, the fix vanishes **silently — no diff, no commit, nothing to
recover from**.

This document is the durable, recoverable copy. It is a **re-appliable hunk set**
anchored on surrounding context (not line numbers), so it can be folded onto Epic
E's landed `report.js` regardless of how that file was regenerated. Do **NOT**
treat "keep the untracked report.js byte-stable" as the recovery plan — treat
THIS spec as the source of truth for the fix.

Do **NOT** port these hunks into the half-gutted monolithic `paper-reader.js`
(violates the no-sibling-edit rule and creates a third divergent copy). They land
onto `report.js` when — and only when — Epic E's decomposition puts the
Report/Review venue helpers there.

## The bug (root cause)

The review cache key is `review:<venue>:<uilang>`, where the venue comes from
`_paperReviewVenue || 'generic'`. A generation entry that isn't the
venue-race-guarded `_switchPaperTab` (e.g. the Generate button) could fire while
`_paperReviewVenue === ''` → generate/persist under `review:generic:en`. On
reload, `_resolveReviewVenue()` picks persisted-per-paper → registry-first
(`neurips`), because the silent auto-default is never persisted. Lookup key
`review:neurips:en` ≠ stored `review:generic:en` → cache miss → the finished
review re-prompts "Generate".

## The fix — five hunks

All five go into `static/js/paper/report.js` (post-Epic-E home of the
Report/Review render/poll/generate stack + the venue helpers).

### Hunk 1 — helper definition (NEW function)

Add this function beside the other review-venue helpers (next to
`_ensureReviewVenues` / `_persistReviewVenue`):

```js
/** Persist the venue a review was ACTUALLY generated/cached under, derived from
 *  the composite langKey (``review:<venue>:<uilang>``) that keyed the DB row.
 *  Called on every terminal-success path (done / cached / cache-hit) so a
 *  reload resolves the SAME venue and its lookup key matches the stored row —
 *  the fix for "already generated but re-prompts Generate". Unlike
 *  _persistReviewVenue-on-explicit-click, this persists the venue that was
 *  effectively used even when it came from the silent registry-first default,
 *  because that default is what the review is now stored under. Report views
 *  and malformed keys are ignored. */
function _persistGeneratedReviewVenue(view, langKey, paperId) {
  if (!view || view.kind !== 'review') return;
  paperId = paperId || _activePaperId;
  if (!paperId) return;
  var parts = String(langKey || '').split(':');
  if (parts[0] !== 'review' || !parts[1]) return;
  _persistReviewVenue(paperId, parts[1]);
}
```

### Hunk 2 — resolve venue BEFORE building the cache key (in `_generatePaperReport`)

Immediately after `var startPaperId = _activePaperId;` and BEFORE
`var langKey = view.langKey();`, insert the resolve-then-generate guard. If
`langKey` is currently computed before `startPaperId`, MOVE the `startPaperId`
capture above it too (the guard must run before `langKey` is read).

```js
  // Review generation MUST resolve the venue BEFORE building the composite
  // cache key. An entry that reaches here with _paperReviewVenue==='' (e.g. the
  // Generate button, which — unlike the venue-race-guarded _switchPaperTab —
  // does NOT pre-resolve) would build langKey off the `|| 'generic'` fallback
  // and generate/persist under review:generic:… while a later reload resolves
  // the real venue → cache-key skew → the finished review re-prompts Generate.
  // Mirror the tab-switch resolve-then-generate guard so every generation entry
  // agrees with reload.
  if (view.kind === 'review') {
    try { await _resolveReviewVenue(); } catch (e) {
      console.warn('[Paper:Review] venue resolve before generate failed:', e);
    }
    if (_activePaperId !== startPaperId) return;
  }

  var langKey = view.langKey();
```

### Hunk 3 — persist on the `done` STREAM EVENT path (in `_applyReportEvent`, `case 'done'`)

Inside `case 'done':`, in the `if (ev.report) { … if (s.paperId === _activePaperId) { … } }`
block, right after `_rememberReportSnapshot(_vDone, ev.report, ev.meta || s.meta);`:

```js
          _persistGeneratedReviewVenue(_vDone, _vDone.langKey(), s.paperId);
```

> NOTE: use the view's composite `langKey()` — NOT the stream's `s.lang`, which
> is the UI lang (`'en'`), not the `review:<venue>:<uilang>` composite.

### Hunk 4 — persist on the POLL `done` path (in `_pollReportTask`)

In the poll success block, inside `if (s.paperId === _activePaperId) { … }`, right
after `_rememberReportSnapshot(view, data.report, data.meta);`:

```js
          _persistGeneratedReviewVenue(view, view.langKey(), s.paperId);
```

### Hunk 5 — persist on the TWO cache-hit paths (in `_generatePaperReport` and `_loadOrGenerateReport`)

Both cache-hit sites use the local `langKey` + `startPaperId`.

(a) In `_generatePaperReport`, the `/start` DB-cache-hit branch
`if (data.cached && data.report) { … }`, after
`_rememberReportSnapshot(view, data.report, data.meta);`:

```js
      _persistGeneratedReviewVenue(view, langKey, startPaperId);
```

(b) In `_loadOrGenerateReport`, the lookup-cache reconnect branch
`if (cacheData && cacheData.ok && cacheData.report) { … }`, after
`_rememberReportSnapshot(view, cacheData.report, cacheData.meta);`:

```js
      _persistGeneratedReviewVenue(view, langKey, startPaperId);
```

## Landing acceptance (hard preconditions — see epic pt_4daa2c3d3eaf460f)

1. **Same-commit bundler wiring (CLAUDE.md §3.2.1):** `report.js` MUST land in the
   SAME commit as its `lib/js_bundler.py` `_DEFERRED_FILES` entry
   (`'paper/report.js'`). Without it the whole Report/Review tab — and this fix —
   is a silent no-op (no 404, no console error). The working-tree `js_bundler.py`
   already carries this entry (staged with Epic E's cut).
2. **Fresh-worktree gate:** once `report.js` + its bundler entry are BOTH at HEAD,
   in a fresh `git worktree` at that HEAD with a provisioned temp-SQLite DB, run
   `tests/test_frontend_review_venue_persist.py` and confirm GREEN there (positive
   + source-level NC) — not just in a dirty working tree.
3. **No double-define:** confirm the monolithic `paper-reader.js` no longer defines
   the venue helpers (they moved to `report.js`).

## Regression test

`tests/test_frontend_review_venue_persist.py` (jsdom) is the biting test:
- positive: a review generated under venue `iclr` (silently resolved, not an
  explicit dropdown click) reloads under key `review:iclr:en` (cache HIT, NO
  Generate prompt); the persisted per-paper venue is `iclr`.
- source-level NC: neuter `_persistGeneratedReviewVenue` to a no-op → reload
  re-resolves registry-first `neurips` → key miss → Generate prompt reappears.

That test file is already tracked/committed intent (green in the working tree); it
evals `report.js` + `paper-reader.js` and will pass once the fix is wired at HEAD.


---

# Paper Reader — `_switchPaperTab` recovery-guard fix (re-appliable fold-in spec)

**Status:** applied in the working-tree `static/js/paper-reader.js` but UNCOMMITTED
— the file is owned + actively regenerated by the paused Epic E decomposition, so
this fix carries the SAME silent-evaporation risk as the venue fix above. This
section is its durable, re-appliable copy.

## The bug

On restart, opening a paper from the library shows **"No paper text available.
Load a PDF first."** even when its PDF is on disk. `_switchPaperTab` gated the
load path on `if (_paperParsedText || _paperHash)`. A library entry restored
after a restart can have empty `parsedText` AND empty `paperHash` (saved before
server-side parsing, or a scanned/failed parse) while its PDF is intact under
`PAPER_DIR`. That guard sent those papers straight to the dead-end message and
NEVER called `_loadOrGenerateReport` → never reached `_ensurePaperText()` (POST
`/api/paper/reparse`), the documented recovery. So recovery was unreachable from
a fresh tab open.

> NOTE: this is the *frontend routing* half. The deeper root cause of the ghosts
> themselves — truncated/aborted uploads landing 15-byte `%PDF-1.4` stubs — is
> fixed on the BACKEND and IS committed/shipped (see the "Backend companion"
> section below). This guard-widening only ensures a *recoverable* paper reaches
> the recovery path; it does not (and cannot) recover a stub.

## The fix — one hunk in `_switchPaperTab` (`static/js/paper-reader.js`)

Widen the guard so the flow proceeds whenever ANY recoverable source exists —
parsed text, a server hash, OR a PDF on disk — and only show the message when
there is truly no PDF:

```js
    // Enter the load path whenever ANY recoverable source exists: parsed text,
    // a server hash, OR a PDF still on disk. A library entry restored after a
    // restart can have empty parsedText + empty hash (saved before server-side
    // parsing, or a scanned/failed parse) while its PDF is intact under
    // PAPER_DIR — in that case _loadOrGenerateReport -> _generatePaperReport
    // runs _ensurePaperText() (POST /api/paper/reparse) to recover the text.
    // Gating on text/hash alone dead-ended those papers on the "load a PDF"
    // message and never reached that recovery. Only show the message when
    // there is truly no PDF to recover from.
    if (_paperParsedText || _paperHash || _paperPdfUrl || _paperPdfFilename) {
      // ... existing review venue-resolve branch + else _loadOrGenerateReport ...
    } else {
      // ... existing "No paper text available" message ...
    }
```

The ONLY change is the guard condition: from
`if (_paperParsedText || _paperHash)` to
`if (_paperParsedText || _paperHash || _paperPdfUrl || _paperPdfFilename)`. The
inner review/report branches are unchanged. If Epic E's cut moved
`_switchPaperTab` into a new file, apply the same guard-widening wherever that
function lands.

## Regression test

`tests/test_frontend_paper_switchtab_recovery.py` (jsdom) — TRACKED/committed:
- positive: a PDF-only paper (empty `parsedText` + empty `paperHash`, but
  `_paperPdfUrl`/`_paperPdfFilename` set) enters `_loadOrGenerateReport` for both
  Report and Review and does NOT paint the dead-end message; a paper with NO PDF
  still shows the message; a parsed-text paper is unchanged.
- source-level NC: revert the guard to `_paperParsedText || _paperHash` → the
  PDF-only paper dead-ends again.

## Backend companion (COMMITTED / shipped — the real root cause)

The stubs themselves are prevented at ingest. `lib.pdf_parser.validate_pdf_bytes`
(a PDF is real only if pymupdf opens it AND it has >= 1 page) gates the three
write paths in `routes/paper.py`: `/api/paper/upload`, `/api/paper/fetch-arxiv`,
`/api/paper/fetch-arxiv-stream`. An invalid/truncated upload is deleted and
rejected with a real error — **no library row is ever seeded**. `_is_ghost_library_row`
also treats a present-but-unopenable small PDF (< `_GHOST_PDF_MAX_STUB_BYTES`) as
a ghost, so the pre-existing 15-byte stubs are skipped from listings
(non-destructively). Regression: `tests/test_paper_pdf_validation.py` (8 tests,
real `parse_pdf` + gate + source-level neuter). This half is NOT gated on Epic E.
