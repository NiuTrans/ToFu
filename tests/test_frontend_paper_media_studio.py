#!/usr/bin/env python3
"""jsdom guards for the paper media STUDIO console (2026-07 redesign).

The podcast + video idle cards were rebuilt from a cramped wrapping row of
tiny <select>s into a "studio console": rich option cards + segmented
controls + toggle switches + a full-width CTA. The contract that keeps the
old suites honest is UNCHANGED (real <select>/<input> ids, .paper-podcast-btn
wiring, hero/terminal states); this suite pins the NEW contract:

  * idle renders the studio head (badge + title + hint), option cards with
    is-selected reflecting state, hidden .pm-sr selects, the voice wrap and
    the .pm-cta button;
  * _pmPick is the ONLY bridge: clicking a card/segment writes data-value
    into the hidden select AND moves .is-selected — then _podcastGenerate /
    _videoGenerate read the SELECTS, so a card pick really reaches the
    start payload (end-to-end chain card → select → Api.*.start body);
  * video toggles keep the real #videoNarrChk / #videoBurnChk checkboxes
    (CSS paints the switch; JS still reads .checked);
  * generating renders the production console (.pm-console + .pm-eq for
    podcast / .pm-renderbar for video); podcast done renders the
    now-playing bar (.pm-player > #podcastAudio) and play/pause toggles the
    spinning-disc .is-playing class.

NEUTER: amputating the value-write line from _pmPick in a COPY of the
module must FAIL the card→select probe — the bridge is load-bearing, not
decoration (without it the cards are dead buttons that silently discard
the user's pick).

Static guards (no node): new CSS classes exist in styles.css, new i18n
keys defined zh+en, JS syntax. Skips cleanly without node + jsdom.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
PODCAST_JS = os.path.join(ROOT, 'static', 'js', 'paper', 'podcast.js')
VIDEO_JS = os.path.join(ROOT, 'static', 'js', 'paper', 'video.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_PODCAST_HARNESS = r"""
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
const T_MAP = {
  'paper.podcastStudioTitle': 'STUDIO_TITLE',
  'paper.podcastHint': 'HINT_TEXT',
  'paper.podcastModeShortName': 'MODE_SHORT',
  'paper.podcastModeFullName': 'MODE_FULL',
  'paper.podcastModeShortSub': 'SHORT_SUB',
  'paper.podcastModeFullSub': 'FULL_SUB',
  'paper.mediaOptDuration': 'DURATION_LABEL',
  'paper.mediaOptLang': 'LANG_LABEL',
  'paper.mediaOptVoice': 'VOICE_LABEL',
  'paper.podcastGenerate': 'GENERATE_TEXT',
  'paper.podcastMakingTitle': 'MAKING_TITLE',
  'paper.podcastTranscriptTitle': 'TRANSCRIPT_TITLE',
  'paper.podcastAbort': 'ABORT_TEXT',
};
win.t = global.t = (k) => T_MAP[k] || k;
win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

const SCRIPT = { title: '测试播客', segments: [
  { id: 0, section: 'cold_open', text: '开场。', est_seconds: 60 },
]};
const DONE_BODY = { ok: true, done: true, status: 'done', cursor: 1, events: [],
  script: SCRIPT, meta: { container: 'mp3' }, audioUrl: '/audio/x.mp3',
  durationSec: 60, scriptOnly: false };

const apiState = {
  statusResp: { ok: true, tts_available: true, default_voice: 'alloy' },
  lookupResp: { ok: true, found: false, report_available: true },
  startBody: null,
};
global.Api = win.Api = { paper: {
  podcastStatus: async () => apiState.statusResp,
  podcastLookup: async () => apiState.lookupResp,
  podcastStart: async (b) => { apiState.startBody = b; return { ok: true, task_id: 'podcast_x1' }; },
  podcastPoll: async () => ({ ok: true, done: false, cursor: 0, events: [] }),
  podcastAbort: async () => ({}),
  podcastScript: async () => ({}),
}};
win._paperHash = 'hash123abc';
global._paperHash = 'hash123abc';
win._switchPaperTab = () => {};
global._switchPaperTab = win._switchPaperTab;

