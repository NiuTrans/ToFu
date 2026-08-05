
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
// Cross-file UI helper: _fetchArxivPaper (paper/arxiv.js) paints ingest
// progress via _renderArxivFetchProgress, which stays in the residual
// paper-reader.js core (not evaled here). It only draws progress chrome —
// irrelevant to the persistence/dedup assertions — so stub it as a no-op.
global._renderArxivFetchProgress = () => {};

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

// The recommend + library functions were split out of paper-reader.js into
// paper/arxiv.js (_applyRecommendEvent, _fetchArxivPaper) and paper/library.js
// (_persistRecommendedCard, _createPaperEntry, _isRecommendedEntry,
// _onPaperLibClick). Eval both in bundle order (arxiv before library); the
// cross-file references resolve at call time in shared global scope.
(0, eval)(fs.readFileSync(path.join(ROOT, 'static', 'js', 'paper', 'arxiv.js'), 'utf8'));
(0, eval)(fs.readFileSync(path.join(ROOT, 'static', 'js', 'paper', 'library.js'), 'utf8'));

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
