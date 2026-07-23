#!/usr/bin/env python3
"""Paper Reader: auto-persist grounded recommend cards to the bookshelf.

The describe-to-recommend feature used to be ephemeral — a recommendation
card only became a persisted bookshelf entry when the user CLICKED it. Close
the tab first and the whole list was lost. This suite drives the REAL shipped
``paper-reader.js`` under jsdom to verify the "directly persisted... otherwise
lost" fix:

  1. **auto-save on candidate** — a grounded card (non-null arxiv_id) is saved
     as a lightweight ``paper_library`` row the moment the 'candidate' event
     lands, via a PUT that ships the arxivId (empty pdf/parsedText = lightweight).
  2. **null-arxiv skip** — a card the engine left ``arxiv_id: null`` is NOT
     saved (it could neither be lazily ingested nor deduped → dead row).
  3. **whole-library dedup** — a card whose normalized arxiv id (vN suffix
     stripped) already exists (lightweight OR fully-read) creates NO second
     row and does not downgrade a read paper.
  4. **lazy-ingest in place** — clicking a saved lightweight entry reuses its
     row id through _fetchArxivPaper so the ingest upgrades the SAME row (no
     duplicate).

Plus an on-disk NEUTER proving the lazy-ingest-in-place branch is load-bearing:
force _createPaperEntry to ignore the explicitId reuse and the click mints a
duplicate row.

Skips cleanly when node/jsdom dev-deps are absent.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAPER_JS = os.path.join(ROOT, 'static', 'js', 'paper-reader.js')


def _node_deps_available():
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


# The harness evals the REAL paper-reader.js in global scope (indirect eval),
# stubs the network (Api.paper.libraryUpsert / fetchArxivStream) + DOM deps,
# then drives the recommend + click flows and reports observable state.
_HARNESS = r"""
const fs = require('fs'), path = require('path');
const ROOT = process.argv[2];
const NEUTER = process.argv[3] || '';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body>' +
  '<div id="paperLibraryList"></div>' +
  '<div id="paperPdfViewer"></div>' +
  '<div id="paperRecommendResults"></div>' +
  '</body>', { url: 'http://localhost/' });
global.window = dom.window;
global.document = dom.window.document;
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
global.Icon = () => '<svg></svg>';
global.t = (k) => k;
const _ls = {};
global.localStorage = {
  getItem: (k) => (k in _ls ? _ls[k] : null),
  setItem: (k, v) => { _ls[k] = String(v); },
  removeItem: (k) => { delete _ls[k]; },
};
try { Object.defineProperty(dom.window, 'localStorage', { value: global.localStorage, configurable: true }); } catch (e) {}
global.debugLog = () => {};

// Record every library upsert PUT the client fires.
const puts = [];
// Record every arXiv ingest the click path triggers.
const fetches = [];
global.Api = {
  paper: {
    libraryUpsert: (id, body) => { puts.push({ id, body }); return Promise.resolve({ ok: true }); },
    // Never resolve the stream — we only need to observe WHICH id the ingest
    // reused and that no duplicate row was minted synchronously.
    fetchArxivStream: (url, id) => { fetches.push({ url, id }); return new Promise(() => {}); },
  },
};

const src = fs.readFileSync(path.join(ROOT, 'static', 'js', 'paper-reader.js'), 'utf8');
(0, eval)(src);

// ── On-disk NEUTER: break the reuse branch of _createPaperEntry so the click
//    can no longer upgrade in place → proves that branch is load-bearing. We
//    wrap the real fn to strip the explicitId, forcing a fresh mint. ──
if (NEUTER === 'no_reuse') {
  const _orig = globalThis._createPaperEntry;
  globalThis._createPaperEntry = function(title, pdfUrl, parsedText, arxivId, explicitId) {
    return _orig(title, pdfUrl, parsedText, arxivId, undefined);  // drop reuse id
  };
}

const out = {};
globalThis._paperLibrary = [];
globalThis._activePaperId = '';