eval(fs.readFileSync(process.argv[2], 'utf8'));  // paper/podcast.js (real, shipped)
_PODCAST_POLL_MS = 1;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
function host() { return document.getElementById('paperPodcastContent'); }
async function settle(n) { for (let i = 0; i < n; i++) await new Promise(r => setTimeout(r, 3)); }

(async () => {
  // ── Case A: idle studio console structure ──
  await _initPodcastTab();
  check('studio_card', !!host().querySelector('.paper-podcast-card.pm-studio'));
  check('studio_head', !!host().querySelector('.pm-studio-badge')
    && host().innerHTML.includes('STUDIO_TITLE')
    && host().innerHTML.includes('HINT_TEXT'));
  const modeCards = host().querySelectorAll('.pm-opt[data-sel="podcastModeSel"]');
  check('mode_option_cards', modeCards.length === 2
    && host().innerHTML.includes('MODE_SHORT')
    && host().innerHTML.includes('MODE_FULL')
    && host().innerHTML.includes('SHORT_SUB'));
  check('mode_card_default_selected',
    host().querySelector('.pm-opt[data-value="short"]').classList.contains('is-selected')
    && !host().querySelector('.pm-opt[data-value="full"]').classList.contains('is-selected'));
  const modeSel = host().querySelector('#podcastModeSel');
  check('hidden_select_present', !!modeSel && modeSel.classList.contains('pm-sr')
    && modeSel.value === 'short');
  const segBtns = host().querySelectorAll('.pm-seg-btn[data-sel="podcastLangSel"]');
  check('lang_segmented_control', segBtns.length === 2
    && host().querySelector('.pm-seg-btn[data-value="zh"]').classList.contains('is-selected')
    && !!host().querySelector('#podcastLangSel.pm-sr'));
  check('voice_wrap', !!host().querySelector('.pm-voice-wrap input#podcastVoiceInp'));
  const cta = host().querySelector('.paper-podcast-btn.pm-cta');
  check('cta_button', !!cta && cta.getAttribute('onclick') === '_podcastGenerate()');

  // ── Case B: _pmPick bridges card → hidden select → start payload ──
  _pmPick(host().querySelector('.pm-opt[data-value="full"]'));
  check('pick_writes_select', modeSel.value === 'full');
  check('pick_moves_selection',
    host().querySelector('.pm-opt[data-value="full"]').classList.contains('is-selected')
    && !host().querySelector('.pm-opt[data-value="short"]').classList.contains('is-selected'));
  _pmPick(host().querySelector('.pm-seg-btn[data-value="en"]'));
  check('seg_writes_select', host().querySelector('#podcastLangSel').value === 'en');
  check('seg_moves_selection',
    host().querySelector('.pm-seg-btn[data-value="en"]').classList.contains('is-selected')
    && !host().querySelector('.pm-seg-btn[data-value="zh"]').classList.contains('is-selected'));
  await _podcastGenerate();
  check('generate_reads_picked_values',
    !!apiState.startBody && apiState.startBody.mode === 'full' && apiState.startBody.lang === 'en');
  check('generating_console', !!host().querySelector('.paper-podcast-card.pm-console')
    && !!host().querySelector('.pm-eq')
    && host().innerHTML.includes('MAKING_TITLE'));
  check('console_abort_wired',
    !!host().querySelector('.pm-console-abort')
    && host().querySelector('.pm-console-abort').getAttribute('onclick') === '_podcastAbort()');
  _pcStopPolling();

  // ── Case C: done → now-playing bar + spinning disc on play ──
  _podcast.status = 'done';
  _podcast.data = DONE_BODY;
  _pcRender();
  const wrap = host().querySelector('#podcastPlayerWrap');
  check('player_bar', !!wrap && wrap.classList.contains('pm-player')
    && !!wrap.querySelector('#podcastAudio') && !!wrap.querySelector('.pm-player-disc'));
  check('transcript_head', host().innerHTML.includes('TRANSCRIPT_TITLE'));
  const audio = wrap.querySelector('#podcastAudio');
  audio.dispatchEvent(new win.Event('play'));
  check('play_spins_disc', wrap.classList.contains('is-playing'));
  audio.dispatchEvent(new win.Event('pause'));
  check('pause_stills_disc', !wrap.classList.contains('is-playing'));

  console.log(out.join('\n'));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""


_VIDEO_HARNESS = r"""
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
  'paper.videoStudioTitle': 'VSTUDIO_TITLE',
  'paper.videoHint': 'VHINT_TEXT',
  'paper.videoGenerate': 'VGENERATE_TEXT',
  'paper.videoMakingTitle': 'VMAKING_TITLE',
  'paper.videoQualityDraft': 'Q_DRAFT',
  'paper.videoQualityStandard': 'Q_STANDARD',
  'paper.videoQualityHigh': 'Q_HIGH',
  'paper.videoQualityStandardSub': 'Q_STD_SUB',
  'paper.videoNarration': 'NARRATION',
  'paper.videoNarrationSub': 'NARR_SUB',
  'paper.videoBurnIn': 'BURNIN',
  'paper.videoBurnInSub': 'BURN_SUB',
  'paper.mediaOptQuality': 'QUALITY_LABEL',
  'paper.mediaOptExtras': 'EXTRAS_LABEL',
  'paper.podcastAbort': 'ABORT_TEXT',
};
win.t = global.t = (k) => T_MAP[k] || k;
win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

const apiState = {
  lookupResp: { ok: true, found: false, report_available: true },
  startBody: null,
};
global.Api = win.Api = {
  motion: {
    status: async () => ({ ok: true, tts_available: true }),
    poll: async () => ({ ok: true, done: false, next_cursor: 0, events: [] }),
    abort: async () => ({}),
    scenes: async () => ({ ok: true, scenes: [] }),
    regenScene: async () => ({ ok: true, task_id: 'regen_1' }),
    fileUrl: (tid, part) => '/file/' + tid + (part ? '?part=' + part : ''),
    sceneFileUrl: (tid, sid) => '/file/' + tid + '/' + sid,
  },
  paper: {
    videoLookup: async () => apiState.lookupResp,
    videoStart: async (b) => { apiState.startBody = b; return { ok: true, task_id: 'motion_x1' }; },
  },
};
win._paperHash = 'hash123abc';
global._paperHash = 'hash123abc';
win._switchPaperTab = () => {};
global._switchPaperTab = win._switchPaperTab;

eval(fs.readFileSync(process.argv[2], 'utf8'));  // paper/video.js (real, shipped)
_PVIDEO_POLL_MS = 1;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
function host() { return document.getElementById('paperVideoContent'); }
async function settle(n) { for (let i = 0; i < n; i++) await new Promise(r => setTimeout(r, 3)); }

(async () => {
  // ── Case A: idle studio console structure ──
  await _initVideoTab();
  check('studio_card', !!host().querySelector('.paper-podcast-card.pm-studio'));
  check('studio_head_video_badge', !!host().querySelector('.pm-studio-badge.is-video')
    && host().innerHTML.includes('VSTUDIO_TITLE'));
  const qCards = host().querySelectorAll('.pm-opt[data-sel="videoQualSel"]');
  check('quality_option_cards', qCards.length === 3
    && host().innerHTML.includes('Q_DRAFT') && host().innerHTML.includes('Q_STANDARD')
    && host().innerHTML.includes('Q_HIGH') && host().innerHTML.includes('Q_STD_SUB'));
  check('quality_default_selected',
    host().querySelector('.pm-opt[data-value="standard"]').classList.contains('is-selected')
    && host().querySelector('#videoQualSel').value === 'standard');
  check('quality_grid_cols3', !!host().querySelector('.pm-options.cols-3'));
  check('lang_segmented_control',
    host().querySelectorAll('.pm-seg-btn[data-sel="videoLangSel"]').length === 2
    && !!host().querySelector('#videoLangSel.pm-sr'));
  // toggles: real checkboxes survive under the CSS switches
  const narr = host().querySelector('#videoNarrChk');
  const burn = host().querySelector('#videoBurnChk');
  check('toggles_keep_real_checkboxes', !!narr && !!burn
    && narr.closest('.pm-toggle') !== null && burn.closest('.pm-toggle') !== null
    && !!narr.closest('.pm-toggle').querySelector('.pm-toggle-track'));
  check('toggle_defaults', narr.checked === true && burn.checked === false);
  check('toggle_labels', host().innerHTML.includes('NARR_SUB')
    && host().innerHTML.includes('BURN_SUB'));
  const cta = host().querySelector('.paper-podcast-btn.pm-cta');
  check('cta_button', !!cta && cta.getAttribute('onclick') === '_videoGenerate()');

  // ── Case B: _pmPick bridges quality card → hidden select → start body ──
  _pmPick(host().querySelector('.pm-opt[data-value="draft"]'));
  check('pick_writes_select', host().querySelector('#videoQualSel').value === 'draft');
  check('pick_moves_selection',
    host().querySelector('.pm-opt[data-value="draft"]').classList.contains('is-selected')
    && !host().querySelector('.pm-opt[data-value="standard"]').classList.contains('is-selected'));
  narr.checked = false;
  await _videoGenerate();
  check('generate_reads_picked_values',
    !!apiState.startBody && apiState.startBody.quality === 'draft'
    && apiState.startBody.narration === false);
  check('generating_console', !!host().querySelector('.paper-podcast-card.pm-console')
    && !!host().querySelector('.pm-renderbar')
    && host().innerHTML.includes('VMAKING_TITLE')
    && !!host().querySelector('#paperVideoGrid'));
  check('console_abort_wired',
    !!host().querySelector('.pm-console-abort')
    && host().querySelector('.pm-console-abort').getAttribute('onclick') === '_videoAbort()');
  _pvStopPolling();

  // ── Case C: done → player inside the studio card ──
  _pvideo.status = 'done';
  _pvideo.result = { final_path: '/job/final.mp4', duration: 6.0, scenes: 0, narrated: true };
  _pvideo._doneTaskId = 'motion_x1';
  _pvRender();
  check('done_studio_card', !!host().querySelector('.paper-podcast-card.pm-studio'));
  check('done_player', !!host().querySelector('.paper-podcast-card.pm-studio .paper-video-player'));

  console.log(out.join('\n'));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _run_harness(harness_src: str, module_path: str, name: str) -> str:
    harness = os.path.join(HERE, f'_{name}_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(harness_src)
    try:
        proc = subprocess.run(
            ['node', harness, module_path, ROOT],
            capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_podcast_studio_console():
    out = _run_harness(_PODCAST_HARNESS, PODCAST_JS, 'studio_podcast')
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'podcast studio failures:\n' + out
    assert out.count('PASS') >= 18, f'expected >=18 PASS lines, got:\n{out}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_video_studio_console():
    out = _run_harness(_VIDEO_HARNESS, VIDEO_JS, 'studio_video')
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'video studio failures:\n' + out
    assert out.count('PASS') >= 15, f'expected >=15 PASS lines, got:\n{out}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_pm_pick_bridge_loadbearing():
    """Amputate the value-write line from _pmPick in a COPY of podcast.js →
    the cards still flip .is-selected (pure CSS class) but the hidden select
    never changes, so generate silently uses the DEFAULT — the
    pick_writes_select / generate_reads_picked_values probes must flip
    FAIL. Proves the bridge line is what makes the cards real controls."""
    src = open(PODCAST_JS, encoding='utf-8').read()
    marker = "    sel.value = btn.getAttribute('data-value');\n"
    assert marker in src, 'pmPick value-write marker not found — test is stale'
    broken = src.replace(marker, '', 1)
    assert broken != src

    tmp = os.path.join(HERE, '_studio_podcast_no_bridge.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(broken)
    try:
        chk = subprocess.run(['node', '--check', tmp], capture_output=True,
                             text=True, timeout=30)
        assert chk.returncode == 0, f'patched JS invalid: {chk.stderr}'
        out = _run_harness(_PODCAST_HARNESS, tmp, 'studio_podcast_neuter')
        assert 'FAIL pick_writes_select' in out, \
            'amputating the value-write did NOT flip the probe:\n' + out
        assert 'FAIL generate_reads_picked_values' in out, \
            'generate must fall back to the default once the bridge is cut:\n' + out
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    assert open(PODCAST_JS, encoding='utf-8').read() == src, 'shipped file modified!'


# ═══ Static guards (no node required) ═══


def test_static_studio_css_classes():
    css = open(os.path.join(ROOT, 'static', 'styles.css'), encoding='utf-8').read()
    for cls in ('.pm-studio', '.pm-console', '.pm-studio-badge',
                '.pm-opt', '.pm-opt.is-selected', '.pm-seg', '.pm-seg-btn',
                '.pm-sr', '.pm-voice-wrap', '.pm-toggle', '.pm-toggle-track',
                '.paper-podcast-btn.pm-cta', '.pm-eq', '.pm-clap',
                '.pm-renderbar', '.pm-player', '.pm-transcript-head',
                '.paper-video-content', '.paper-video-player',
                '.paper-video-grid', '.paper-video-grid-row',
                '.paper-video-cell', '.paper-video-thumb',
                '.paper-video-thumb-empty', '.paper-video-regen'):
        assert cls in css, f'CSS class {cls} missing from styles.css'


def test_static_studio_i18n_keys():
    src = open(os.path.join(ROOT, 'static', 'js', 'i18n.js'), encoding='utf-8').read()
    keys = ('paper.podcastStudioTitle', 'paper.videoStudioTitle',
            'paper.podcastMakingTitle', 'paper.videoMakingTitle',
            'paper.mediaOptDuration', 'paper.mediaOptLang',
            'paper.mediaOptVoice', 'paper.mediaOptQuality',
            'paper.mediaOptExtras', 'paper.mediaOptional',
            'paper.podcastModeShortName', 'paper.podcastModeShortSub',
            'paper.podcastModeFullName', 'paper.podcastModeFullSub',
            'paper.videoQualityDraftSub', 'paper.videoQualityStandardSub',
            'paper.videoQualityHighSub', 'paper.videoNarrationSub',
            'paper.videoBurnInSub', 'paper.podcastTranscriptTitle')
    for key in keys:
        m = re.search(re.escape(f"'{key}'") +
                      r":\s*\{\s*zh:\s*'((?:[^'\\]|\\.)*)',\s*en:\s*'((?:[^'\\]|\\.)*)'\s*\}",
                      src)
        assert m, f'i18n key {key} missing'
        assert m.group(1).strip() and m.group(2).strip(), \
            f'i18n key {key} has an empty zh or en'


def test_static_js_syntax():
    if not shutil.which('node'):
        pytest.skip('node not installed')
    for path in (PODCAST_JS, VIDEO_JS):
        proc = subprocess.run(['node', '--check', path],
                              capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, f'{path} syntax: {proc.stderr}'


if __name__ == '__main__':
    import sys
    test_static_studio_css_classes()
    test_static_studio_i18n_keys()
    if _node_deps_available():
        test_static_js_syntax()
        test_podcast_studio_console()
        test_video_studio_console()
        test_NEUTER_pm_pick_bridge_loadbearing()
    else:
        print('SKIP jsdom cases — node + jsdom not available')
    print('ALL PASSED')
    sys.exit(0)
