"""jsdom guard for the Paper Podcast tab (Layer 4 of the podcast feature).

Loads the REAL shipped ``static/js/paper/podcast.js`` under jsdom with stubbed
``Api.paper.podcast*`` and drives the full tab state machine:

  * report_required render → the go-report button really switches tabs;
  * idle generate card shows the degrade banner when no TTS slot exists
    (owner directive — degrade must be VISIBLE, never a silent dead-end);
  * generate → poll (progress events) → done → player + transcript render,
    click-to-seek lands on the segment prefix offset, timeupdate highlights;
  * script_only render: degrade banner, NO <audio>, script export present;
  * sleep timer (owner P1): selecting minutes schedules a pause; firing it
    pauses the audio.

NEUTER (source-level negative control): a copy of the module with the
degrade-banner call amputated must FAIL the banner probe — proving the
banner is load-bearing, not incidental. The shipped file is never modified.

Static guards (no node): Api surface, bundle registration, index.html
tab+panel, i18n zh+en keys, the _switchPaperTab branch, JS syntax.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
PODCAST_JS = os.path.join(ROOT, 'static', 'js', 'paper', 'podcast.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div id="paperPodcastContent"></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.localStorage = win.localStorage;
global.Blob = win.Blob;
global.URL = win.URL;
win.URL.createObjectURL = () => 'blob:fake';
win.URL.revokeObjectURL = () => {};
win.HTMLAnchorElement.prototype.click = function () { this._clicked = true; };
const T_MAP = {
  'paper.podcastNoTts': 'NO_TTS_BANNER_TEXT',
  'paper.podcastNeedReport': 'NEED_REPORT_TEXT',
  'paper.podcastGoReport': 'GO_REPORT',
  'paper.podcastScriptPhase': 'WRITING_SCRIPT',
  'paper.podcastAudioPhase': 'SYNTH_AUDIO',
  'paper.reportNoText': 'NO_PAPER_TEXT',
  'paper.podcastLookupFailed': 'LOOKUP_FAILED_TEXT',
  'paper.podcastRetry': 'RETRY_TEXT',
  'paper.podcastHeroTitle': 'HERO_TITLE',
  'paper.podcastStepReport': 'STEP_REPORT',
  'paper.podcastStepPodcast': 'STEP_PODCAST',
};
win.t = global.t = (k) => T_MAP[k] || k;
win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
win.debugLog = global.debugLog = () => {};
// Model-picker stubs: two registered chat models + a toolbar preset. The
// picker's fallbacks (isChatModel/_modelShortName undefined) stay exercised.
win._registeredModels = global._registeredModels = [
  { model_id: 'm-alpha', provider_id: 'p1', provider_name: 'P1' },
  { model_id: 'm-beta', provider_id: 'p1', provider_name: 'P1' },
];
win.config = global.config = { model: 'm-alpha' };

const SCRIPT = { title: '测试播客', segments: [
  { id: 0, section: 'cold_open', speaker: 'host', text: '开场:成绩 86.3。', est_seconds: 60 },
  { id: 1, section: 'method', speaker: 'host', text: '方法:稀疏路由。', est_seconds: 60 },
  { id: 2, section: 'recap', speaker: 'host', text: '三条带走。', est_seconds: 60 },
]};
const DONE_BODY = { ok: true, done: true, status: 'done', cursor: 3, events: [],
  script: SCRIPT, meta: { container: 'mp3' }, audioUrl: '/api/v1/paper/podcast/audio/h1/short/zh/alloy',
  durationSec: 180, scriptOnly: false, model: 'm-beta' };
const SCRIPT_ONLY_BODY = Object.assign({}, DONE_BODY, {
  audioUrl: '', scriptOnly: true, meta: { degrade_reason: 'no_tts_slot' } });

// Mutable Api mock — each case rewires the queues it needs.
const apiState = {
  statusResp: { ok: true, tts_available: true, default_voice: 'alloy' },
  lookupResp: { ok: true, found: false, report_available: true },
  startResp: { ok: true, task_id: 'podcast_x1' },
  startBodies: [],
  lookupBodies: [],
  pollQueue: [],
  reportTabCalls: 0,
};
global.Api = win.Api = { paper: {
  podcastStatus: async () => apiState.statusResp,
  podcastLookup: async (body) => { apiState.lookupBodies.push(body); return apiState.lookupResp; },
  podcastStart: async (body) => { apiState.startBodies.push(body); return apiState.startResp; },
  podcastPoll: async () => apiState.pollQueue.length
    ? apiState.pollQueue.shift()
    : { ok: true, done: false, cursor: 0, events: [], progress: { done: 0, total: 0 } },
  podcastAbort: async () => ({}),
  podcastScript: async () => ({}),
}};
win._paperHash = 'hash123abc';
global._paperHash = 'hash123abc';
win._switchPaperTab = (tab) => { if (tab === 'report') apiState.reportTabCalls++; };
global._switchPaperTab = win._switchPaperTab;

eval(fs.readFileSync(process.argv[2], 'utf8'));  // paper/podcast.js (real, shipped)
_PODCAST_POLL_MS = 1;  // shrink the poll cadence for the harness

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
function host() { return document.getElementById('paperPodcastContent'); }
async function settle(n) { for (let i = 0; i < n; i++) await new Promise(r => setTimeout(r, 3)); }

(async () => {
  // ── Case A: report_required — go-report button switches to the report tab ──
  apiState.lookupResp = { ok: true, found: false, report_available: false };
  _podcast.status = 'idle'; _podcast.mode = 'short'; _podcast.lang = 'zh';
  await _initPodcastTab();
  check('report_required_state', host().innerHTML.includes('NEED_REPORT_TEXT'));
  check('report_required_hero', !!host().querySelector('.paper-podcast-hero')
    && host().innerHTML.includes('HERO_TITLE')
    && host().innerHTML.includes('STEP_REPORT')
    && host().innerHTML.includes('STEP_PODCAST'));
  const goBtn = host().querySelector('button.paper-podcast-btn');
  check('go_report_button_present', !!goBtn);
  // jsdom does NOT execute inline onclick attributes — assert the wiring
  // then invoke the global the browser WOULD call.
  check('go_report_wired', !!goBtn &&
    goBtn.getAttribute('onclick') === "_switchPaperTab('report')");
  win._switchPaperTab('report');
  check('go_report_switches_tab', apiState.reportTabCalls === 1);

  // ── Case A2: FAILED lookup → lookup_failed (honest), NEVER report_required ──
  // Regression guard for 2026-07-25: a 5xx'd lookup (onError:'null' → null)
  // used to fall through to report_required — the "generate a report first"
  // lie for papers that HAD one, unfixable by chaining to the Report tab.
  apiState.lookupResp = null;  // server 5xx with onError:'null'
  await _initPodcastTab();
  check('lookup_failed_state', _podcast.status === 'lookup_failed');
  check('lookup_failed_not_report_required',
    !host().innerHTML.includes('NEED_REPORT_TEXT'));
  check('lookup_failed_hero', !!host().querySelector('.paper-podcast-hero')
    && host().innerHTML.includes('LOOKUP_FAILED_TEXT'));
  const retryBtn = host().querySelector('button.paper-podcast-btn-ghost');
  check('lookup_retry_wired', !!retryBtn &&
    retryBtn.getAttribute('onclick') === '_initPodcastTab(true)');
  // ok:false (reachable server, explicit failure) → same honest state
  apiState.lookupResp = { ok: false, error: 'boom' };
  await _initPodcastTab();
  check('lookup_ok_false_also_lookup_failed',
    _podcast.status === 'lookup_failed'
    && !host().innerHTML.includes('NEED_REPORT_TEXT'));

  // ── Case B: idle card + degrade banner (no TTS slot configured) ──
  apiState.statusResp = { ok: true, tts_available: false, default_voice: '' };
  apiState.lookupResp = { ok: true, found: false, report_available: true };
  await _initPodcastTab();
  check('idle_card_renders', !!host().querySelector('#podcastModeSel'));
  check('degrade_banner_shown', host().innerHTML.includes('NO_TTS_BANNER_TEXT'));
  // Model picker (the whole point of the panel fix): the field renders in
  // the studio card, seeded from the toolbar preset (config.model).
  check('model_field_renders', !!host().querySelector('#podcastModelBtn')
    && !!host().querySelector('#podcastModelDropdown'));
  const seedLabel = host().querySelector('#podcastModelLabel');
  check('model_seeded_from_preset', !!seedLabel && seedLabel.textContent === 'm-alpha');

  // ── Case C: generate → poll → done → player + transcript + seek ──
  apiState.statusResp = { ok: true, tts_available: true, default_voice: 'alloy' };
  // Pick a DIFFERENT model than the seed — the start body must carry it.
  _pmSelectModel('podcast', 'm-beta');
  apiState.pollQueue = [
    { ok: true, done: false, cursor: 2, events: [{ type: 'segment_done', done: 1, total: 3 }],
      progress: { done: 1, total: 3 } },
    DONE_BODY,
  ];
  await _podcastGenerate();
  check('generate_shows_progress', host().innerHTML.includes('WRITING_SCRIPT'));
  check('start_body_carries_model', apiState.startBodies.length === 1
    && apiState.startBodies[0].model === 'm-beta');
  check('model_pick_persisted', win.localStorage.getItem('paperPodcastModel') === 'm-beta');
  check('generate_persists_options',
    win.localStorage.getItem('paperPodcastMode') === 'short'
    && win.localStorage.getItem('paperPodcastLang') === 'zh');
  await settle(6);
  const progLine = host().innerHTML;
  check('done_renders_player', !!host().querySelector('#podcastAudio'));
  const modelBadge = host().querySelector('#podcastModelBadge');
  check('done_model_badge', !!modelBadge && modelBadge.textContent === 'm-beta');
  check('done_inline_model_picker', !!host().querySelector('#podcastModelDropdown'));
  check('audio_src', host().querySelector('#podcastAudio').src.includes(
    '/api/v1/paper/podcast/audio/h1/short/zh/alloy'));
  const dl = host().querySelector('a[download]');
  check('download_link', !!dl && dl.getAttribute('download').includes('.mp3'));
  const segs = host().querySelectorAll('.paper-podcast-seg');
  check('transcript_segments', segs.length === 3);
  // click segment 2 → seeks to seg[0].est_seconds (60). Inline onclick is
  // inert under jsdom — assert the wiring, then call the global directly.
  const audio = host().querySelector('#podcastAudio');
  check('seek_wired', segs[1].getAttribute('onclick') === '_podcastSeekSegment(1)');
  _podcastSeekSegment(1);
  check('seek_prefix_offset', Math.abs(audio.currentTime - 60) < 0.01);
  // timeupdate → the second segment highlights
  audio.currentTime = 61;
  audio.dispatchEvent(new win.Event('timeupdate'));
  check('timeupdate_highlights', segs[1].classList.contains('active')
    && !segs[0].classList.contains('active'));

  // ── Case D: sleep timer — scheduling + firing pauses the audio ──
  let paused = false;
  audio.pause = () => { paused = true; };
  const sel = host().querySelector('#podcastSleepSel');
  check('sleep_select_present', !!sel);
  sel.value = '5';
  _podcastSleepTimerChange();
  check('sleep_timer_scheduled', !!_podcast.sleepTimerId && _podcast.sleepDeadline > 0);
  // fire the scheduled callback immediately (harness: invoke via captured state)
  // The module stored the timer id on _podcast; fire by clearing through the
  // recorded deadline path — emulate by calling the change handler's effect:
  // reach into the pending timer via setTimeout registry is not exposed, so
  // instead validate the pause path through the module's own timeout:
  await new Promise(r => setTimeout(r, 5));  // timer is 5min — cannot fire here
  check('sleep_timer_not_fired_early', paused === false);
  // Simulate the fire by directly invoking the scheduled callback: replace
  // setTimeout capture — rerun the change handler with a captured callback.
  let captured = null;
  const origSetTimeout = global.setTimeout;
  global.setTimeout = (fn, ms) => { captured = fn; return 777; };
  sel.value = '5';
  _podcastSleepTimerChange();
  global.setTimeout = origSetTimeout;
  check('sleep_callback_captured', typeof captured === 'function');
  if (captured) captured();
  check('sleep_fire_pauses', paused === true);

  // ── Case E: script_only — banner, NO audio, export present ──
  _podcast.data = SCRIPT_ONLY_BODY;
  _podcast.status = 'script_only';
  _pcRender();
  check('script_only_banner', host().innerHTML.includes('NO_TTS_BANNER_TEXT'));
  check('script_only_no_audio', !host().querySelector('#podcastAudio'));
  const exportBtn = Array.from(host().querySelectorAll('button'))
    .find(b => b.getAttribute('onclick') === '_podcastExportScript()');
  check('script_export_button', !!exportBtn);
  let exportThrew = false;
  try { _podcastExportScript(); } catch (e) { exportThrew = true; }
  check('script_export_runs', !exportThrew);  // URL/Blob stubbed above

  // ── Case F: reload-grade persistence — (mode, lang) survive a page reload ──
  // The family: the backend re-attach scan matches (paper_hash, mode, lang)
  // exactly, so a reload that reset the panel to 'short'/'zh' made a 'full'
  // run invisible (the tab-switch half is the route fix in routes/paper.py).
  // The picks persist exactly like the model (paperPodcastModel).
  // F1: picking an option card syncs the hidden select AND persists.
  apiState.statusResp = { ok: true, tts_available: true, default_voice: 'alloy' };
  apiState.lookupResp = { ok: true, found: false, report_available: true };
  win.localStorage.removeItem('paperPodcastMode');
  win.localStorage.removeItem('paperPodcastLang');
  _podcast.status = 'idle'; _podcast.mode = 'short'; _podcast.lang = 'zh';
  await _initPodcastTab();
  const fullBtn = host().querySelector('[data-sel="podcastModeSel"][data-value="full"]');
  check('reload_opt_card_present', !!fullBtn);
  _pmPick(fullBtn);
  check('pick_syncs_hidden_select',
    host().querySelector('#podcastModeSel').value === 'full');
  check('mode_pick_persisted',
    win.localStorage.getItem('paperPodcastMode') === 'full');
  const enBtn = host().querySelector('[data-sel="podcastLangSel"][data-value="en"]');
  _pmPick(enBtn);
  check('lang_pick_persisted',
    win.localStorage.getItem('paperPodcastLang') === 'en');
  // F2: SIMULATED RELOAD — fresh module state (factory defaults), same
  // localStorage. The lookup must go out with the PERSISTED (full, en),
  // and a live task must re-attach with the server's clocks.
  _podcast.mode = 'short'; _podcast.lang = 'zh';
  _podcast.status = 'idle'; _podcast.taskId = '';
  const created = Date.now() - 60000, updated = Date.now() - 5000;
  apiState.lookupResp = { ok: true, found: true, running: true,
    task_id: 'podcast_reload1', model: 'm-alpha',
    createdAt: created, updatedAt: updated };
  apiState.lookupBodies = [];
  await _initPodcastTab();
  check('reload_lookup_body_options', apiState.lookupBodies.length === 1
    && apiState.lookupBodies[0].mode === 'full'
    && apiState.lookupBodies[0].lang === 'en');
  check('reload_reattaches', _podcast.status === 'generating'
    && _podcast.taskId === 'podcast_reload1');
  check('reload_renders_console', !!host().querySelector('.pm-console'));
  check('reload_server_clock_adopted', _podcast.genStartedAt === created);
  _pcStopPolling();  // keep the exit deterministic (poll cadence is 1ms here)

  console.log(out.join('\n'));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _run_harness(podcast_js: str) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_paper_podcast_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        return subprocess.run(
            ['node', harness, podcast_js, ROOT],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_podcast_tab_state_machine():
    proc = _run_harness(PODCAST_JS)
    out = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{out}'
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'podcast tab failures:\n' + out
    assert out.count('PASS') >= 40, f'expected >=40 PASS lines, got:\n{out}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_degrade_banner_loadbearing():
    """Amputate the degrade-banner call from a COPY of the module and prove
    the idle-card banner probe flips to FAIL — the banner is load-bearing
    (owner directive: the no-TTS degrade must be visible)."""
    src = open(PODCAST_JS, encoding='utf-8').read()
    marker = "if (!s.ttsAvailable) h += _pcDegradeBanner();"
    assert marker in src, 'banner marker not found — test is stale'
    broken = src.replace(marker, '', 1)
    assert broken != src

    tmp = os.path.join(HERE, '_paper_podcast_no_banner.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(broken)
    try:
        chk = subprocess.run(['node', '--check', tmp], capture_output=True,
                             text=True, timeout=30)
        assert chk.returncode == 0, f'patched JS invalid: {chk.stderr}'
        proc = _run_harness(tmp)
        out = proc.stdout.strip()
        assert proc.returncode == 0, f'node crashed: {proc.stderr}\n{out}'
        assert 'FAIL degrade_banner_shown' in out, \
            'amputating the banner did NOT flip the probe — banner non-load-bearing:\n' + out
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    assert open(PODCAST_JS, encoding='utf-8').read() == src, 'shipped file modified!'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_lookup_gate_loadbearing():
    """Amputate the ``look && look.ok`` gate from a COPY of the module
    (restoring the old fall-through) and prove a null lookup flips the
    ``lookup_failed`` probe to FAIL — the gate is what stands between a
    server 5xx and the "generate a report first" lie (2026-07-25 incident:
    the missing paper_podcasts table 500'd every lookup and the tab told
    users with reports to go make one)."""
    src = open(PODCAST_JS, encoding='utf-8').read()
    gated = """    if (look && look.ok) {
      _podcast.reportAvailable = !!look.report_available;
      _podcast.status = _podcast.reportAvailable ? 'idle' : 'report_required';
    } else {"""
    assert gated in src, 'ok-gate marker not found — test is stale'
    # The pre-fix shape: a failed lookup falls through to report_required.
    ungated = """    _podcast.reportAvailable = !!(look && look.report_available);
    _podcast.status = _podcast.reportAvailable ? 'idle' : 'report_required';
    if (false) {"""
    broken = src.replace(gated, ungated, 1)
    assert broken != src

    tmp = os.path.join(HERE, '_paper_podcast_no_gate.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(broken)
    try:
        chk = subprocess.run(['node', '--check', tmp], capture_output=True,
                             text=True, timeout=30)
        assert chk.returncode == 0, f'patched JS invalid: {chk.stderr}'
        proc = _run_harness(tmp)
        out = proc.stdout.strip()
        assert proc.returncode == 0, f'node crashed: {proc.stderr}\n{out}'
        assert 'FAIL lookup_failed_state' in out, \
            'amputating the ok-gate did NOT flip the probe — gate non-load-bearing:\n' + out
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    assert open(PODCAST_JS, encoding='utf-8').read() == src, 'shipped file modified!'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_model_in_start_body_loadbearing():
    """Amputate ``model: _podcast.model || undefined,`` from a COPY of the
    module and prove the start-body probe flips to FAIL — the picked model
    reaching the request is the whole point of the picker; without this
    line the UI is a placebo (the backend would silently use the default)."""
    src = open(PODCAST_JS, encoding='utf-8').read()
    marker = "      voice: _podcast.voice, model: _podcast.model || undefined,\n"
    assert marker in src, 'model body marker not found — test is stale'
    broken = src.replace(marker, '      voice: _podcast.voice,\n', 1)
    assert broken != src

    tmp = os.path.join(HERE, '_paper_podcast_no_model.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(broken)
    try:
        chk = subprocess.run(['node', '--check', tmp], capture_output=True,
                             text=True, timeout=30)
        assert chk.returncode == 0, f'patched JS invalid: {chk.stderr}'
        proc = _run_harness(tmp)
        out = proc.stdout.strip()
        assert proc.returncode == 0, f'node crashed: {proc.stderr}\n{out}'
        assert 'FAIL start_body_carries_model' in out, \
            'amputating the model field did NOT flip the probe:\n' + out
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    assert open(PODCAST_JS, encoding='utf-8').read() == src, 'shipped file modified!'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_seed_options_loadbearing():
    """Amputate the ``_pcSeedOptions();`` call from a COPY of the module and
    prove the reload lookup-body probe flips to FAIL — the seed is what makes
    a page reload re-issue the SAME (mode, lang) the run was started with;
    without it a 'full' run is invisible to the reset-to-'short' panel."""
    src = open(PODCAST_JS, encoding='utf-8').read()
    marker = "  _pcSeedOptions();\n"
    assert marker in src, 'seed marker not found — test is stale'
    broken = src.replace(marker, '', 1)
    assert broken != src

    tmp = os.path.join(HERE, '_paper_podcast_no_seed.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(broken)
    try:
        chk = subprocess.run(['node', '--check', tmp], capture_output=True,
                             text=True, timeout=30)
        assert chk.returncode == 0, f'patched JS invalid: {chk.stderr}'
        proc = _run_harness(tmp)
        out = proc.stdout.strip()
        assert proc.returncode == 0, f'node crashed: {proc.stderr}\n{out}'
        assert 'FAIL reload_lookup_body_options' in out, \
            'amputating the seed did NOT flip the probe — seed non-load-bearing:\n' + out
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    assert open(PODCAST_JS, encoding='utf-8').read() == src, 'shipped file modified!'


# ═══ Static guards (no node required) ═══

def test_static_api_surface():
    src = open(os.path.join(ROOT, 'static', 'js', 'api.js'), encoding='utf-8').read()
    for name in ('podcastStatus', 'podcastStart', 'podcastPoll',
                 'podcastLookup', 'podcastAbort', 'podcastScript'):
        assert name in src, f'Api.paper.{name} missing'


def test_static_bundle_registration():
    from lib.js_bundler import _BUNDLE_FILES, _DEFERRED_FILES
    all_files = list(_BUNDLE_FILES) + list(_DEFERRED_FILES)
    assert 'paper/podcast.js' in all_files, \
        'paper/podcast.js not in the bundler allowlist (CLAUDE.md §3.2.1)'


def test_static_tab_wiring():
    html = open(os.path.join(ROOT, 'index.html'), encoding='utf-8').read()
    assert html.count('data-tab="podcast"') >= 2, \
        'index.html needs the podcast tab button AND panel'
    assert 'paperPodcastContent' in html
    reader = open(os.path.join(ROOT, 'static', 'js', 'paper-reader.js'),
                  encoding='utf-8').read()
    assert "if (tab === 'podcast') _initPodcastTab();" in reader, \
        '_switchPaperTab podcast branch missing'


def test_static_i18n_keys():
    src = open(os.path.join(ROOT, 'static', 'js', 'i18n.js'), encoding='utf-8').read()
    for key in ('paper.tabPodcast', 'paper.podcastNoTts', 'paper.podcastNeedReport',
                'paper.podcastSleepTimer', 'paper.podcastDownloadAudio',
                'paper.podcastGenerate', 'paper.podcastScriptPhase'):
        assert f"'{key}'" in src, f'i18n key {key} missing'
    # every podcast key must carry BOTH zh and en
    import re
    for m in re.finditer(r"'(paper\.podcast[^']*|paper\.tabPodcast)':\s*\{\s*zh:\s*'((?:[^'\\]|\\.)*)',\s*en:\s*'((?:[^'\\]|\\.)*)'\s*\}", src):
        assert m.group(2).strip() and m.group(3).strip(), \
            f'i18n key {m.group(1)} has an empty zh or en'


def test_static_option_persistence():
    """Reload-grade re-attach contract (static half):
      1. podcast.js persists (mode, lang) under paperPodcastMode/Lang and
         seeds them inside tab init BEFORE the lookup call;
      2. the video panel is pinned CLEAN on this axis — its lookup body is
         paper_hash-only, so a stale lang pick can never hide a live task."""
    src = open(PODCAST_JS, encoding='utf-8').read()
    assert "'paperPodcastMode'" in src and "'paperPodcastLang'" in src
    seed_call = src.index('  _pcSeedOptions();')
    lookup_call = src.index('podcastLookup({')
    assert seed_call < lookup_call, 'seed must run before the lookup'
    vsrc = open(os.path.join(ROOT, 'static', 'js', 'paper', 'video.js'),
                encoding='utf-8').read()
    assert 'videoLookup({ paper_hash: _pvideo.paperHash })' in vsrc, \
        'video lookup must stay paper_hash-only (reload-safe by construction)'


def test_static_js_syntax():
    if not shutil.which('node'):
        pytest.skip('node not installed')
    proc = subprocess.run(['node', '--check', PODCAST_JS],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f'podcast.js syntax: {proc.stderr}'


if __name__ == '__main__':
    import sys
    test_static_api_surface()
    test_static_bundle_registration()
    test_static_tab_wiring()
    test_static_i18n_keys()
    if _node_deps_available():
        test_static_js_syntax()
        test_podcast_tab_state_machine()
        test_NEUTER_degrade_banner_loadbearing()
    else:
        print('SKIP jsdom cases — node + jsdom not available')
    print('ALL PASSED')
    sys.exit(0)
