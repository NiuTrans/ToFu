#!/usr/bin/env python3
"""jsdom: paper-media elapsed clocks survive a refresh / tab switch.

The owner-reported symptom: a video job the backend had been running for ten
minutes displayed ``已用 0:03`` after a refresh, because the panel minted its
stopwatch from a local ``Date.now()`` and re-minted it on every re-attach.

Four independent properties are pinned here. They are separate tests because
each fails for a different reason and a single "it works" assertion would let
three of them rot unnoticed:

  1. ELAPSED CONTINUES  — a 600s-old job re-attaches showing 10:00, not 0:00.
  2. STALL SURVIVES     — this is the DANGEROUS half. Re-minting the
     last-activity clock washes an already-silent job into looking healthy,
     erasing the only stall signal the user has. A refresh onto a job silent
     for 10 minutes must still render the stale tint + "still running".
  3. BAD UNITS REJECTED — a seconds-epoch (~1.78e9) or a double-converted
     ms value (~1.78e15) must be IGNORED, not rendered. Neither throws:
     seconds renders a ~50-year elapsed, double-converted renders year 58000.
     Both are strictly worse than the 0:00 they replaced, because 0:00 at
     least looks wrong.
  4. TICKER ALIVE       — `_initVideoTab` opens with `_pvStopPolling()` (which
     stops the 1s ticker) and several branches return before `_pvRender()`
     re-arms it. A correct start instant with a dead ticker is still a frozen
     stopwatch.

Behaviour-asserting: every case drives the REAL module functions against a
stubbed Api and asserts rendered DOM text / live state — no source-text
anchors, so a reasonable rewrite of the implementation keeps these honest.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIDEO_JS = os.path.join(ROOT, 'static', 'js', 'paper', 'video.js')
PODCAST_JS = os.path.join(ROOT, 'static', 'js', 'paper', 'podcast.js')

import pytest  # noqa: E402

pytestmark = pytest.mark.unit


def _node_deps_available():
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


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
  'paper.mediaElapsed': 'elapsed',
  'paper.mediaLastActive': 'last-active',
  'paper.mediaStillRunning': 'STILL_RUNNING',
  'paper.videoPhaseRender': 'PH_RENDER',
};
win.t = global.t = (k) => T_MAP[k] || k;
win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;');
win._paperHash = 'hash123abc';
global._paperHash = 'hash123abc';

const warns = [];
console.warn = (...a) => { warns.push(a.join(' ')); };

const apiState = {
  lookupResp: { ok: true, found: false, report_available: true },
  pollResp: { ok: true, done: false, next_cursor: 1, events: [] },
};
global.Api = win.Api = {
  motion: {
    status: async () => ({ ok: true, tts_available: true }),
    poll: async () => apiState.pollResp,
    abort: async () => ({}),
    scenes: async () => ({ ok: true, scenes: [] }),
    regenScene: async () => ({ ok: true, task_id: 'r1' }),
    fileUrl: (tid) => '/file/' + tid,
    sceneFileUrl: (tid, sid) => '/file/' + tid + '/' + sid,
  },
  paper: {
    videoLookup: async () => apiState.lookupResp,
    videoStart: async () => ({ ok: true, task_id: 'motion_x1' }),
  },
};

eval(fs.readFileSync(process.argv[2], 'utf8'));
_PVIDEO_POLL_MS = 1;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
async function settle(n) {
  for (let i = 0; i < n; i++) await new Promise(r => setTimeout(r, 3));
}
function liveLine() { return document.getElementById('videoActivityLine'); }

(async () => {
  const NOW = Date.now();

  /* ── Case 1: a 600s-old running job re-attaches ── */
  apiState.lookupResp = { ok: true, found: true, running: true,
                          task_id: 'motion_old', status: 'running',
                          createdAt: NOW - 600000, updatedAt: NOW - 2000 };
  await _initVideoTab();
  await settle(4);
  _pvStopPoll();                       // freeze polling; keep the state
  _pvRenderActivity();
  const txt1 = (liveLine() || {}).textContent || '';
  // 600s == 10:00. The pre-fix behaviour rendered 0:00.
  check('elapsed_continues_from_server_start', /elapsed 10:0\d/.test(txt1));
  check('elapsed_not_zero_after_reattach', !/elapsed 0:0[0-3]\b/.test(txt1));

  /* ── Case 2: re-attach onto a SILENT job keeps the stall visible ── */
  _pvStopPolling();
  _pvideo.status = 'idle'; _pvideo.taskId = '';
  apiState.lookupResp = { ok: true, found: true, running: true,
                          task_id: 'motion_silent', status: 'running',
                          createdAt: NOW - 900000, updatedAt: NOW - 600000 };
  await _initVideoTab();
  await settle(4);
  _pvStopPoll();
  _pvRenderActivity();
  const el2 = liveLine();
  const txt2 = (el2 || {}).textContent || '';
  check('stall_survives_refresh_is_stale_class',
        !!el2 && el2.classList.contains('is-stale'));
  check('stall_survives_refresh_warning_text',
        txt2.indexOf('STILL_RUNNING') !== -1);
  check('stall_last_active_not_reset', !/last-active 0:0\d/.test(txt2));

  /* ── Case 3: wrong-magnitude clocks are rejected, not rendered ── */
  _pvStopPolling();
  _pvideo.status = 'idle'; _pvideo.taskId = '';
  warns.length = 0;
  apiState.lookupResp = { ok: true, found: true, running: true,
                          task_id: 'motion_secs', status: 'running',
                          createdAt: Math.floor(NOW / 1000),   // SECONDS
                          updatedAt: Math.floor(NOW / 1000) };
  await _initVideoTab();
  await settle(4);
  _pvStopPoll();
  _pvRenderActivity();
  const txt3 = (liveLine() || {}).textContent || '';
  // A seconds value would make elapsed ~50 years = ~29,000,000 minutes.
  const m3 = txt3.match(/elapsed (\d+):/);
  const mins3 = m3 ? parseInt(m3[1], 10) : -1;
  check('seconds_epoch_rejected_not_rendered', mins3 >= 0 && mins3 < 5);
  check('seconds_epoch_warns', warns.some(w => w.indexOf('SECONDS') !== -1));

  _pvStopPolling();
  _pvideo.status = 'idle'; _pvideo.taskId = '';
  warns.length = 0;
  apiState.lookupResp = { ok: true, found: true, running: true,
                          task_id: 'motion_future', status: 'running',
                          createdAt: NOW * 1000,      // double-converted
                          updatedAt: NOW * 1000 };
  await _initVideoTab();
  await settle(4);
  _pvStopPoll();
  _pvRenderActivity();
  const txt4 = (liveLine() || {}).textContent || '';
  const m4 = txt4.match(/elapsed (\d+):/);
  const mins4 = m4 ? parseInt(m4[1], 10) : -1;
  check('future_epoch_rejected_not_rendered', mins4 >= 0 && mins4 < 5);
  check('future_epoch_warns', warns.some(w => w.indexOf('future') !== -1));

  /* ── Case 4: the 1s ticker survives the stop/resume race ──
   *
   * The real failure mode is NOT the happy re-attach (there _pvRender()
   * arms the ticker itself). It is the window where the ticker has been
   * stopped — _initVideoTab()/_pvStopPolling() do exactly that, and several
   * branches return before any re-render — while polling continues. A poll
   * with no phase change resumes through _pvRenderProgress(), which paints
   * the progress line but NEVER re-arms the ticker, so the stopwatch is
   * frozen even though the start instant is now correct. */
  _pvStopPolling();
  _pvideo.status = 'idle'; _pvideo.taskId = '';
  apiState.lookupResp = { ok: true, found: true, running: true,
                          task_id: 'motion_tick', status: 'running',
                          createdAt: NOW - 300000, updatedAt: NOW - 1000 };
  apiState.pollResp = { ok: true, done: false, next_cursor: 9, events: [] };
  await _initVideoTab();
  await settle(4);
  // Simulate the ticker being torn down mid-run (tab switch / re-init)
  // while the task keeps polling.
  _pvStopTick();
  check('ticker_precondition_dead', _pvideo.tickTimer === null);
  await _pvPollOnce();                 // no phase change → _pvRenderProgress
  await settle(2);
  _pvStopPoll();
  check('ticker_alive_after_reattach', _pvideo.tickTimer !== null);
  const before = (liveLine() || {}).textContent || '';
  await new Promise(r => setTimeout(r, 1200));   // real 1s tick must fire
  const after = (liveLine() || {}).textContent || '';
  check('ticker_text_advances', before !== after);
  _pvStopPolling();

  /* ── Case 5: the DISK-FALLBACK re-attach — lookup carries NO clocks ──
   *
   * After a server restart the task is gone from memory, so the lookup falls
   * back to the on-disk manifest. Case 1 only ever fed a lookup that HAD
   * clocks, so this shape — the one the post-restart user actually gets — was
   * never exercised. Two things must hold:
   *   - the first poll's clocks are adopted (the panel cannot stay at 0:00);
   *   - the liveness line is repainted BY THAT POLL, not on the next 1s tick.
   * The ticker is deliberately killed just before the poll so only the poll
   * path can have painted what we assert. */
  _pvStopPolling();
  _pvideo.status = 'idle'; _pvideo.taskId = '';
  _pvideo.genStartedAt = 0; _pvideo.lastEventAt = 0;
  apiState.lookupResp = { ok: true, found: true, running: true,
                          task_id: 'motion_disk', status: 'running' };
  apiState.pollResp = { ok: true, done: false, next_cursor: 3, events: [],
                        createdAt: NOW - 480000, updatedAt: NOW - 4000 };
  await _initVideoTab();
  await settle(4);
  _pvStopTick();                       // only the poll may repaint from here
  await _pvPollOnce();
  await settle(2);                     // far below the 1000ms tick interval
  _pvStopPoll();
  const txt5 = (liveLine() || {}).textContent || '';
  check('clockless_lookup_healed_by_poll_without_a_tick',
        /elapsed 8:0\d/.test(txt5));
  check('clockless_lookup_not_left_at_zero', !/elapsed 0:0\d/.test(txt5));
  _pvStopPolling();

  console.log(out.join('\n'));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""

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
  'paper.mediaElapsed': 'elapsed',
  'paper.mediaLastActive': 'last-active',
  'paper.mediaStillRunning': 'STILL_RUNNING',
};
win.t = global.t = (k) => T_MAP[k] || k;
win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;');
win._paperHash = 'hash123abc';
global._paperHash = 'hash123abc';
win.Blob = global.Blob = function () {};
win.URL.createObjectURL = () => 'blob:x';

const warns = [];
console.warn = (...a) => { warns.push(a.join(' ')); };

const apiState = {
  lookupResp: { ok: true, found: false, report_available: true },
  pollResp: { ok: true, done: false, cursor: 1, events: [] },
};
global.Api = win.Api = {
  paper: {
    podcastStatus: async () => ({ ok: true, tts_available: true, default_voice: 'v' }),
    podcastLookup: async () => apiState.lookupResp,
    podcastPoll: async () => apiState.pollResp,
    podcastStart: async () => ({ ok: true, task_id: 'pc_x1' }),
    podcastAbort: async () => ({}),
  },
};

eval(fs.readFileSync(process.argv[2], 'utf8'));
_PODCAST_POLL_MS = 1;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
async function settle(n) {
  for (let i = 0; i < n; i++) await new Promise(r => setTimeout(r, 3));
}
function liveLine() { return document.getElementById('podcastActivityLine'); }

(async () => {
  const NOW = Date.now();

  apiState.lookupResp = { ok: true, found: true, running: true,
                          task_id: 'pc_old',
                          createdAt: NOW - 600000, updatedAt: NOW - 2000 };
  await _initPodcastTab();
  await settle(4);
  _pcStopPoll();
  _pcRenderActivity();
  const txt1 = (liveLine() || {}).textContent || '';
  check('elapsed_continues_from_server_start', /elapsed 10:0\d/.test(txt1));
  check('elapsed_not_zero_after_reattach', !/elapsed 0:0[0-3]\b/.test(txt1));

  _pcStopPolling();
  _podcast.status = 'idle'; _podcast.taskId = '';
  apiState.lookupResp = { ok: true, found: true, running: true,
                          task_id: 'pc_silent',
                          createdAt: NOW - 900000, updatedAt: NOW - 600000 };
  await _initPodcastTab();
  await settle(4);
  _pcStopPoll();
  _pcRenderActivity();
  const el2 = liveLine();
  const txt2 = (el2 || {}).textContent || '';
  check('stall_survives_refresh_is_stale_class',
        !!el2 && el2.classList.contains('is-stale'));
  check('stall_survives_refresh_warning_text',
        txt2.indexOf('STILL_RUNNING') !== -1);
  check('stall_last_active_not_reset', !/last-active 0:0\d/.test(txt2));

  _pcStopPolling();
  _podcast.status = 'idle'; _podcast.taskId = '';
  warns.length = 0;
  apiState.lookupResp = { ok: true, found: true, running: true,
                          task_id: 'pc_secs',
                          createdAt: Math.floor(NOW / 1000),
                          updatedAt: Math.floor(NOW / 1000) };
  await _initPodcastTab();
  await settle(4);
  _pcStopPoll();
  _pcRenderActivity();
  const txt3 = (liveLine() || {}).textContent || '';
  const m3 = txt3.match(/elapsed (\d+):/);
  const mins3 = m3 ? parseInt(m3[1], 10) : -1;
  check('seconds_epoch_rejected_not_rendered', mins3 >= 0 && mins3 < 5);
  check('seconds_epoch_warns', warns.some(w => w.indexOf('SECONDS') !== -1));

  _pcStopPolling();
  _podcast.status = 'idle'; _podcast.taskId = '';
  apiState.lookupResp = { ok: true, found: true, running: true,
                          task_id: 'pc_tick',
                          createdAt: NOW - 300000, updatedAt: NOW - 1000 };
  apiState.pollResp = { ok: true, done: false, cursor: 9, events: [] };
  await _initPodcastTab();
  await settle(4);
  _pcStopTick();
  check('ticker_precondition_dead', _podcast.tickTimer === null);
  await _pcPollOnce();                 // no phase change → _pcRenderProgress
  await settle(2);
  _pcStopPoll();
  check('ticker_alive_after_reattach', _podcast.tickTimer !== null);
  const before = (liveLine() || {}).textContent || '';
  await new Promise(r => setTimeout(r, 1200));
  const after = (liveLine() || {}).textContent || '';
  check('ticker_text_advances', before !== after);
  _pcStopPolling();

  /* ── Case 5: DISK-FALLBACK re-attach — lookup carries NO clocks ──
   * See the video harness: the post-restart shape was never exercised, and
   * the poll must repaint the liveness line itself rather than leaving the
   * corrected elapsed to the next 1s tick. Ticker killed before the poll. */
  _pcStopPolling();
  _podcast.status = 'idle'; _podcast.taskId = '';
  _podcast.genStartedAt = 0; _podcast.lastEventAt = 0;
  apiState.lookupResp = { ok: true, found: true, running: true,
                          task_id: 'pc_disk' };
  apiState.pollResp = { ok: true, done: false, cursor: 3, events: [],
                        createdAt: NOW - 480000, updatedAt: NOW - 4000 };
  await _initPodcastTab();
  await settle(4);
  _pcStopTick();                       // only the poll may repaint from here
  await _pcPollOnce();
  await settle(2);                     // far below the 1000ms tick interval
  _pcStopPoll();
  const txt5 = (liveLine() || {}).textContent || '';
  check('clockless_lookup_healed_by_poll_without_a_tick',
        /elapsed 8:0\d/.test(txt5));
  check('clockless_lookup_not_left_at_zero', !/elapsed 0:0\d/.test(txt5));
  _pcStopPolling();

  console.log(out.join('\n'));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _run(harness, module_path):
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as f:
        f.write(harness)
        hp = f.name
    try:
        r = subprocess.run(['node', hp, module_path, ROOT],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise AssertionError(f'harness failed:\n{r.stdout}\n{r.stderr}')
        return r.stdout
    finally:
        os.unlink(hp)


@pytest.mark.skipif(not _node_deps_available(), reason='node/jsdom unavailable')
class TestPaperMediaClockReattach(unittest.TestCase):

    def _assert_all_pass(self, out):
        fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
        self.assertFalse(fails, f'failing checks: {fails}\nfull:\n{out}')
        self.assertIn('PASS', out)

    def test_video_clocks_survive_reattach(self):
        self._assert_all_pass(_run(_VIDEO_HARNESS, VIDEO_JS))

    def test_podcast_clocks_survive_reattach(self):
        self._assert_all_pass(_run(_PODCAST_HARNESS, PODCAST_JS))

    # ── NEUTER: removing the seed must break the elapsed contract ──

    def _neuter(self, src_path, old, new, harness, expect_fail):
        src = open(src_path, encoding='utf-8').read()
        self.assertIn(old, src, 'NEUTER anchor missing — re-point it')
        poisoned = src.replace(old, new, 1)
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                         encoding='utf-8') as f:
            f.write(poisoned)
            pp = f.name
        try:
            subprocess.run(['node', '--check', pp], check=True,
                           capture_output=True)
            out = _run(harness, pp)
            self.assertIn(f'FAIL {expect_fail}', out,
                          f'NEUTER did not bite — {expect_fail} still passes:\n{out}')
        finally:
            os.unlink(pp)
        self.assertEqual(open(src_path, encoding='utf-8').read(), src,
                         'shipped file modified!')

    def test_NEUTER_video_seed_removed_breaks_elapsed(self):
        self._neuter(
            VIDEO_JS,
            '        _pmAdoptServerClocks(_pvideo, look);',
            '        /* neutered */',
            _VIDEO_HARNESS,
            'elapsed_continues_from_server_start')

    def test_NEUTER_podcast_seed_removed_breaks_elapsed(self):
        self._neuter(
            PODCAST_JS,
            '      _pmAdoptServerClocks(_podcast, look);',
            '      /* neutered */',
            _PODCAST_HARNESS,
            'elapsed_continues_from_server_start')

    def test_NEUTER_video_unit_guard_removed_renders_absurd_elapsed(self):
        """Dropping the range check must let a seconds epoch through."""
        self._neuter(
            VIDEO_JS,
            '    if (n < 1e12) {',
            '    if (false) {',
            _VIDEO_HARNESS,
            'seconds_epoch_rejected_not_rendered')

    def test_NEUTER_video_ticker_reassert_removed(self):
        """Without the re-assert a re-attached panel keeps a dead ticker."""
        self._neuter(
            VIDEO_JS,
            "    if (_pvideo.status === 'generating') _pvStartTick();",
            '    /* neutered */',
            _VIDEO_HARNESS,
            'ticker_alive_after_reattach')

    # ── NEUTER: the poll must repaint the liveness line itself ──
    #
    # Dropping the repaint leaves the corrected elapsed invisible until the
    # next 1s tick. That is exactly the 0:00 flash a user reports as "the
    # timer restarted", and on the disk-fallback re-attach it is the only
    # thing standing between them and a permanent 0:00.

    def test_NEUTER_video_poll_repaint_removed_leaves_zero_showing(self):
        self._neuter(
            VIDEO_JS,
            '    _pvRenderActivity();\n    _pvSchedulePoll();',
            '    _pvSchedulePoll();',
            _VIDEO_HARNESS,
            'clockless_lookup_healed_by_poll_without_a_tick')

    def test_NEUTER_podcast_poll_repaint_removed_leaves_zero_showing(self):
        self._neuter(
            PODCAST_JS,
            '    _pcRenderActivity();\n    _pcSchedulePoll();',
            '    _pcSchedulePoll();',
            _PODCAST_HARNESS,
            'clockless_lookup_healed_by_poll_without_a_tick')


if __name__ == '__main__':
    unittest.main()
