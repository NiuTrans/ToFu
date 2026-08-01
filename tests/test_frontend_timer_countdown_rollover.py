"""Regression: the timer "Next check in Ns" countdown is LIVE and rolls over.

WHY
---
The timer watcher panel shows a "Next check in ~Ns…" hint while it polls in
the background. Two independent bugs were possible:

  1. FROZEN — the hint computed `secs` ONCE at render time and baked it into
     static text. `_syncToolRoundsDOM`'s fingerprint gate (correctly) skips
     re-renders when nothing changed, so the number never counted down. FIX:
     a 1 Hz ticker (`_tickTimerCountdowns`) updates `[data-timer-next]`
     elements in place — mirrors the swarm panel's `[data-sw-start]` ticker.

  2. STUCK-AT-DUE-NOW — unlike swarm's START time (immutable), the timer's
     next-poll TARGET changes every cycle. The backend sends a fresh
     `nextPollTs` on every poll/skip (lib/scheduler/executor/_timer.py). If
     the fingerprint did NOT fold `_timerNextPollTs`, the round would only
     re-render (and refresh its `data-timer-next` attribute) by CORRELATION
     with `_timerPolls.length` changing — an implicit, fragile dependency.
     FIX: `_syncToolRoundsDOM` folds `_timerNextPollTs` (seconds) into the
     fingerprint, so a new target ALWAYS forces the attribute to roll over.

This harness loads the REAL shipped `tool_rounds.js` + `streaming_ui.js`
under jsdom and asserts:
  • the countdown renders with a live `[data-timer-next]` attribute + a
    `.timer-next-poll-txt` span;
  • the 1 Hz ticker rewrites the span text in place (no re-render);
  • a new `nextPollTs` moves the fingerprint AND updates the attribute
    (rollover), even when no other round field changed;
  • NEUTER: with `_timerNextPollTs` removed from the fingerprint, the same
    rollover scenario would NOT move the fingerprint — proving the folded
    field is what guarantees the attribute refresh.

Runs the REAL JS under jsdom; skips cleanly when node + jsdom aren't
installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[4];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.CSS = win.CSS = undefined;
// jsdom defaults visibilityState to 'visible' — the ticker gate needs that.

// ── Stubs for globals the real renderers touch ──
win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
win.t = global.t = (k, d) => (typeof d === 'string' ? d : k + (d && d.n != null ? ':' + d.n : ''));
win.Icon = global.Icon = (name, size) => '<svg data-icon="' + name + '"></svg>';
win.renderMarkdown = global.renderMarkdown = (s) => '<p>' + global.escapeHtml(s) + '</p>';
win._isRoundSwarm = global._isRoundSwarm = (r) => false;

// Load REAL shipped modules. tool_rounds.js first (defines _renderTimerWatcherBlock,
// _timerNextPollText, _tickTimerCountdowns, and installs window._timerCountdownTicker),
// then streaming_ui.js (defines _syncToolRoundsDOM which calls into tool_rounds).
eval(fs.readFileSync(process.argv[2], 'utf8'));  // ui/tool_rounds.js
eval(fs.readFileSync(process.argv[3], 'utf8'));  // ui/streaming_ui.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

check('sync_fn_exposed', typeof _syncToolRoundsDOM === 'function');
check('ticker_fn_exposed', typeof _tickTimerCountdowns === 'function');
check('text_fn_exposed', typeof _timerNextPollText === 'function');
// The module installs a real setInterval on load — cancel it so it can't
// fire mid-test; we call _tickTimerCountdowns() directly instead.
if (win._timerCountdownTicker) { clearInterval(win._timerCountdownTicker); }

function mkTimerRound(nextTsMs, pollCount) {
  // A timer_create round mid-poll: searching, with one poll recorded so the
  // watcher block renders (see _renderUnifiedToolLine timer branch).
  const polls = [];
  for (let i = 1; i <= (pollCount || 1); i++) {
    polls.push({ pollNum: i, decision: 'wait', reason: 'still waiting', ts: 1000 + i });
  }
  return {
    roundNum: 1,
    status: 'searching',
    toolName: 'timer_create',
    query: 'watch job',
    results: null,
    _timerPolls: polls,
    _timerTimerId: 'tmr_abc',
    _timerNextPollTs: nextTsMs,
    _timerPollInterval: 600,
    _timerMaxPolls: 72,
    _timerCheckInstruction: 'check whether the job finished',
  };
}

// ── Scenario 1: countdown renders live (attribute + span present) ──
{
  const container = document.createElement('div');
  document.body.appendChild(container);
  const future = Date.now() + 170 * 1000;   // ~170s out
  const rounds = [mkTimerRound(future, 1)];
  _syncToolRoundsDOM(container, rounds);
  const hint = container.querySelector('.timer-next-poll[data-timer-next]');
  check('s1_hint_rendered', !!hint);
  check('s1_attr_matches', !!hint && +hint.getAttribute('data-timer-next') === future);
  const span = hint && hint.querySelector('.timer-next-poll-txt');
  check('s1_span_present', !!span);
  check('s1_text_has_seconds', !!span && /Next check in ~1\d\ds/.test(span.textContent));
}

// ── Scenario 2: the 1Hz ticker rewrites the span IN PLACE (no re-render) ──
{
  const container = document.createElement('div');
  document.body.appendChild(container);
  // 100s in the future; we can't advance the real clock, so instead move the
  // attribute back in time by 90s and tick — the ticker must recompute from
  // the (now nearer) target and shrink the number, touching ONLY the span.
  const nearTarget = Date.now() + 100 * 1000;
  const rounds = [mkTimerRound(nearTarget, 1)];
  _syncToolRoundsDOM(container, rounds);
  const hint = container.querySelector('.timer-next-poll[data-timer-next]');
  const span = hint.querySelector('.timer-next-poll-txt');
  const before = span.textContent;
  const spanNodeBefore = span;               // identity check: same node
  const panelHtmlBefore = container.querySelector('.timer-watcher-block').outerHTML.length;
  // Simulate 90s of wall-clock elapsing by rewriting the attribute closer.
  hint.setAttribute('data-timer-next', String(Date.now() + 10 * 1000));
  _tickTimerCountdowns();
  const spanAfter = container.querySelector('.timer-next-poll-txt');
  check('s2_same_span_node', spanAfter === spanNodeBefore);   // no re-render, in-place
  check('s2_text_changed', spanAfter.textContent !== before);
  check('s2_text_now_smaller', /~1?\ds/.test(spanAfter.textContent));
}

// ── Scenario 3: a NEW nextPollTs moves the fingerprint AND rolls the
//    attribute over — even though NO other round field changed. ──
{
  const container = document.createElement('div');
  document.body.appendChild(container);
  const t1 = Date.now() + 600 * 1000;
  const rounds = [mkTimerRound(t1, 1)];
  _syncToolRoundsDOM(container, rounds);
  const fp1 = container._roundsFingerprint;
  const attr1 = +container.querySelector('.timer-next-poll[data-timer-next]').getAttribute('data-timer-next');

  // Backend sends the next poll's target — a DIFFERENT nextPollTs. Nothing
  // else about the round changes (same status, same poll count) to isolate
  // that the target alone drives the rollover.
  const t2 = t1 + 600 * 1000;
  rounds[0]._timerNextPollTs = t2;
  _syncToolRoundsDOM(container, rounds);
  const fp2 = container._roundsFingerprint;
  const attr2 = +container.querySelector('.timer-next-poll[data-timer-next]').getAttribute('data-timer-next');

  check('s3_fingerprint_moved', fp1 !== fp2);
  check('s3_attr_rolled_over', attr2 === t2 && attr2 !== attr1);
}

// ── Scenario 4 (NEUTER — proves the folded field is load-bearing): compute
//    the fingerprint the way the OLD code did (WITHOUT _timerNextPollTs) over
//    the exact Scenario-3 rollover. It must NOT move — demonstrating that
//    dropping the fold would reintroduce the stuck-countdown bug. ──
{
  // Re-implement ONLY the timer-relevant fingerprint terms, both with and
  // without the nextPollTs fold, to show the fold is what makes fp move.
  function fpWithout(r) {
    let f = 1;
    f = Math.imul(f, 31) + (r.roundNum | 0);
    f = Math.imul(f, 31) + (r.status === 'searching' ? 1 : 0);
    if (r._timerPolls) f = Math.imul(f, 31) + r._timerPolls.length;
    if (r._timerSkipCount) f = Math.imul(f, 31) + r._timerSkipCount;
    return f;
  }
  function fpWith(r) {
    let f = fpWithout(r);
    if (r._timerNextPollTs) f = Math.imul(f, 31) + ((r._timerNextPollTs / 1000) | 0);
    return f;
  }
  const t1 = Date.now() + 600 * 1000;
  const rA = mkTimerRound(t1, 1);
  const rB = mkTimerRound(t1 + 600 * 1000, 1);   // only nextPollTs differs
  check('s4_neuter_without_fold_fp_stuck', fpWithout(rA) === fpWithout(rB));
  check('s4_with_fold_fp_moves', fpWith(rA) !== fpWith(rB));
}

console.log(out.join('\n'));
// tool_rounds.js installs REAL setInterval tickers on load, and an un-cleared
// interval keeps node's event loop alive forever (the harness then dies on the
// 60s subprocess timeout instead of reporting its results). The clearInterval
// above only covers the ONE handle this test knows by name, so it silently
// stopped being sufficient the moment a second ticker shipped in the same file
// (measured: adding window._cmdTimerTicker hung this harness for the full 60s).
// Exiting explicitly is name-independent, and is the same discipline
// tests/_tool_rounds_wire_parity_harness.js already documents.
process.exit(0);
"""


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_timer_countdown_is_live_and_rolls_over():
    harness = os.path.join(HERE, '_timer_countdown_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'ui', 'tool_rounds.js'),    # argv[2]
             os.path.join(JS_DIR, 'ui', 'streaming_ui.js'),   # argv[3]
             ROOT,                                            # argv[4]
             ],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'timer-countdown failures:\n' + output
    assert output.count('PASS') >= 13, f'expected >=13 PASS lines, got:\n{output}'