// A recommend-stream state object (shape _pollRecommendTask builds). One shared
// instance across the run, mirroring a single describe session.
const recState = { results: [], toolRounds: [], correction: null, cursor: 0 };
// Helper mirrors the engine 'candidate' path: _applyRecommendEvent(s, ev).
function candidate(card) { globalThis._applyRecommendEvent(recState, { type: 'candidate', card: card }); }

// 1. Grounded card → auto-saved (one lightweight row + one PUT carrying arxivId).
candidate({ arxiv_id: '2502.09992', title: 'Paper A', why: 'matches your query' });
out.after_first_count = globalThis._paperLibrary.length;
out.first_is_recommended = globalThis._isRecommendedEntry(globalThis._paperLibrary[0]);
out.first_put_has_arxiv = puts.length === 1 && puts[0].body.arxivId === '2502.09992'
                          && !puts[0].body.pdfUrl && !puts[0].body.parsedText;
out.active_not_stolen = globalThis._activePaperId === '';   // background save must not steal focus

// 2. null arxiv_id → skipped (no new row, no new PUT).
const putsBefore = puts.length, rowsBefore = globalThis._paperLibrary.length;
candidate({ arxiv_id: null, title: 'Ungrounded paper', why: 'no id' });
out.null_skipped = (globalThis._paperLibrary.length === rowsBefore) && (puts.length === putsBefore);

// 3a. Dedup vs a versioned id of the SAME paper → no second row.
candidate({ arxiv_id: '2502.09992v3', title: 'Paper A (v3 dup)', why: 'dup' });
out.dedup_no_new_row = (globalThis._paperLibrary.length === 1);

// 3b. Dedup must NOT downgrade a fully-read paper. Seed a read paper, then a
//     recommend card for the same id → row stays read (has pdfUrl/parsedText).
globalThis._paperLibrary.unshift({
  id: 'read_1', title: 'Read Paper B', arxivId: '2601.00001',
  pdfUrl: '/uploads/papers/b.pdf', pdfFilename: 'b.pdf', parsedText: 'body text',
  qaHistory: [], paperHash: 'deadbeef', images: [], babelCache: {}, createdAt: 1, pageCount: 12,
});
const rowsBefore2 = globalThis._paperLibrary.length;
candidate({ arxiv_id: '2601.00001', title: 'B recommend dup', why: 'dup of read' });
out.read_not_duplicated = (globalThis._paperLibrary.length === rowsBefore2);
const readRow = globalThis._paperLibrary.find(p => p.id === 'read_1');
out.read_not_downgraded = !!(readRow && readRow.pdfUrl && readRow.parsedText);

// 4. Lazy-ingest in place: click the saved lightweight Paper A row → ingest
//    reuses its id, no duplicate row minted.
const litRow = globalThis._paperLibrary.find(p => globalThis._isRecommendedEntry(p) && p.arxivId === '2502.09992');
out.lit_row_id = litRow ? litRow.id : null;
const countBeforeClick = globalThis._paperLibrary.length;
globalThis._onPaperLibClick(litRow.id);
out.fetch_reused_id = fetches.length === 1 && fetches[0].id === litRow.id;
out.no_dup_after_click = (globalThis._paperLibrary.length === countBeforeClick);

// The click hands litRow.id to _fetchArxivPaper, which (at the ingest 'done'
// stage) calls _createPaperEntry(..., litRow.id) to UPGRADE that row in place.
// Our stubbed stream never resolves, so drive that final step directly: does
// _createPaperEntry with the reuse id upgrade the SAME row, or mint a new one?
const countBeforeUpgrade = globalThis._paperLibrary.length;
const upgraded = globalThis._createPaperEntry(
  'Paper A (ingested)', '/uploads/papers/a.pdf', 'full body text', '2502.09992', litRow.id);
