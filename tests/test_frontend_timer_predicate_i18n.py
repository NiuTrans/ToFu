"""tests/test_frontend_timer_predicate_i18n.py — the two owner-reported timer
card defects from the poll-log screenshot, policed under jsdom against the REAL
shipped ``tool_rounds.js`` + ``i18n.js``.

WHY
---
A hybrid timer that AUTO-PROMOTED to a pure shell predicate (condition_kind
``code``) rendered two confusing things in the collapsed poll card
(owner, 2026-07-17):

  1. The per-poll reason was the RAW developer-English backend note
     ``predicate no match (exit=1)`` — untranslated dev jargon inside an
     otherwise-Chinese UI, and showing a Unix exit code instead of a plain
     "not met yet" verdict. FIX: ``_timerPollReasonText`` recognizes the
     ``predicate {matched|no match} (exit=N)`` / ``predicate ambiguous …``
     shapes and renders an i18n'd verdict (timerBlock.predicate*), leaving a
     genuine free-form LLM reason untouched.

  2. The kind badge still said "混合 / hybrid" even after the timer had been
     promoted to pure ``code`` — because ``_timerConditionKind`` was only set
     on the ``started`` event and never refreshed. FIX: the backend now stamps
     ``conditionKind`` on EVERY poll event and the SSE handler refreshes it, so
     a rendered round carrying ``_timerConditionKind='code'`` shows the
     command-based identity + no "Verifier model" row.

This harness renders a timer round via the real ``_renderUnifiedToolLine`` and
asserts the visible verdict text + the kind badge. Two NEUTERs prove each
assertion is load-bearing.
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


# argv: [node, harness, tool_rounds.js, i18n.js, ROOT, mode]
#   mode = live | neuter_reason | neuter_kind
_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const TR = process.argv[2];
const I18N = process.argv[3];
const ROOT = process.argv[4];
const MODE = process.argv[5] || 'live';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.navigator = win.navigator;
global.console = console;
global.localStorage = win.localStorage = { getItem: () => null, setItem: () => {} };

// Load the REAL i18n.js so t() and setLanguage() are the shipped ones.
eval(fs.readFileSync(I18N, 'utf8'));
win.t = global.t = t;
if (typeof setLanguage === 'function') setLanguage('zh');

// Minimal globals the renderer touches.
win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
win.Icon = global.Icon = (name) => '<svg data-icon="' + name + '"></svg>';
win.IconDot = global.IconDot = () => '<svg data-dot></svg>';
win.renderMarkdown = global.renderMarkdown = (s) => '<p>' + global.escapeHtml(s) + '</p>';
win._isRoundSwarm = global._isRoundSwarm = () => false;
win.getActiveConv = global.getActiveConv = () => null;

let trSrc = fs.readFileSync(TR, 'utf8');

if (MODE === 'neuter_reason') {
  // Restore the OLD behaviour: display the raw reason verbatim (drop the
  // normalizer) — proving the normalizer is what removes the dev-English note.
  const before = trSrc;
  trSrc = trSrc.replace(
    'const fullReason = _timerPollReasonText(p, _t);',
    'const fullReason = p.reason || "";');
  if (trSrc === before) { console.log('FAIL neuter_reason_regex_drift'); }
}
if (MODE === 'neuter_kind') {
  // Force isCodeTimer false regardless of _timerConditionKind — proving the
  // kind-badge assertion is driven by the promoted kind, not something else.
  const before = trSrc;
  trSrc = trSrc.replace(
    'const isCodeTimer = condKind === "code";',
    'const isCodeTimer = false;');
  if (trSrc === before) { console.log('FAIL neuter_kind_regex_drift'); }
}

eval(trSrc);
if (win._timerCountdownTicker) { clearInterval(win._timerCountdownTicker); }

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// A promoted hybrid→code timer: kind='code', a not-met predicate poll whose
// backend reason is the raw dev-English note the owner saw.
const round = {
  roundNum: 3,
  status: 'searching',
  toolName: 'timer_create',
  query: 'watch fastText download',
  results: null,
  _timerTimerId: 'tmr_cb975e0a',
  _timerConditionKind: 'code',
  _timerConditionCommand: 'test "$(stat -c %s /tmp/x)" -gt 4000000000',
  _timerPollInterval: 90,
  _timerMaxPolls: 40,
  _timerPolls: [
    { pollNum: 28, decision: 'wait', reason: 'predicate no match (exit=1)', ts: Date.now() },
  ],
};

const container = document.createElement('div');
document.body.appendChild(container);
container.innerHTML = _renderUnifiedToolLine(round, true);
const html = container.innerHTML;

// 1. The raw dev-English note must NOT appear anywhere in the rendered card.
check('no_raw_predicate_note', html.indexOf('predicate no match') < 0);
check('no_bare_exit_token', html.indexOf('exit=1') < 0);

// 2. The translated plain verdict IS shown (zh string from i18n.js) with the
//    exit code substituted.
const zhWait = t('timerBlock.predicateWait').replace('{code}', '1');
check('translated_verdict_shown', html.indexOf(zhWait) >= 0);
check('verdict_not_raw_key', zhWait.indexOf('timerBlock.') < 0);

// 3. Promoted kind → command-based badge + NO "Verifier model (LLM)" row.
check('kind_badge_command_based', html.indexOf(t('timerBlock.kindCode')) >= 0);
check('no_hybrid_badge', html.indexOf(t('timerBlock.kindHybrid')) < 0);
check('decided_by_code_row', html.indexOf(t('timerBlock.deciderCode')) >= 0);
check('no_llm_verifier_row', html.indexOf(t('timerBlock.verifierLLM')) < 0);

// 4. A genuine free-form LLM reason must pass through the normalizer untouched.
const round2 = Object.assign({}, round, {
  _timerConditionKind: 'llm',
  _timerCheckInstruction: 'Is the build green?',
  _timerConditionCommand: '',
  _timerPolls: [{ pollNum: 2, decision: 'wait', reason: 'build still compiling', ts: Date.now() }],
});
const c2 = document.createElement('div');
c2.innerHTML = _renderUnifiedToolLine(round2, true);
check('llm_reason_passthrough', c2.innerHTML.indexOf('build still compiling') >= 0);

console.log(out.join('\n'));
process.exit(0);
"""


