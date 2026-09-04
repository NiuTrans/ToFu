"""Compiled Report/Review owner: start, replay, stop and stale-work fences."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_section_path
from tests._paper_vite import compiled_typescript


pytestmark = pytest.mark.unit
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
SOURCE = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'paper', 'report-runtime.ts')
RETAINED_RENDERER = Path(runtime_section_path(
    'paper/report.js', scope_prelude=False))
RETAINED_READER = Path(runtime_section_path(
    'paper-reader.js', scope_prelude=False))
INDEX_HTML = Path(ROOT) / 'index.html'
ESBUILD = os.path.join(ROOT, 'scripts', 'vite_test_bundle.mjs')


_HARNESS = r"""
const fs = require('fs'), path = require('path');
const ROOT = process.argv[1], BUILT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body>' +
  '<button id="reportStop"><span>Stop</span></button>' +
  '<button id="reviewStop"><span>Stop</span></button></body>', { url:'http://localhost/' });
global.window = global;
global.document = dom.window.document;
global.localStorage = dom.window.localStorage;
global.HTMLButtonElement = dom.window.HTMLButtonElement;
global._activePaperId = 'paper-a';
global._REPORT_REGEN_INTENT_KEY = 'regen-report';
global._REPORT_POLL_MS = 999999;
global._REPORT_POLL_BACKOFF_MS = 999999;
global._REPORT_ABORT_GRACE_MS = 1;
global.t = (key) => key;
const calls = { paints:0, detach:0, saves:0, snapshots:0, aborts:[] };
const views = {
  report:{ kind:'report', stopBtnId:'reportStop', regenIntentKey:'regen-report',
    cache:'', meta:null, stream:null, langKey:()=> 'en' },
  review:{ kind:'review', stopBtnId:'reviewStop', regenIntentKey:'regen-review',
    cache:'', meta:null, stream:null, langKey:()=> 'review:generic:en' },
  rebuttal:{ kind:'rebuttal', stopBtnId:'rebuttalStop', regenIntentKey:'regen-rebuttal',
    cache:'', meta:null, stream:null, langKey:()=> 'rebuttal:generic:en' },
};
global._reportView = (kind) => views[kind || 'report'];
global._paintReportFromState = () => { calls.paints++; };
global._detachReportPush = () => { calls.detach++; };
global._rememberReportSnapshot = () => { calls.snapshots++; };
global._persistGeneratedReviewVenue = () => {};
global._saveActivePaperState = () => { calls.saves++; };
global._applyResolvedTitle = () => {};
global._teardownReadingTracker = () => {};
global._resetReportSnapshots = () => {};
global._applyReportEventRaw = (state, event) => {
  if (event.type === 'delta') { state.fullText += event.delta; state.contentStarted = true; }
  return true;
};
global.taskReplayIngestPage = (state, page, apply, cursor) => {
  for (const event of page.events || []) apply(state, event);
  return { nextCursor:page.next_cursor ?? cursor };
};
let responseData = { ok:true, status:'running', events:[], next_cursor:0 };
let deferredResolve = null;
global.Api = { paper:{
  reportPoll: async () => ({ ok:true, status:200, json:async () => {
    if (deferredResolve !== null) return new Promise((resolve) => { deferredResolve = resolve; });
    return responseData;
  }}),
  reportAbort: async (taskId) => { calls.aborts.push(taskId); },
}};

(0, eval)(fs.readFileSync(BUILT, 'utf8'));
const ReportRuntime = global.TofuTestBundle;