out.upgrade_same_row_id = (upgraded && upgraded.id === litRow.id);
out.upgrade_no_new_row = (globalThis._paperLibrary.length === countBeforeUpgrade);
out.upgrade_filled_pdf = !!(upgraded && upgraded.pdfUrl && upgraded.parsedText);
out.upgrade_no_longer_recommended = !(upgraded && globalThis._isRecommendedEntry(upgraded));

console.log(JSON.stringify(out));
"""


def _run(neuter=''):
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, dir=ROOT) as f:
        harness = f.name
        f.write(_HARNESS)
    try:
        proc = subprocess.run(['node', harness, ROOT, neuter],
                              capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    if proc.returncode != 0:
        raise AssertionError(f'harness failed (rc={proc.returncode}):\n{proc.stderr}\n{proc.stdout}')
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_grounded_card_auto_saves():
    out = _run()
    assert out['after_first_count'] == 1, 'grounded card should create exactly one bookshelf row'
    assert out['first_is_recommended'], 'saved card must be a lightweight recommended entry'
    assert out['first_put_has_arxiv'], 'the PUT must ship arxivId with empty pdf/parsedText'
    assert out['active_not_stolen'], 'a background save must not steal the active-paper pointer'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_null_arxiv_card_is_skipped():
    out = _run()
    assert out['null_skipped'], 'a card with null arxiv_id must NOT be persisted'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_dedup_whole_library():
    out = _run()
    assert out['dedup_no_new_row'], 'a versioned dup of an existing id must not create a second row'
    assert out['read_not_duplicated'], 'a recommend card for an already-read paper must not add a row'
    assert out['read_not_downgraded'], 'a read paper must never be downgraded to recommended'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_lazy_ingest_in_place_no_duplicate():
    out = _run()
    assert out['lit_row_id'], 'expected a lightweight Paper A row to click'
    assert out['fetch_reused_id'], 'click must lazily ingest reusing the saved row id'
    # The ingest 'done' step upgrades the SAME row rather than minting a duplicate.
    assert out['upgrade_same_row_id'], 'ingest must upgrade the reused row (same id)'
    assert out['upgrade_no_new_row'], 'ingest-in-place must not add a second row'
    assert out['upgrade_filled_pdf'], 'ingest must fill pdfUrl/parsedText into the row'
    assert out['upgrade_no_longer_recommended'], \
        'the upgraded row must flip from recommended to a normal paper'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_no_reuse_mints_duplicate():
    """Break _createPaperEntry's explicitId reuse → ingesting a saved
    recommendation forks a NEW row instead of upgrading in place. This proves
    the in-place-reuse branch is load-bearing."""
    base = _run()
    neutered = _run(neuter='no_reuse')
    # Baseline: the reuse branch keeps a single row and flips it to a real paper.
    assert base['upgrade_no_new_row'] and base['upgrade_same_row_id'], \
        'baseline must upgrade in place'
    # Neutered: _createPaperEntry ignores explicitId → a fresh row is minted, so
    # the count grows and the upgraded object is NOT the original lightweight row.
    assert not neutered['upgrade_no_new_row'], \
        'with reuse neutered, ingest MUST mint a duplicate row (proves branch is load-bearing)'
    assert not neutered['upgrade_same_row_id'], \
        'with reuse neutered, the ingested row must have a different id'


def _color(s, c):
    return f'\033[{c}m{s}\033[0m'


def main():
    print()
    print(_color('═══ Paper Recommend-Persist Tests ═══', '36'))
    if not _node_deps_available():
        print(' ', _color('•', '33'), 'jsdom tests skipped (node/jsdom not installed)')
        return
    for fn in (test_grounded_card_auto_saves, test_null_arxiv_card_is_skipped,
               test_dedup_whole_library, test_lazy_ingest_in_place_no_duplicate,
               test_NEUTER_no_reuse_mints_duplicate):
        fn()
        print(' ', _color('✓', '32'), fn.__name__)
    print()
    print(_color('═══ ALL TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
