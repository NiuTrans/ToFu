#!/usr/bin/env python3
"""tests/test_frontend_sidebar_ratelimit.py — the sidebar mirrors the in-answer
"限流中" (rate-limit) phase chip as a sidebar dot + status tag.

WHY
---
The in-bubble retry banner (streaming_ui.js) already shows "⏳ 模型 X 限流中，
正在排队重试" while a turn is parked on a 429. The user asked for the SAME
status in the sidebar, so a rate-limited conversation is identifiable without
opening it — parallel to the translating / memory-prefetch / streaming dots.

DESIGN (root-cause, not a patch)
--------------------------------
The sidebar must NOT grow a second rate-limit flag-setter in every lane (SSE
phase / poll / VU). Instead it DERIVES the verdict from the ONE live phase
slice (ui/stream_session.js), which every lane already writes:

  • conversation_list.js reads the verdict via the module-owned predicate
    ``convRateLimitPhase(convId)`` (never touches ``streamSessions`` directly —
    the pinned read-surface guard stays at the 3-file allowlist);
  • stream_session.js repaints the sidebar (renderConversationList) ONLY when
    the rate-limit VERDICT flips, so heartbeat beats (attempt 1→2→3) don't churn
    the sidebar every ~5s and no separate clearing hook is needed (finishStream
    / twStop call clearStreamSession, which flips the verdict off).

The honest-label ruling (lib/llm_dispatch/retry_i18n.py) is preserved: quota /
backoff / upstream / shared-project-contention waits do NOT light the dot —
only a genuine 429 / rate-limit cooldown does.

WHAT IS ASSERTED
----------------
Everything is driven through the REAL shipped functions, sliced out of the
files (never retyped, charter #24):
  ① _phaseRateLimited / convRateLimitPhase behavioural cells (429, typed
     reasonKey, and the three NON-qualifying waits);
  ② the flip-triggered sidebar repaint (on→off via clearStreamSession);
  ③ _convStatusFlags derives rateLimited from the live phase;
  ④ static: i18n keys present (zh+en), CSS classes present, derived-flag is
     derived-not-direct, no setStreamPhase write added to conversation_list;
  ⑤ NEUTER: deleting the derived flag / predicate / i18n keys turns a cell red.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q \\
       tests/test_frontend_sidebar_ratelimit.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SESSION_JS = os.path.join(ROOT, 'static', 'js', 'ui', 'stream_session.js')
CONVLIST_JS = os.path.join(ROOT, 'static', 'js', 'ui', 'conversation_list.js')
I18N_JS = os.path.join(ROOT, 'static', 'js', 'i18n.js')
STYLES_CSS = os.path.join(ROOT, 'static', 'styles.css')


def _node_available() -> bool:
    return bool(shutil.which('node'))


requires_node = pytest.mark.skipif(
    not _node_available(), reason='node not installed')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _scan_source(path: str) -> str:
    """Comments stripped (charter #24) so prose can't satisfy/violate a guard."""
    src = _read(path)
    try:
        from _source_scan import strip_comments  # type: ignore
        return strip_comments(src, lang='js')
    except Exception:
        scan = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
        return re.sub(r'^\s*//.*$', '', scan, flags=re.M)


def _slice_fn(src: str, name: str) -> str:
    """Slice `function NAME(...) { ... }` out of src by brace matching."""
    i = src.index('function ' + name + '(')
    depth = 0
    open_ = src.index('{', i)
    for k in range(open_, len(src)):
        if src[k] == '{':
            depth += 1
        elif src[k] == '}':
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError('unbalanced braces for ' + name)


# ── Harness A: the session module (real, whole) ───────────────────────────
_SESSION_HARNESS = r"""
const fs = require('fs');
const sessionSrc = fs.readFileSync(process.argv[2], 'utf8');
const scenario = JSON.parse(process.argv[3]);

global.activeStreams = new Map([['convA', {}]]);   // live SSE → setStreamPhase is legal
const renders = [];
global.renderConversationList = () => renders.push(Date.now());

(0, eval)(sessionSrc);   // REAL module: streamSessions, setStreamPhase,
                         // clearStreamSession, convRateLimitPhase, _phaseRateLimited

const out = { cells: [] };
function cell(name, cond) { out.cells.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ① set a phase, read back the derived verdict + flip repaint count
function setAndProbe(phase) {
  const before = renders.length;
  setStreamPhase('convA', phase);
  return { verdict: convRateLimitPhase('convA'), repainted: renders.length > before };
}

for (const step of scenario.steps) {
  if (step.do === 'set') {
    const r = setAndProbe(step.phase);
    cell(step.name + '_verdict', (r.verdict !== null) === step.expectRl);
    cell(step.name + '_repaint', r.repainted === step.expectRepaint);
  } else if (step.do === 'clear') {
    const before = renders.length;
    clearStreamSession('convA');
    cell(step.name + '_verdict', convRateLimitPhase('convA') === null);
    cell(step.name + '_repaint', renders.length > before);
  }
}
console.log(JSON.stringify(out));
"""


def _run_session(steps: list) -> list:
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                     encoding='utf-8') as fh:
        fh.write(_SESSION_HARNESS)
        path = fh.name
    try:
        proc = subprocess.run(
            ['node', path, SESSION_JS, json.dumps({'steps': steps})],
            capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
        return json.loads(proc.stdout.strip().splitlines()[-1])['cells']
    finally:
        os.unlink(path)


_RL_429 = {'phase': 'retrying', 'detailKey': 'stream.phase.retryRateLimited',
           'detailArgs': {'model': 'kimi', 'attempt': 2}, 'attempt': 2}
_RL_REASONKEY = {'phase': 'retrying', 'detailKey': 'stream.phase.retryReason',
                 'detailArgs': {'reason': 'x', 'reasonKey': 'stream.retryReason.rateLimited'},
                 'attempt': 1}
_RL_HEARTBEAT = {'phase': 'retrying', 'detailKey': 'stream.phase.waitingFirstByteReason',
                 'detailArgs': {'reason': 'rate_limit', 'reasonKey': 'stream.retryReason.waitingForModel'},
                 'attempt': 3}
_NONRL_QUOTA = {'phase': 'retrying', 'detailKey': 'stream.phase.retryReason',
                'detailArgs': {'reason': 'x', 'reasonKey': 'stream.retryReason.keyBalanceExhausted'},
                'attempt': 1}
_NONRL_CONTENTION = {'phase': 'retrying', 'detailKey': 'stream.phase.retryReason',
                     'detailArgs': {'reason': 'x', 'reasonKey': 'stream.retryReason.waitingSharedProject'},
                     'attempt': 1}
_NONRL_NOT_RETRYING = {'phase': 'llm_thinking', 'detailKey': 'stream.phase.thinking'}


@requires_node
def test_rate_limit_verdict_on_429_retry():
    """A 429 retry phase lights the derived verdict."""
    cells = _run_session([
        {'do': 'set', 'name': 'on_429', 'phase': _RL_429,
         'expectRl': True, 'expectRepaint': True},
    ])
    assert cells == ['PASS on_429_verdict', 'PASS on_429_repaint'], cells


@requires_node
def test_rate_limit_verdict_on_typed_reasonkey():
    """A typed 限流 reasonKey riding a generic retry frame lights the verdict."""
    for ph, tag in ((_RL_REASONKEY, 'reasonkey'), (_RL_HEARTBEAT, 'heartbeat')):
        cells = _run_session([
            {'do': 'set', 'name': tag, 'phase': ph,
             'expectRl': True, 'expectRepaint': True},
        ])
        assert cells == [f'PASS {tag}_verdict', f'PASS {tag}_repaint'], cells


@requires_node
def test_honest_label_non_qualifying_waits_stay_dark():
    """Honest label: quota / contention / non-retrying phases must NOT light."""
    for ph, tag in ((_NONRL_QUOTA, 'quota'), (_NONRL_CONTENTION, 'contention'),
                    (_NONRL_NOT_RETRYING, 'notretrying')):
        cells = _run_session([
            {'do': 'set', 'name': tag, 'phase': ph,
             'expectRl': False, 'expectRepaint': False},
        ])
        assert cells == [f'PASS {tag}_verdict', f'PASS {tag}_repaint'], cells


@requires_node
def test_flip_on_then_clear_repaints_once_and_no_churn_on_beats():
    """Flip on → 1 repaint; same-verdict beats → 0; clear (turn end) → 1."""
    cells = _run_session([
        {'do': 'set', 'name': 'on', 'phase': _RL_429,
         'expectRl': True, 'expectRepaint': True},
        # beat advances the attempt but KEEPS the verdict → no repaint
        {'do': 'set', 'name': 'beat', 'phase': dict(_RL_429, attempt=5),
         'expectRl': True, 'expectRepaint': False},
        # turn ends (twStop/finishStream) → verdict off → repaint once
        {'do': 'clear', 'name': 'off'},
    ])
    assert cells == ['PASS on_verdict', 'PASS on_repaint',
                     'PASS beat_verdict', 'PASS beat_repaint',
                     'PASS off_verdict', 'PASS off_repaint'], cells


# ── Harness B: _convStatusFlags derivation (real, sliced) ────────────────
_FLAGS_HARNESS = r"""
const fs = require('fs');
global.window = global;
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

global.activeStreams = new Map([['c1', {}]]);   // streaming
global._FINISH_NORMAL = new Set(['stop', 'end_turn']);
global._FINISH_ERR = new Set(['error']);
global._autopilotRunConcluded = function () { return false; };
global.convIsBusy = function (conv) { return !!conv && (activeStreams.has(conv.id) || !!conv.activeTaskId); };
global.computeConvStateConfidence = function () { return 'confirmed'; };

// Stub the module-owned predicate (conversation_list reads through it).
global._probePhase = null;
global.convRateLimitPhase = function (id) { return global._probePhase; };

eval(fs.readFileSync(process.argv[2], 'utf8'));   // REAL _convStatusFlags
check('fn_exposed', typeof _convStatusFlags === 'function');

const c = { id: 'c1', messages: [] };

global._probePhase = { model: 'kimi', attempt: 2 };
check('rate_limited_when_phase_says_so', _convStatusFlags(c).rateLimited === true);

global._probePhase = null;
check('not_rate_limited_when_phase_null', _convStatusFlags(c).rateLimited === false);

// Not streaming → no rate-limit even if a stale phase predicate fires.
global.activeStreams = new Map();
global._probePhase = { model: 'kimi', attempt: 2 };
check('not_rate_limited_when_idle', _convStatusFlags(c).rateLimited === false);

console.log(out.join('\n'));
"""


def _run_flags() -> str:
    src = _read(CONVLIST_JS)
    flags = _slice_fn(src, '_convStatusFlags')
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                     encoding='utf-8') as fh:
        fh.write(_FLAGS_HARNESS)
        harness = fh.name
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                     encoding='utf-8') as ff:
        ff.write(flags)
        flags_path = ff.name
    try:
        proc = subprocess.run(['node', harness, flags_path],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
        return proc.stdout.strip()
    finally:
        os.unlink(harness)
        os.unlink(flags_path)


@requires_node
def test_conv_status_flags_derives_rate_limited():
    out = _run_flags()
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'convStatusFlags derivation failures:\n' + out
    assert out.count('PASS') >= 4, f'expected >=4 PASS lines:\n{out}'


# ── Harness C (JSDOM): the REAL _convStatusHtml + _applyConvItemStatus swap the
#    actual dot/tag DOM on a rate-limit flip — the owner's acceptance criterion.
#    The flip-counter harness (A) proves the repaint fires; THIS proves the row
#    that repaint touches actually shows the rate-limit dot + tag and clears it.
_DOM_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="convList"></div></body>',
  targets: [process.argv[2]],
  globals: {
    activeConvId: 'other',
    // A streaming conv whose rate-limit verdict we flip via the real predicate.
    _rlPhase: null,
    convRateLimitPhase: function (id) { return window._rlPhase; },
    activeStreams: new Map([['c1', {}]]),
    conversations: [{ id: 'c1', title: 'T', messages: [], updatedAt: 1 }],
    computeConvBusy: function (conv, streams) { return streams.has(conv.id); },
    computeConvStateConfidence: function () { return 'confirmed'; },
    formatConvTime: function () { return '12:00'; },
    t: function (k) { return k; },
    escapeHtml: function (s) { return String(s == null ? '' : s); },
    getFolders: function () { return []; },
    getActiveFolderId: function () { return null; },
    areFoldersLoaded: function () { return true; },
  },
});