def _run(mode: str):
    harness = os.path.join(HERE, '_timer_predicate_i18n_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'ui', 'tool_rounds.js'),  # argv[2]
             os.path.join(JS_DIR, 'i18n.js'),               # argv[3]
             ROOT,                                          # argv[4]
             mode,                                          # argv[5]
             ],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    return proc


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_predicate_poll_row_is_translated_and_kind_reflects_promotion():
    proc = _run('live')
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'timer predicate/i18n failures:\n' + output
    assert output.count('PASS') >= 9, f'expected >=9 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_neuter_raw_reason_leaks_dev_english():
    """NEUTER: display the raw reason verbatim → the dev-English note leaks
    back, proving _timerPollReasonText is what removes it."""
    proc = _run('neuter_reason')
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'FAIL neuter_reason_regex_drift' not in output, (
        'NEUTER regex did not match — the reason-render line drifted:\n' + output)
    assert 'FAIL no_raw_predicate_note' in output, (
        'NEUTER did not surface the leak — the normalizer is NOT load-bearing:\n' + output)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_neuter_kind_ignores_promotion():
    """NEUTER: force isCodeTimer=false → the hybrid badge / LLM verifier row
    come back, proving the badge is driven by the promoted _timerConditionKind."""
    proc = _run('neuter_kind')
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'FAIL neuter_kind_regex_drift' not in output, (
        'NEUTER regex did not match — the isCodeTimer line drifted:\n' + output)
    assert ('FAIL kind_badge_command_based' in output
            or 'FAIL no_llm_verifier_row' in output), (
        'NEUTER did not surface the stale-kind badge — the kind assertion is '
        'NOT load-bearing:\n' + output)


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