(async () => {
  ReportRuntime.setReportRegenIntent('hash-a', 'en', 'regen-report');
  const intentRoundTrip = ReportRuntime.hasReportRegenIntent(
    'hash-a', 'en', 'regen-report');

  const live = ReportRuntime.makeReportStreamState(
    'paper-a', 'en', 'task-1', 'report');
  views.report.stream = live;
  responseData = { ok:true, status:'done', next_cursor:2,
    events:[{type:'delta', delta:'draft'}], report:'final report',
    meta:{model:'m1'} };
  await ReportRuntime.pollReportTask(views.report);
  const done = { status:live.status, cursor:live.cursor, text:live.fullText,
    cache:views.report.cache, timer:live.pollTimer };

  // Replace the active stream while response.json is pending.
  const stale = ReportRuntime.makeReportStreamState(
    'paper-a', 'en', 'task-stale', 'report');
  views.report.stream = stale;
  deferredResolve = () => {};
  const pendingPoll = ReportRuntime.pollReportTask(views.report);
  await new Promise((resolve) => setTimeout(resolve, 0));
  const replacement = ReportRuntime.makeReportStreamState(
    'paper-a', 'en', 'task-new', 'report');
  views.report.stream = replacement;
  deferredResolve({ ok:true, status:'done', next_cursor:9, events:[], report:'stale' });
  await pendingPoll;
  deferredResolve = null;
  const staleFenced = views.report.stream === replacement && views.report.cache === 'final report';

  const provisional = ReportRuntime.makeReportStreamState(
    'paper-a', 'en', '', 'review');
  views.review.stream = provisional;
  ReportRuntime.stopPaperReview();
  const pendingStop = provisional.pendingStop && provisional.stopRequested;

  const stoppable = ReportRuntime.makeReportStreamState(
    'paper-a', 'en', 'task-stop', 'review');
  views.review.stream = stoppable;
  ReportRuntime.stopPaperReview();
  await new Promise((resolve) => setTimeout(resolve, 5));
  const forcedAbort = stoppable.status === 'aborted' && !stoppable.pollTimer;

  views.report.stream = replacement;
  views.review.stream = stoppable;
  ReportRuntime.destroyReportRuntime();
  console.log(JSON.stringify({ intentRoundTrip, done, staleFenced, pendingStop,
    forcedAbort, aborts:calls.aborts, destroyed:Object.values(views).every(v => v.stream === null),
    leakedGlobal:typeof global._setReportRegenIntent === 'function' }));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""


_START_HARNESS = r"""
const fs = require('fs'), path = require('path');
const ROOT = process.argv[1], BUILT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body>' +
  '<div id="reportContent"></div><div id="reviewContent"></div>' +
  '<div id="rebuttalContent"></div>' +
  '<button id="reportStop"><span>Stop</span></button>' +
  '<button id="reviewStop"><span>Stop</span></button>' +
  '<button id="rebuttalStop"><span>Stop</span></button></body>',
  { url:'http://localhost/' });
global.window = global;
global.document = dom.window.document;
global.localStorage = dom.window.localStorage;
global.HTMLElement = dom.window.HTMLElement;
global.HTMLButtonElement = dom.window.HTMLButtonElement;
global._activePaperId = 'paper-a';
global._paperParsedText = 'p'.repeat(120000);
global._paperHash = 'a'.repeat(32);
global._paperFileName = 'Paper A';
global._paperPdfFilename = 'paper-a.pdf';
global._REPORT_POLL_MS = 999999;
global._REPORT_POLL_BACKOFF_MS = 999999;
global._REPORT_ABORT_GRACE_MS = 1;
global.t = (key) => key;

function makeView(kind, containerId, langKey) {
  return {
    kind, containerId, stopBtnId:kind + 'Stop',
    regenIntentKey:'regen-' + kind, cache:'', meta:null, stream:null,
    model:'model-a', uiLang:() => 'en', langKey:() => langKey,
  };
}
const views = {
  report:makeView('report', 'reportContent', 'en'),
  review:makeView('review', 'reviewContent', 'review:venue:en'),
  rebuttal:makeView('rebuttal', 'rebuttalContent', 'rebuttal:venue:en'),
};
global._reportView = (kind) => views[kind || 'report'];

const calls = {
  starts:[], aborts:[], renders:[], saves:[], titles:[], toasts:[],
  attaches:0, detaches:0,
};
global.Api = { paper:{
  reportStart:(body) => new Promise((resolve, reject) => {
    calls.starts.push({ body, resolve, reject });
  }),
  // An unresolved poll has no active handle. It lets the start owner attach a
  // real stream without introducing timers or repaint noise into this harness.
  reportPoll:() => new Promise(() => {}),
  reportAbort:async (taskId) => { calls.aborts.push(taskId); return { ok:true }; },
}};
global._renderReportSkeleton = (container) => { container.textContent = 'SKELETON'; };
global._syncReportToolbar = () => {};
global._attachReportPush = () => { calls.attaches++; };
global._detachReportPush = () => { calls.detaches++; };
global._paintReportFromState = () => {};
global._populatePaperReportModelDropdown = () => {};
global._getActivePaperEntry = () => ({ title:'Paper A.pdf' });
global._saveActivePaperState = (mode) => { calls.saves.push(mode); };
global._persistGeneratedReviewVenue = () => {};
global._applyResolvedTitle = (title) => { calls.titles.push(title); };
global._renderFinalReport = (container, report) => {
  calls.renders.push(report);
  container.textContent = report;
};
global._teardownReadingTracker = () => {};
global._resetReportSnapshots = () => {};
global.showToast = (message) => { calls.toasts.push(message); };

(0, eval)(fs.readFileSync(BUILT, 'utf8'));
const ReportRuntime = global.TofuTestBundle;
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

(async () => {
  const report = views.report;

  // Older cached success must not replace a newer force-started task for the
  // same paper and view kind.
  const staleCachedPromise = ReportRuntime.generatePaperReport(false, report);
  await Promise.resolve();
  const staleCachedCall = calls.starts.at(-1);
  const hashOnlyStart = staleCachedCall.body.paper_hash === 'a'.repeat(32)
    && !Object.hasOwn(staleCachedCall.body, 'paper_text');
  const hashOnlyWireBounded = JSON.stringify(staleCachedCall.body).length < 512
    && JSON.stringify({
      ...staleCachedCall.body, paper_text:global._paperParsedText,
    }).length - JSON.stringify(staleCachedCall.body).length >= 120000;
  const freshPromise = ReportRuntime.generatePaperReport(true, report);
  await Promise.resolve();
  const freshCall = calls.starts.at(-1);
  freshCall.resolve({ ok:true, task_id:'task-fresh', paper_hash:'b'.repeat(32) });
  await freshPromise;
  staleCachedCall.resolve({
    ok:true, cached:true, report:'STALE-CACHED', meta:{ model:'old' },
  });
  await staleCachedPromise;
  const staleCachedFenced = report.stream?.taskId === 'task-fresh'
    && report.cache !== 'STALE-CACHED'
    && document.getElementById('reportContent').textContent !== 'STALE-CACHED';

  // The same fence must suppress an older rejection; otherwise it erases the
  // new stream and repaints a terminal error over it.
  ReportRuntime.resetReportLocalState(report);
  report.cache = '';
  const staleErrorPromise = ReportRuntime.generatePaperReport(true, report);
  await Promise.resolve();
  const staleErrorCall = calls.starts.at(-1);
  const newestPromise = ReportRuntime.generatePaperReport(true, report);
  await Promise.resolve();
  const newestCall = calls.starts.at(-1);
  newestCall.resolve({ ok:true, task_id:'task-newest', paper_hash:'c'.repeat(32) });
  await newestPromise;
  const startsBeforeStaleFallback = calls.starts.length;
  staleErrorCall.reject({
    status:400, code:'paper_source_required', message:'STALE-ERROR',
  });
  await tick();
  const staleFallbackSuppressed = calls.starts.length === startsBeforeStaleFallback;
  // Let the negative-control bundle settle: without the fence it dispatches
  // an unnecessary text fallback, which would otherwise remain unresolved and
  // let Node exit before this harness can print its decisive failure.
  if (!staleFallbackSuppressed) {
    calls.starts.at(-1).resolve({
      ok:true, task_id:'task-stale-fallback', paper_hash:'9'.repeat(32),
    });
  }
  await staleErrorPromise;
  const staleErrorFenced = report.stream?.taskId === 'task-newest'
    && !document.getElementById('reportContent').textContent.includes('STALE-ERROR');

  // Stop during the slow /start round-trip is carried from the provisional
  // task to the authoritative task id as soon as it arrives.
  ReportRuntime.resetReportLocalState(report);
  report.cache = '';
  const stopPromise = ReportRuntime.generatePaperReport(true, report);
  await Promise.resolve();
  const stopCall = calls.starts.at(-1);
  const provisionalStop = report.stream?.taskId === '';
  ReportRuntime.stopPaperReport(report);
  const stopWasPending = report.stream?.pendingStop === true;
  stopCall.resolve({ ok:true, task_id:'task-stop', paper_hash:'d'.repeat(32) });
  await stopPromise;
  const pendingStopHonored = calls.aborts.includes('task-stop')
    && report.stream?.taskId === 'task-stop'
    && report.stream?.stopRequested === true;

  // A cache hit from /start follows the same canonical apply path as reopen:
  // no zombie provisional stream, and only lightweight metadata persistence.
  ReportRuntime.resetReportLocalState(report);
  report.cache = '';
  const cachedPromise = ReportRuntime.generatePaperReport(true, report);
  await Promise.resolve();
  const cachedCall = calls.starts.at(-1);
  const saveStart = calls.saves.length;
  cachedCall.resolve({
    ok:true, cached:true, report:'CANONICAL-CACHE', meta:{ model:'cache-model' },
    paper_hash:'e'.repeat(32), resolvedTitle:'Resolved Paper', lang:'en',
  });
  await cachedPromise;
  const cachedApplied = report.stream === null
    && report.cache === 'CANONICAL-CACHE'
    && calls.renders.at(-1) === 'CANONICAL-CACHE'
    && calls.titles.at(-1) === 'Resolved Paper'
    && calls.saves.slice(saveStart).join(',') === 'metadata';

  // A hash-only source miss may retry exactly once with text because the
  // explicit 400 proves the server dispatched no paid task. Ambiguous errors
  // are never retried (covered by the stale fence above).
  ReportRuntime.resetReportLocalState(report);
  report.cache = '';
  const fallbackStartIndex = calls.starts.length;
  const fallbackPromise = ReportRuntime.generatePaperReport(true, report);
  await Promise.resolve();
  const hashOnlyCall = calls.starts.at(-1);
  hashOnlyCall.reject({
    status:400, code:'paper_source_required',
    message:'Stored paper text unavailable',
  });
  await tick();
  const textFallbackCall = calls.starts.at(-1);
  textFallbackCall.resolve({
    ok:true, task_id:'task-source-fallback', paper_hash:'f'.repeat(32),
  });
  await fallbackPromise;
  const sourceFallbackBounded = calls.starts.length === fallbackStartIndex + 2
    && !Object.hasOwn(hashOnlyCall.body, 'paper_text')
    && textFallbackCall.body.paper_text.length === 120000
    && textFallbackCall.body.paper_hash === hashOnlyCall.body.paper_hash;

  // A malformed legacy hash is not offered as content authority: send the
  // available text immediately instead of paying a predictable 400 round-trip.
  ReportRuntime.resetReportLocalState(report);
  report.cache = '';
  global._paperHash = 'legacy-invalid-hash';
  const invalidHashPromise = ReportRuntime.generatePaperReport(true, report);
  await Promise.resolve();
  const invalidHashCall = calls.starts.at(-1);
  invalidHashCall.resolve({
    ok:true, task_id:'task-invalid-hash', paper_hash:'2'.repeat(32),
  });
  await invalidHashPromise;
  const invalidHashUsesTextImmediately =
    !Object.hasOwn(invalidHashCall.body, 'paper_hash')
    && invalidHashCall.body.paper_text.length === 120000;

  // Persisted rebuttal drafts are bounded by both entry count and the same
  // 40k character ceiling enforced by the backend prompt builder.
  localStorage.removeItem('paper_rebuttal_text_by_id');
  for (let index = 0; index < 35; index++) {
    global._activePaperId = 'paper-' + index;
    ReportRuntime.onRebuttalInputChange(
      index === 34 ? 'x'.repeat(40050) : 'draft-' + index,
    );
  }
  const draftMap = JSON.parse(localStorage.getItem('paper_rebuttal_text_by_id'));
  global._activePaperId = 'paper-34';
  const restoredDraft = ReportRuntime.restorePaperRebuttalInputText();
  const draftsBounded = Object.keys(draftMap).length === 32
    && !('paper-0' in draftMap) && !('paper-2' in draftMap)
    && draftMap['paper-34'].length === 40000
    && restoredDraft.length === 40000;

  const startsBeforeBlank = calls.starts.length;
  ReportRuntime.onRebuttalInputChange('   ');
  await ReportRuntime.generatePaperRebuttal();
  const blankRebuttalGuarded = calls.starts.length === startsBeforeBlank
    && calls.toasts.at(-1) === 'paper.rebuttalNeedText';

  ReportRuntime.onRebuttalInputChange('y'.repeat(40050));
  views.review.cache = '';
  views.review.stream = null;
  const startsBeforeReviewGuard = calls.starts.length;
  await ReportRuntime.generatePaperRebuttal();
  const missingReviewGuarded = calls.starts.length === startsBeforeReviewGuard
    && calls.toasts.at(-1) === 'paper.rebuttalNeedReview';

  views.review.cache = 'ORIGINAL-REVIEW';
  const rebuttalPromise = ReportRuntime.generatePaperRebuttal(true);
  await Promise.resolve();
  const rebuttalCall = calls.starts.at(-1);
  rebuttalCall.resolve({
    ok:true, task_id:'task-rebuttal', paper_hash:'1'.repeat(32),
  });
  await rebuttalPromise;
  const rebuttalPayloadBounded = rebuttalCall.body.author_rebuttal.length === 40000
    && rebuttalCall.body.lang === 'rebuttal:venue:en';

  await tick();
  console.log(JSON.stringify({
    staleCachedFenced, staleErrorFenced, staleFallbackSuppressed, hashOnlyStart,
    hashOnlyWireBounded, provisionalStop, stopWasPending, pendingStopHonored, cachedApplied,
    sourceFallbackBounded, invalidHashUsesTextImmediately, draftsBounded, blankRebuttalGuarded,
    missingReviewGuarded, rebuttalPayloadBounded,
  }));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""


def _run_start_harness(runtime_contents: str | None = None) -> dict[str, bool]:
    with compiled_typescript(SOURCE, contents=runtime_contents) as runtime_js:
        process = subprocess.run(
            ['node', '-e', _START_HARNESS, ROOT, runtime_js],
            capture_output=True, text=True, timeout=60,
        )
    assert process.returncode == 0, process.stderr + process.stdout
    return json.loads(process.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(
    not shutil.which('node') or not os.path.isfile(ESBUILD)
    or not os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom')),
    reason='node/jsdom/esbuild dev dependencies required',
)
def test_compiled_report_runtime_owns_poll_stop_and_destroy(tmp_path):
    built = tmp_path / 'report-runtime.js'
    compiled = subprocess.run(
        [ESBUILD, SOURCE, '--bundle', '--format=iife', '--platform=browser',
         f'--outfile={built}'], capture_output=True, text=True, timeout=60)
    assert compiled.returncode == 0, compiled.stderr
    process = subprocess.run(
        ['node', '-e', _HARNESS, ROOT, str(built)], capture_output=True,
        text=True, timeout=60)
    assert process.returncode == 0, process.stderr + process.stdout
    result = json.loads(process.stdout.strip().splitlines()[-1])
    assert result['intentRoundTrip']
    assert result['done'] == {
        'status': 'done', 'cursor': 2, 'text': 'final report',
        'cache': 'final report', 'timer': None,
    }
    assert result['staleFenced'] and result['pendingStop']
    assert result['forcedAbort']
    assert result['aborts'] == ['task-stop']
    assert result['destroyed'] and not result['leakedGlobal']


@pytest.mark.skipif(
    not shutil.which('node') or not os.path.isfile(ESBUILD)
    or not os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom')),
    reason='node/jsdom/Vite dev dependencies required',
)
def test_generation_owner_fences_stale_starts_and_bounds_rebuttal_state():
    result = _run_start_harness()
    assert all(result.values()), result


@pytest.mark.skipif(
    not shutil.which('node') or not os.path.isfile(ESBUILD)
    or not os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom')),
    reason='node/jsdom/Vite dev dependencies required',
)
def test_generation_fence_negative_control_exposes_same_paper_races():
    source = Path(SOURCE).read_text(encoding='utf-8')
    marker = """  return reportStartGenerations.get(reportStartKey(view)) === generation
    && String(globals()._activePaperId || '') === paperId
    && (!provisionalStream || view.stream === provisionalStream);"""
    assert marker in source, 'generation-fence marker changed; update the negative control'
    broken = source.replace(
        marker,
        "  return String(globals()._activePaperId || '') === paperId;",
        1,
    )
    result = _run_start_harness(broken)
    assert not result['staleCachedFenced']
    assert not result['staleErrorFenced']
    assert Path(SOURCE).read_text(encoding='utf-8') == source


def test_retained_report_file_contains_renderers_not_task_owner():
    renderer = RETAINED_RENDERER.read_text(encoding='utf-8')
    reader = RETAINED_READER.read_text(encoding='utf-8')
    assert '__tofuInstallReportRuntime' not in renderer
    assert 'function _pollReportTask' not in renderer
    assert 'function _stopPaperReport' not in renderer
    assert 'function _generatePaperReport' not in renderer
    assert 'function _renderFinalReport' in renderer
    for required_port in (
        '_attachReportPush', '_renderReportSkeleton', '_resolveReviewVenue',
        '_syncReportToolbar',
    ):
        assert f'runtimeScope.{required_port} = {required_port};' in renderer
    for required_port in ('_applyResolvedTitle', '_ensurePaperText'):
        assert f'runtimeScope.{required_port} = {required_port};' in reader


def test_rebuttal_textarea_matches_runtime_and_backend_limit():
    index = INDEX_HTML.read_text(encoding='utf-8')
    start = index.index('<textarea id="paperRebuttalInput"')
    opening_tag = index[start:index.index('>', start)]
    assert 'maxlength="40000"' in opening_tag