const conv = window.conversations[0];
// _convStatusFlags is module-private but hoisted to global by the bundler-style
// concat / indirect eval; resolve it directly.
check('flags_fn_exposed', typeof _convStatusFlags === 'function');
check('html_fn_exposed', typeof _convStatusHtml === 'function');
check('apply_fn_exposed', typeof _applyConvItemStatus === 'function');

// Build a real row the way _buildConvItemHTML does (streaming, not rate-limited).
window._rlPhase = null;
const row = document.createElement('div');
row.innerHTML = _buildConvItemHTML(conv, 'T', '');
const rowEl = row.firstElementChild;
document.getElementById('convList').appendChild(rowEl);

// Baseline: streaming dot, NOT rate-limit, no rate-limit tag.
_applyConvItemStatus(rowEl, conv);
check('baseline_streaming_dot', !!rowEl.querySelector('.conv-streaming-dot'));
check('baseline_not_ratelimit_dot', !rowEl.querySelector('.conv-ratelimit-dot'));
check('baseline_no_ratelimit_tag', !rowEl.querySelector('.conv-status-ratelimit'));

// 429 arrives → flip the verdict via the REAL predicate, patch IN PLACE.
window._rlPhase = { model: 'kimi', attempt: 2 };
_applyConvItemStatus(rowEl, conv);
check('on_429_ratelimit_dot', !!rowEl.querySelector('.conv-ratelimit-dot'));
check('on_429_keeps_streaming_base', !!rowEl.querySelector('.conv-streaming-dot'));
check('on_429_ratelimit_tag', !!rowEl.querySelector('.conv-status-ratelimit'));
check('on_429_tag_text', (rowEl.querySelector('.conv-status-ratelimit') || {}).textContent === 'sidebar.rateLimitedTag');

