"""jsdom guard for the Paper Video Abstract tab (P3 of the motion-video feature).

Loads the REAL shipped ``static/js/paper/video.js`` under jsdom with stubbed
``Api.motion.*`` / ``Api.paper.video*`` and drives the full tab state machine:

  * report_required render (chain to the Report tab) and the honest
    lookup_failed state (a 5xx'd lookup must never lie "generate a report");
  * idle generate card + degrade banner when no TTS slot exists;
  * generate → poll (phase / scene_done events) → done → player + downloads
    + per-scene grid (preview thumbnails, per-scene regen buttons);
  * per-scene regen: button wiring → regen task poll → grid refresh.

NEUTER (source-level negative control): a copy of the module with the
per-scene regen button amputated must FAIL the regen-button probe — the
button is the whole point of the panel (load-bearing, not decoration).

Static guards (no node): Api surface (paper.video* + motion.*), bundle
registration, index.html tab+panel, i18n zh+en keys, the _switchPaperTab
branch, JS syntax.

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
VIDEO_JS = os.path.join(ROOT, 'static', 'js', 'paper', 'video.js')


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
  '<!DOCTYPE html><body><div id="paperVideoContent"></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
const T_MAP = {
  'paper.videoNeedReport': 'NEED_REPORT_TEXT',
  'paper.videoGoReport': 'GO_REPORT',
  'paper.videoNoTts': 'NO_TTS_BANNER_TEXT',
  'paper.videoLookupFailed': 'LOOKUP_FAILED_TEXT',
  'paper.videoRetry': 'RETRY_TEXT',
  'paper.videoHeroTitle': 'HERO_TITLE',
  'paper.videoGenerate': 'GENERATE_TEXT',
  'paper.videoRegen': 'REGEN_TEXT',
  'paper.videoRegening': 'REGENING_TEXT',
  'paper.videoScenesTitle': 'SCENES_TITLE',
  'paper.videoDownload': 'DOWNLOAD_TEXT',
  'paper.videoPhaseCompose': 'PHASE_COMPOSE',
  'reportNoText': 'NO_PAPER_TEXT',
  'paper.reportNoText': 'NO_PAPER_TEXT',
};
win.t = global.t = (k) => T_MAP[k] || k;
win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
// Model-picker stubs: two registered chat models + a toolbar preset, and
// localStorage so the per-panel persistence path runs for real.
global.localStorage = win.localStorage;
win._registeredModels = global._registeredModels = [
  { model_id: 'm-alpha', provider_id: 'p1', provider_name: 'P1' },
  { model_id: 'm-beta', provider_id: 'p1', provider_name: 'P1' },
];
win.config = global.config = { model: 'm-alpha' };

const apiState = {
  statusResp: { ok: true, tts_available: true },
  lookupResp: { ok: true, found: false, report_available: true },
  startResp: { ok: true, task_id: 'motion_x1' },
  pollQueue: [],
  scenesResp: { ok: true, scenes: [
    { scene_id: 'scene-001', start: 0, end: 3, text: '第一句。', has_video: true, has_composition: true },
    { scene_id: 'scene-002', start: 3, end: 6, text: '第二句。', has_video: true, has_composition: true },
  ]},
  regenCalls: [],
  reportTabCalls: 0,
  startBodies: [],
};
global.Api = win.Api = {
  motion: {
    status: async () => apiState.statusResp,
    poll: async () => apiState.pollQueue.length
      ? apiState.pollQueue.shift()
      : { ok: true, done: false, next_cursor: 0, events: [] },
    abort: async () => ({}),
    scenes: async () => apiState.scenesResp,
    regenScene: async (tid, sid) => {
      apiState.regenCalls.push([tid, sid]);
      return { ok: true, task_id: 'motion_regen1', regen_of: tid };
    },
    fileUrl: (tid, part) => '/api/v1/motion/videos/' + tid + '/file' + (part ? '?part=' + part : ''),
    sceneFileUrl: (tid, sid) => '/api/v1/motion/videos/' + tid + '/scenes/' + sid + '/file',
  },
  paper: {
    videoStart: async (body) => { apiState.startBodies.push(body); return apiState.startResp; },
    videoLookup: async () => apiState.lookupResp,
  },
};
win._paperHash = 'hash123abc';
global._paperHash = 'hash123abc';
win._switchPaperTab = (tab) => { if (tab === 'report') apiState.reportTabCalls++; };
global._switchPaperTab = win._switchPaperTab;

eval(fs.readFileSync(process.argv[2], 'utf8'));  // paper/video.js (real, shipped)
_PVIDEO_POLL_MS = 1;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
function host() { return document.getElementById('paperVideoContent'); }
async function settle(n) { for (let i = 0; i < n; i++) await new Promise(r => setTimeout(r, 3)); }

(async () => {
  // ── Case A: report_required — go-report button wired to the report tab ──
  apiState.lookupResp = { ok: true, found: false, report_available: false };
  _pvideo.status = 'idle'; _pvideo.lang = 'zh';
  await _initVideoTab();
  check('report_required_state', host().innerHTML.includes('NEED_REPORT_TEXT'));
  const goBtn = host().querySelector('button.paper-podcast-btn');
  check('go_report_wired', !!goBtn &&
    goBtn.getAttribute('onclick') === "_switchPaperTab('report')");
  win._switchPaperTab('report');
  check('go_report_switches_tab', apiState.reportTabCalls === 1);

  // ── Case A2: FAILED lookup → lookup_failed (honest), NEVER report_required ──
  apiState.lookupResp = null;  // server 5xx with onError:'null'
  await _initVideoTab();
  check('lookup_failed_state', _pvideo.status === 'lookup_failed');
  check('lookup_failed_not_report_required',
    !host().innerHTML.includes('NEED_REPORT_TEXT'));

  // ── Case B: idle card + degrade banner (no TTS slot) ──
  apiState.statusResp = { ok: true, tts_available: false };
  apiState.lookupResp = { ok: true, found: false, report_available: true };
  await _initVideoTab();
  check('idle_card_renders', !!host().querySelector('#videoLangSel')
    && !!host().querySelector('#videoNarrChk') && !!host().querySelector('#videoBurnChk'));
  check('degrade_banner_shown', host().innerHTML.includes('NO_TTS_BANNER_TEXT'));
  // Model picker (the whole point of the panel fix): the field renders in
  // the studio card, seeded from the toolbar preset (config.model).
  check('model_field_renders', !!host().querySelector('#videoModelBtn')
    && !!host().querySelector('#videoModelDropdown'));
  const seedLabel = host().querySelector('#videoModelLabel');
  check('model_seeded_from_preset', !!seedLabel && seedLabel.textContent === 'm-alpha');

  // ── Case C: generate → poll → done → player + scene grid + regen ──
  apiState.statusResp = { ok: true, tts_available: true };
  // Pick a DIFFERENT model than the seed — the start body must carry it.
  _pmSelectModel('video', 'm-beta');
  apiState.pollQueue = [
    { ok: true, done: false, next_cursor: 2, events: [
      { type: 'phase', phase: 'compose' },
      { type: 'scene_done', scene_id: 'scene-001', ok: true, done: 1, total: 2 }] },
    { ok: true, done: true, status: 'done', next_cursor: 3, events: [],
      model: 'm-beta',
      result: { final_path: '/job/final.mp4', duration: 6.0, scenes: 2,
                narrated: true, workdir: '/job' } },
  ];
  await _videoGenerate();
  check('generate_shows_progress', !!host().querySelector('#videoProgressLine'));
  check('start_body_carries_model', apiState.startBodies.length === 1
    && apiState.startBodies[0].model === 'm-beta');
  await settle(8);
  const player = host().querySelector('#paperVideoPlayer');
  check('done_renders_player', !!player);
  const modelBadge = host().querySelector('#videoModelBadge');
  check('done_model_badge', !!modelBadge && modelBadge.textContent === 'm-beta');
  check('done_inline_model_picker', !!host().querySelector('#videoModelDropdown'));
  check('player_src', !!player && player.src.includes(
    '/api/v1/motion/videos/motion_x1/file'));
  const dl = host().querySelector('a[download]');
  check('download_link', !!dl && dl.getAttribute('download').includes('.mp4'));
  await settle(4);  // scenes load
  const cells = host().querySelectorAll('.paper-video-cell');
  check('scene_grid_cells', cells.length === 2);
  check('scene_thumbs', host().querySelectorAll('video.paper-video-thumb').length === 2);
  const regenBtn = host().querySelector('.paper-video-regen[data-scene="scene-002"]');
  check('regen_button_present', !!regenBtn);
  check('regen_wired', !!regenBtn &&
    regenBtn.getAttribute('onclick') === "_videoRegenScene('scene-002')");
  // regen flow: button → regen task → poll done → grid refresh
  apiState.pollQueue = [
    { ok: true, done: true, status: 'done', next_cursor: 1, events: [],
      result: { final_path: '/job/final.mp4', regen_of: 'motion_x1',
                scene_id: 'scene-002', duration: 6.0 } },
  ];
  await _videoRegenScene('scene-002');
  check('regen_dispatched', apiState.regenCalls.length === 1
    && apiState.regenCalls[0][0] === 'motion_x1'
    && apiState.regenCalls[0][1] === 'scene-002');
  await settle(6);
  check('regen_state_cleared', _pvideo.regenSceneId === '' && _pvideo.regenTaskId === '');

  console.log(out.join('\n'));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _run_harness(video_js: str) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_paper_video_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        return subprocess.run(
            ['node', harness, video_js, ROOT],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_video_tab_state_machine():
    proc = _run_harness(VIDEO_JS)
    out = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{out}'
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'video tab failures:\n' + out
    assert out.count('PASS') >= 17, f'expected >=17 PASS lines, got:\n{out}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_regen_button_loadbearing():
    """Amputate the per-scene regen BUTTON from a COPY of the module and
    prove the regen-button probe flips to FAIL — the per-scene re-render
    affordance is the panel's raison d'être (load-bearing)."""
    src = open(VIDEO_JS, encoding='utf-8').read()
    marker = "h += '<button class=\"paper-video-regen\" data-scene=\"'"
    assert marker in src, 'regen button marker not found — test is stale'
    # Remove the whole regen-button append (4 chained h += lines end with ';')
    import re
    broken, n = re.subn(
        re.escape("h += '<button class=\"paper-video-regen\" data-scene=\"'")
        + r"[\s\S]*?\+ '</button>';\n",
        '', src, count=1)
    assert n == 1, 'regen button block not excised — test is stale'

    tmp = os.path.join(HERE, '_paper_video_no_regen.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(broken)
    try:
        chk = subprocess.run(['node', '--check', tmp], capture_output=True,
                             text=True, timeout=30)
        assert chk.returncode == 0, f'patched JS invalid: {chk.stderr}'
        proc = _run_harness(tmp)
        out = proc.stdout.strip()
        assert proc.returncode == 0, f'node crashed: {proc.stderr}\n{out}'
        assert 'FAIL regen_button_present' in out, \
            'amputating the regen button did NOT flip the probe:\n' + out
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    assert open(VIDEO_JS, encoding='utf-8').read() == src, 'shipped file modified!'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_model_in_start_body_loadbearing():
    """Amputate ``model: _pvideo.model || undefined,`` from a COPY of the
    module and prove the start-body probe flips to FAIL — the picked model
    reaching the request is the whole point of the picker; without this
    line the UI is a placebo (the backend would silently use the default)."""
    src = open(VIDEO_JS, encoding='utf-8').read()
    marker = "      model: _pvideo.model || undefined,\n"
    assert marker in src, 'model body marker not found — test is stale'
    broken = src.replace(marker, '', 1)
    assert broken != src

    tmp = os.path.join(HERE, '_paper_video_no_model.js')
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

    assert open(VIDEO_JS, encoding='utf-8').read() == src, 'shipped file modified!'


# ═══ Static guards (no node required) ═══

def test_static_api_surface():
    src = open(os.path.join(ROOT, 'static', 'js', 'api.js'), encoding='utf-8').read()
    for name in ('videoStart', 'videoLookup'):
        assert name in src, f'Api.paper.{name} missing'
    for name in ('status', 'poll', 'abort', 'scenes', 'regenScene',
                 'fileUrl', 'sceneFileUrl'):
        assert name in src, f'Api.motion.{name} missing'
    assert 'const motion = {' in src and 'swarm, endpoint, logs, motion,' in src, \
        'motion domain not exposed on the Api surface'


def test_static_bundle_registration():
    from lib.js_bundler import _BUNDLE_FILES, _DEFERRED_FILES
    all_files = list(_BUNDLE_FILES) + list(_DEFERRED_FILES)
    assert 'paper/video.js' in all_files, \
        'paper/video.js not in the bundler allowlist (CLAUDE.md §3.2.1)'


def test_static_tab_wiring():
    html = open(os.path.join(ROOT, 'index.html'), encoding='utf-8').read()
    assert html.count('data-tab="video"') >= 2, \
        'index.html needs the video tab button AND panel'
    assert 'paperVideoContent' in html
    reader = open(os.path.join(ROOT, 'static', 'js', 'paper-reader.js'),
                  encoding='utf-8').read()
    assert "if (tab === 'video') _initVideoTab();" in reader, \
        '_switchPaperTab video branch missing'


def test_static_i18n_keys():
    src = open(os.path.join(ROOT, 'static', 'js', 'i18n.js'), encoding='utf-8').read()
    keys = ('paper.tabVideo', 'paper.videoHint', 'paper.videoGenerate',
            'paper.videoNoTts', 'paper.videoNeedReport', 'paper.videoRegen',
            'paper.videoRegening', 'paper.videoScenesTitle',
            'paper.videoBurnIn', 'paper.videoNarration')
    for key in keys:
        assert f"'{key}'" in src, f'i18n key {key} missing'
    import re
    for m in re.finditer(
            r"'(paper\.video[^']*|paper\.tabVideo)':\s*\{\s*zh:\s*'((?:[^'\\]|\\.)*)',\s*en:\s*'((?:[^'\\]|\\.)*)'\s*\}",
            src):
        assert m.group(2).strip() and m.group(3).strip(), \
            f'i18n key {m.group(1)} has an empty zh or en'


def test_static_js_syntax():
    if not shutil.which('node'):
        pytest.skip('node not installed')
    proc = subprocess.run(['node', '--check', VIDEO_JS],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f'video.js syntax: {proc.stderr}'


if __name__ == '__main__':
    import sys
    test_static_api_surface()
    test_static_bundle_registration()
    test_static_tab_wiring()
    test_static_i18n_keys()
    if _node_deps_available():
        test_static_js_syntax()
        test_video_tab_state_machine()
        test_NEUTER_regen_button_loadbearing()
    else:
        print('SKIP jsdom cases — node + jsdom not available')
    print('ALL PASSED')
    sys.exit(0)
