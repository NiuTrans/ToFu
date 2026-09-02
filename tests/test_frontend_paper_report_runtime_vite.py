"""Compiled Report/Review task owner: replay, stop and stale-stream fences."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_section_path


pytestmark = pytest.mark.unit
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
SOURCE = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'paper', 'report-runtime.ts')
RETAINED_RENDERER = Path(runtime_section_path(
    'paper/report.js', scope_prelude=False))
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


def test_retained_report_file_contains_renderers_not_task_owner():
    renderer = RETAINED_RENDERER.read_text(encoding='utf-8')
    assert '__tofuInstallReportRuntime' not in renderer
    assert 'function _pollReportTask' not in renderer
    assert 'function _stopPaperReport' not in renderer
    assert 'function _renderFinalReport' in renderer