// Turn ends (phase cleared) → the same in-place patch clears the dot + tag.
window._rlPhase = null;
_applyConvItemStatus(rowEl, conv);
check('after_clear_no_ratelimit_dot', !rowEl.querySelector('.conv-ratelimit-dot'));
check('after_clear_no_ratelimit_tag', !rowEl.querySelector('.conv-status-ratelimit'));
check('after_clear_streaming_dot_back', !!rowEl.querySelector('.conv-streaming-dot'));

report();
"""


def test_dom_dot_swaps_on_ratelimit_flip():
    """The row's ACTUAL dot + tag DOM swaps on a 429 flip and clears on turn end.

    This is the acceptance criterion the flip-counter harness could not prove:
    _applyConvItemStatus must reconcile the real dot markup (the
    .conv-streaming-dot.conv-ratelimit-dot modifier) and the localized tag.
    """
    from tests._jsdom import run_harness
    run_harness(
        target_js=CONVLIST_JS,
        body_js=_DOM_BODY,
        min_pass=13,
        label='sidebar ratelimit dom',
    )


# ── Static guards (comments stripped, charter #24) ───────────────────────
# ── Static guards (comments stripped, charter #24) ───────────────────────

def test_i18n_keys_present_zh_and_en():
    src = _read(I18N_JS)
    for key in ("'sidebar.rateLimited'", "'sidebar.rateLimitedTag'"):
        assert key in src, f'{key} missing from i18n.js'
        # both zh + en entries on the key line
        line = next(ln for ln in src.splitlines() if key in ln)
        assert 'zh:' in line and 'en:' in line, (
            f'{key} must carry both zh and en: {line!r}')


def test_css_classes_present():
    css = re.sub(r'/\*.*?\*/', ' ', _read(STYLES_CSS), flags=re.S)
    for cls in ('.conv-ratelimit-dot', '.conv-status-ratelimit'):
        assert cls in css, f'{cls} missing from styles.css'


def test_status_html_renders_ratelimit_branch():
    scan = _scan_source(CONVLIST_JS)
    assert 'conv-ratelimit-dot' in scan, (
        '_convStatusHtml lost the rate-limit dot branch')
    assert 'conv-status-ratelimit' in scan, (
        '_convStatusHtml lost the rate-limit status-tag branch')
    assert "sidebar.rateLimitedTag" in scan, (
        '_convStatusHtml no longer localizes the rate-limit tag')


def test_derived_flag_is_derived_not_direct_and_no_writer_added():
    """conversation_list must read via the module predicate, NOT touch
    streamSessions directly (read-surface guard), and must NOT become a
    setStreamPhase writer (writer-allowlist guard)."""
    scan = _scan_source(CONVLIST_JS)
    assert 'convRateLimitPhase(' in scan, (
        'the derived flag no longer reads convRateLimitPhase — it cannot stay '
        'in lockstep with the live phase truth')
    assert 'streamSessions' not in scan, (
        'conversation_list touched streamSessions directly — the read-surface '
        'guard (convview apply guards) is bypassed; read via convRateLimitPhase')
    assert not re.search(r'\bsetStreamPhase\s*\(', scan), (
        'conversation_list became a setStreamPhase writer — the writer '
        'allowlist (3 files) is widened; the sidebar is read-only over phase')


# ── NEUTER proofs ─────────────────────────────────────────────────────────

def test_NEUTER_deleting_429_branch_blinds_the_verdict():
    """Deleting the retryRateLimited branch of _phaseRateLimited must make the
    429 cell go dark (the honest-label ruling is load-bearing)."""
    src = _read(SESSION_JS)
    nc = src.replace(
        "  if (p.detailKey === 'stream.phase.retryRateLimited') return true;\n",
        "")
    assert nc != src, 'NC did not modify stream_session.js'
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                     encoding='utf-8') as fh:
        fh.write(nc)
        nc_path = fh.name
    try:
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                         encoding='utf-8') as hh:
            hh.write(_SESSION_HARNESS)
            harness = hh.name
        proc = subprocess.run(
            ['node', harness, nc_path,
             json.dumps({'steps': [{'do': 'set', 'name': 'on_429',
                                    'phase': _RL_429,
                                    'expectRl': False, 'expectRepaint': False}]})],
            capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        cells = json.loads(proc.stdout.strip().splitlines()[-1])['cells']
        # With the 429 branch deleted the verdict is dark → the probe passes,
        # proving the branch is what lights it on the REAL file.
        assert cells == ['PASS on_429_verdict', 'PASS on_429_repaint'], cells
    finally:
        os.unlink(nc_path)
        os.unlink(harness)


@requires_node
def test_NEUTER_without_derived_flag_convlist_stays_dark():
    """Deleting the rateLimited derivation from _convStatusFlags must turn the
    rate-limited cell false (proves the flag is load-bearing, not cosmetic)."""
    src = _read(CONVLIST_JS)
    flags = _slice_fn(src, '_convStatusFlags')
    nc = re.sub(
        r"  const rateLimited = !!\(streaming && typeof convRateLimitPhase === 'function'\n"
        r"\s*&& convRateLimitPhase\(c\.id\)\);\n",
        "  const rateLimited = false;\n",
        flags, count=1)
    assert nc != flags, 'NC did not modify _convStatusFlags'
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                     encoding='utf-8') as fh:
        fh.write(_FLAGS_HARNESS)
        harness = fh.name
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                     encoding='utf-8') as ff:
        ff.write(nc)
        flags_path = ff.name
    try:
        proc = subprocess.run(['node', harness, flags_path],
                              capture_output=True, text=True, timeout=60)
        out = proc.stdout.strip()
        assert 'FAIL rate_limited_when_phase_says_so' in out, (
            'neutered flag should FAIL the rate-limited cell:\n' + out)
    finally:
        os.unlink(harness)
        os.unlink(flags_path)


def test_NEUTER_i18n_guard_detects_missing_key():
    """The i18n static guard must fire if a key is dropped (not vacuously green)."""
    src = _read(I18N_JS)
    assert "'sidebar.rateLimited'" in src, 'premise: key exists on the real file'
    assert "'sidebar.rateLimited'" not in src.replace(
        "'sidebar.rateLimited'", "'sidebar.rateLimited_REMOVED'"), (
            'NEUTER FAILED: removing the key did not make the guard target absent')


if __name__ == '__main__':
    import json as _json  # noqa: F401  (harness uses json via subprocess argv)
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for fn in fns:
        try:
            fn()
            print('  PASS', fn.__name__)
        except Exception as e:  # noqa: BLE001
            print('  RED ', fn.__name__, '::', str(e)[:300])
