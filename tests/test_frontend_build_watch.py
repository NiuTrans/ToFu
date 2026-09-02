"""Regression harness for the build watch (long-lived-tab handshake).

WHY
---
A tab keeps running the vite bundle it was loaded with indefinitely — the
``vite:preloadError`` reload only fires when a lazy chunk 404s, so a tab that
never hits a missing chunk runs yesterday's JS forever. Measured symptom
(2026-08-19/20): the sidebar 今天→昨天→今天 date-group interleave was fixed in
d1feed32, yet the user still saw it because their days-old tab ran the pre-fix
bundle. The build watch polls ``/api/health``'s new ``buildId`` and
hard-reloads when the disk build differs.

The reload must be:
  • IDLE-GATED   — never yank the page mid-stream / mid-draft;
  • LOOP-GUARDED — one attempt per build id per tab session, so an upstream
                   cache serving a stale index.html can't spin the tab;
  • FAIL-QUIET   — no buildId (old server) / no entry script tag → never reload.

This harness evals the REAL build-watch block extracted from
``frontend/src/runtime/app-runtime.js`` (main.js section) under jsdom and
drives ``_buildWatchTick()`` with a mocked ``Api.health.info``.
Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import re

import pytest

from tests._jsdom import run_harness
from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit


def _build_watch_block() -> str:
    src = open(runtime_section_path('main.js'), encoding='utf-8').read()
    start = src.index('let _buildWatchTimer')
    end = src.index('// ── Event bindings ──')
    block = src[start:end]
    assert 'function _buildWatchTick' in block
    assert '_reloadPage();' in block
    return block


_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const BUILD_WATCH = process.argv[4];
const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body>' +
        '<script type="module" src="static/vite/assets/main-AAA111.js"></script>' +
        '<textarea id="userInput"></textarea></body>',
  targets: [process.argv[2]],   // the extracted build-watch block
  globals: {
    activeStreams: new Map(),
    conversations: [],
    convIsBusy: c => globalThis.activeStreams.has(c.id) || !!c.activeTaskId,
    t: (k) => k,
    showToast: (...a) => { globalThis.__toasts.push(a); },
  },
});

/* The shared harness doesn't publish the jsdom window's sessionStorage as a
 * bare global (and win.sessionStorage is a read-only accessor, so it can't go
 * through `globals`). The loop guard reads bare `sessionStorage` at tick
 * time — bridge it here so the guard really bites (it fails OPEN on error). */
globalThis.sessionStorage = window.sessionStorage;

globalThis.__toasts = [];
let reloads = 0;
globalThis._reloadPage = () => { reloads++; };

let serverBuild = 'main-AAA111.js';
globalThis.Api = { health: { info: async () => ({ ok: true, buildId: serverBuild }) } };

(async () => {
  // ── A: same build → no reload, no toast. ──
  await _buildWatchTick();
  check('A.same-build-no-reload', reloads === 0 && __toasts.length === 0);

  // ── B: newer build, idle → reload exactly once; loop guard blocks a 2nd. ──
  serverBuild = 'main-BBB222.js';
  await _buildWatchTick();
  check('B.new-build-idle-reloads', reloads === 1 && __toasts.length === 0);
  await _buildWatchTick();
  check('B.loop-guard-no-second-reload', reloads === 1);

  // ── C: newer build while streaming → defer + one toast; idle later → reload. ──
  sessionStorage.clear();
  reloads = 0; __toasts.length = 0;
  serverBuild = 'main-CCC333.js';
  conversations = [{ id: 'conv1' }];
  activeStreams.set('conv1', {});
  await _buildWatchTick();
  await _buildWatchTick();   // still busy — toast only ONCE per build id
  check('C.busy-defers-no-reload', reloads === 0);
  check('C.busy-toasts-once', __toasts.length === 1);
  activeStreams.clear();
  conversations = [];
  await _buildWatchTick();
  check('C.idle-after-busy-reloads', reloads === 1);

  // ── D: composer draft counts as busy. ──
  sessionStorage.clear();
  reloads = 0; __toasts.length = 0;
  serverBuild = 'main-DDD444.js';
  document.getElementById('userInput').value = 'half-typed';
  await _buildWatchTick();
  check('D.composer-draft-defers', reloads === 0 && __toasts.length === 1);
  document.getElementById('userInput').value = '';
  await _buildWatchTick();
  check('D.cleared-composer-reloads', reloads === 1);

  // ── E: server predates buildId (old build) → never reload. ──
  sessionStorage.clear();
  reloads = 0;
  globalThis.Api = { health: { info: async () => ({ ok: true }) } };
  await _buildWatchTick();
  check('E.no-buildid-no-reload', reloads === 0);

  // ── F: activeTaskId pin (no live stream entry) also counts as busy. ──
  globalThis.Api = { health: { info: async () => ({ ok: true, buildId: 'main-EEE555.js' }) } };
  sessionStorage.clear();
  reloads = 0;
  globalThis.conversations = [{ id: 'c1', activeTaskId: 'task1' }];
  await _buildWatchTick();
  check('F.activetask-defers', reloads === 0);
  globalThis.conversations = [];

  // ── G: the busy defer is BOUNDED — a permanently-busy tab (a stale
  //    activeTaskId pin survives every server restart) must still self-heal
  //    once the defer budget elapses; boot re-attaches any genuinely-live
  //    task, so the reload costs a view flash, never lost work. ──
  sessionStorage.clear();
  reloads = 0; __toasts.length = 0;
  globalThis.Api = { health: { info: async () => ({ ok: true, buildId: 'main-GGG777.js' }) } };
  activeStreams.set('conv9', {});
  conversations = [{ id: 'conv9' }];
  await _buildWatchTick();
  check('G.busy-first-tick-defers', reloads === 0 && __toasts.length === 1);
  const _realNow = Date.now;
  Date.now = () => _realNow() + 31 * 60 * 1000;   // 31 min later, still "busy"
  await _buildWatchTick();
  Date.now = _realNow;
  check('G.wedged-busy-eventually-reloads', reloads === 1);
  activeStreams.clear();
  conversations = [];

  report();
})().catch(e => { console.error('HARNESS EXC', e); report(); });
"""


def test_build_watch_block_is_top_level_in_main_section():
    """The build-watch block must ship at top level of the main.js section
    (an earlier draft nested it inside the init() IIFE, invisible to the
    harness and unpublished)."""
    src = open(runtime_section_path('main.js'), encoding='utf-8').read()
    idx_def = src.index('function _startBuildWatch()')
    idx_init = src.index('(function init() {')
    assert idx_def < idx_init, 'build watch must be defined BEFORE the init IIFE'
    # Top-level = two-space indent is NOT allowed on the function keyword.
    line_start = src.rindex('\n', 0, idx_def) + 1
    assert src[line_start:idx_def] == '', 'build watch must be top-level (no indent)'


def test_boot_wires_build_watch():
    src = open(runtime_section_path('main.js'), encoding='utf-8').read()
    assert "_startBuildWatch === 'function') _startBuildWatch();" in src


def test_build_watch_behaviour():
    import tempfile
    import os
    block = _build_watch_block()
    fd, path = tempfile.mkstemp(suffix='.js', prefix='build-watch-')
    try:
        os.write(fd, block.encode('utf-8'))
        os.close(fd)
        run_harness(
            target_js=path,
            body_js=_BODY,
            min_pass=12,
            label='build watch',
        )
    finally:
        os.unlink(path)
