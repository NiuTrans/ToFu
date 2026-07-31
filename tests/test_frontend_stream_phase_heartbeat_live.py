#!/usr/bin/env python3
"""Guard: the first-byte waiting HEARTBEAT repaints LIVE, without a conv switch.

THE BUG (2026-07-27, owner report — "这个 stream phase 文字不是实时更新的吗？为什么
我要切换对话才能更新？" on `已等待 100s：yuju-claude-opus-5-evaDaily 尚未返回首个字节…`):

Two independent defects on the SAME line of text, one per layer:

1. INGRESS (root cause, sse_pipeline.js + streaming_render.js). The `phase`
   event handler rebuilds a NEW object for `setStreamPhase` from an explicit
   field whitelist. That whitelist dropped `attempt` (and `statusCode`/
   `model`). But the backend heartbeat
   (lib/tasks_pkg/manager/_stream.py::_on_waiting) emits phase='retrying'
   every ~20s with attempt=<beat number>, and its source comment states the
   choice is "deliberate and load-bearing: the frontend retrying branch keys
   its DOM refresh on ``attempt``, so each beat actually repaints — a
   constant-phase heartbeat would freeze on the first beat's text." Because
   the field never survived ingress, `phase.attempt` was ALWAYS undefined.

2. RENDER (streaming_ui.js). The status zone repaints only when its
   `data-phase-key` changes. The retrying branch built that key as
   "retry:" + attempt — with attempt permanently undefined→0 the key was the
   CONSTANT string "retry:0" for every beat, so the 20s/40s/…/100s texts were
   computed and then thrown away. Sibling branches (compact:/exec:/working:/
   think:/waiting-model:) keyed on the RAW `detailKey`, which is likewise
   constant while `detailArgs` change — the same latent bug class.

Switching conversations "fixed" it only because that tears the bubble down and
rebuilds it, painting whatever the latest phase happens to be.

THE FIX: pass `attempt` (+statusCode/model, and detailKey/detailArgs on the VU
path) through both ingress whitelists, and key every phase branch on the
RESOLVED TEXT so a changing label always repaints.

This harness drives the REAL ingress reducer and the REAL `updateStreamingUI`
with a 3-beat heartbeat exactly as the backend emits it, and asserts the DOM
text actually advances 20s→40s→60s with no conv switch. NEUTER arms re-break
each layer on a scratch copy and prove the assertions go RED.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q \\
       tests/test_frontend_stream_phase_heartbeat_live.py
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile

import pytest

from tests._jsdom import (
    HERE,
    JS_DIR,
    ROOT,
    node_deps_available,
    run_harness,
)

pytestmark = pytest.mark.unit

STREAMING_UI = os.path.join(JS_DIR, 'ui', 'streaming_ui.js')
SSE_PIPELINE = os.path.join(JS_DIR, 'ui', 'sse_pipeline.js')
STREAMING_RENDER = os.path.join(JS_DIR, 'ui', 'streaming_render.js')
_SHARED_HARNESS_JS = os.path.join(HERE, '_jsdom_harness.js')

# The render-layer fix: the retrying key must fold in the resolved text.
_RENDER_FIX_LINE = (
    '    _phaseKey = "retry:" + (phase.attempt || 0) + ":" + _txt;')

_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);

/* Localized heartbeat labels, byte-copied from static/js/i18n.js so the
 * assertion reads the SAME string production renders. */
const I18N = {
  'stream.phase.waitingFirstByte':
    '已等待 {elapsed}s：{model} 尚未返回首个字节…',
  'stream.phase.retrying': '正在重试…',
};

const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner"></div></div></body>',
  targets: [process.argv[2]],
  globals: {
    isNearBottom: () => true,
    scrollToBottom: () => {},
    _syncToolRoundsDOM: () => {},
    _buildSwarmInboxChipsHTML: () => '',
    Icon: () => '<svg></svg>',
    t: (key, args) => {
      let s = I18N[key];
      if (s === undefined) return key;
      if (args) for (const k of Object.keys(args)) {
        s = s.split('{' + k + '}').join(String(args[k]));
      }
      return s;
    },
  },
});

/* The streaming bubble, with a status zone present (the phase paint target).
 * Seeds every zone updateStreamingUI dereferences unconditionally (thinking
 * and fc are read without a null guard on the paint path). */
document.getElementById('chatInner').innerHTML =
  '<div class="message streaming-message" id="streaming-msg">' +
    '<div class="message-body" id="streaming-body">' +
      '<div data-zone="content" class="stream-content"></div>' +
      '<div data-zone="thinking" class="stream-thinking" style="display:none"></div>' +
      '<div data-zone="tool" class="stream-tool"></div>' +
      '<div data-zone="fc" class="stream-fc"></div>' +
      '<div data-zone="status" class="stream-status"></div>' +
    '</div>' +
  '</div>';
const body = document.getElementById('streaming-body');
const statusZone = body.querySelector('[data-zone="status"]');

/* ── The INGRESS reducer, extracted verbatim from the production source ──
 * We don't eval all of sse_pipeline.js (it needs the whole app substrate);
 * instead we PIN the real whitelist by slicing the `setStreamPhase(convId, {…})`
 * object literal out of the phase branch and building it here. If a future
 * edit drops a field from that literal, this harness picks the drop up. */
const fs = require('fs');
const path = require('path');
const pipelinePath = process.env.PIPELINE_JS
  || path.join(process.argv[3], 'static', 'js', 'ui', 'sse_pipeline.js');
const pipelineSrc = fs.readFileSync(pipelinePath, 'utf-8');
const phaseIdx = pipelineSrc.indexOf('} else if (ev.type === "phase") {');
check('ingress_phase_branch_found', phaseIdx > 0);
const callIdx = pipelineSrc.indexOf('setStreamPhase(convId, {', phaseIdx);
const endIdx = pipelineSrc.indexOf('});', callIdx);
check('ingress_setStreamPhase_call_found', callIdx > 0 && endIdx > callIdx);
const literal = pipelineSrc.slice(
  callIdx + 'setStreamPhase(convId, '.length, endIdx + 1);
/* Build the real reducer from the real literal. */
const reduceIngress = new Function('ev', 'return ' + literal + ';');

/* Exactly what lib/tasks_pkg/manager/_stream.py::_on_waiting emits per beat
 * (FIRST_BYTE_HEARTBEAT_S=20 → attempt = elapsed // 20). */
function heartbeatEvent(elapsedSec) {
  const model = 'yuju-claude-opus-5-evaDaily';
  return {
    type: 'phase',
    phase: 'retrying',
    detail: `Waiting ${elapsedSec}s — no first byte from ${model} yet…`,
    detailKey: 'stream.phase.waitingFirstByte',
    detailArgs: { model, elapsed: elapsedSec },
    attempt: Math.max(1, Math.floor(elapsedSec / 20)),
    model,
  };
}

function beat(elapsedSec) {
  const phase = reduceIngress(heartbeatEvent(elapsedSec));
  updateStreamingUI({ toolRounds: [], content: '', thinking: '', phase });
  return statusZone.textContent || '';
}

/* 1. INGRESS: `attempt` must survive the whitelist — the backend's documented
 *    repaint contract depends on it. */
const reduced = reduceIngress(heartbeatEvent(40));
check('ingress_preserves_attempt', reduced.attempt === 2);
check('ingress_preserves_detailKey',
      reduced.detailKey === 'stream.phase.waitingFirstByte');
check('ingress_preserves_detailArgs_elapsed',
      !!reduced.detailArgs && reduced.detailArgs.elapsed === 40);

/* 2. RENDER: three consecutive beats must each repaint the LIVE seconds. */
const t20 = beat(20);
const t40 = beat(40);
const t60 = beat(60);
check('beat20_painted', t20.indexOf('已等待 20s') >= 0);
check('beat40_repainted_live', t40.indexOf('已等待 40s') >= 0);
check('beat60_repainted_live', t60.indexOf('已等待 60s') >= 0);
/* The frozen-text signature of the bug: still showing the FIRST beat. */
check('beat60_is_not_frozen_on_first_beat', t60.indexOf('已等待 20s') < 0);

/* 3. The sibling latent class: a phase whose detailKey is CONSTANT while its
 *    detailArgs change must still repaint (compacting/exec/working/thinking
 *    all keyed on the raw detailKey before this fix). */
function argOnlyBeat(n) {
  updateStreamingUI({
    toolRounds: [], content: '', thinking: '',
    phase: { phase: 'retrying', detailKey: 'stream.phase.waitingFirstByte',
             detailArgs: { model: 'M', elapsed: n }, attempt: 7 },
  });
  return statusZone.textContent || '';
}
const a1 = argOnlyBeat(80);
const a2 = argOnlyBeat(100);
check('same_attempt_changed_args_still_repaints',
      a1.indexOf('已等待 80s') >= 0 && a2.indexOf('已等待 100s') >= 0);

report();
"""


def _run_raw(streaming_ui_path: str, pipeline_path: str) -> str:
    """Run the harness without asserting — for the NEUTER arms."""
    if not node_deps_available():
        pytest.skip('node + jsdom dev-deps not installed (run `npm install`)')
    with tempfile.NamedTemporaryFile(
        'w', suffix='.js', dir=HERE, delete=False, encoding='utf-8'
    ) as fh:
        harness_path = fh.name
        fh.write(_BODY)
    try:
        proc = subprocess.run(
            ['node', harness_path, streaming_ui_path, ROOT],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, 'JSDOM_HARNESS': _SHARED_HARNESS_JS,
                 'PIPELINE_JS': pipeline_path},
        )
    finally:
        try:
            os.remove(harness_path)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}'
    return (proc.stdout or '').strip()


def _scratch(src: str) -> str:
    with tempfile.NamedTemporaryFile(
        'w', suffix='.js', dir=HERE, delete=False, encoding='utf-8'
    ) as fh:
        fh.write(src)
        return fh.name


def test_heartbeat_repaints_live_without_conv_switch():
    """Fixed code: ingress keeps `attempt`, and every beat repaints live."""
    output = run_harness(
        target_js=STREAMING_UI,
        body_js=_BODY,
        min_pass=10,
        label='stream-phase-heartbeat-live',
    )
    for name in (
        'ingress_phase_branch_found',
        'ingress_setStreamPhase_call_found',
        'ingress_preserves_attempt',
        'ingress_preserves_detailKey',
        'ingress_preserves_detailArgs_elapsed',
        'beat20_painted',
        'beat40_repainted_live',
        'beat60_repainted_live',
        'beat60_is_not_frozen_on_first_beat',
        'same_attempt_changed_args_still_repaints',
    ):
        assert f'PASS {name}' in output, output


def test_NEUTER_ingress_attempt_drop_is_load_bearing():
    """NEUTER layer 1 ALONE: drop `attempt` from the ingress whitelist.

    The ingress field check goes red — that is the whole assertion. The
    heartbeat still REPAINTS, and that is the correct, deliberate result: the
    render-layer text fold (layer 2) is defence in depth and independently
    saves the paint. Asserting a freeze here would be asserting a bug we also
    fixed. `attempt` is still forwarded because the backend's documented
    contract depends on it (and `statusCode` drives the 429 branch); the
    dual-NEUTER below is what proves the ORIGINAL freeze is really gone.
    """
    src = open(SSE_PIPELINE, encoding='utf-8').read()
    line = '        attempt: ev.attempt || 0,\n'
    assert line in src, (
        'ingress no longer forwards `attempt` — re-check the fix.')
    scratch = _scratch(src.replace(line, '', 1))
    try:
        output = _run_raw(STREAMING_UI, scratch)
    finally:
        try:
            os.remove(scratch)
        except OSError:
            pass
    assert 'FAIL ingress_preserves_attempt' in output, (
        'NEUTER ineffective: attempt survived its own removal:\n' + output)


def test_NEUTER_both_layers_reproduces_the_original_freeze():
    """NEUTER layers 1+2 TOGETHER — the exact HEAD~ state the owner hit.

    Ingress drops `attempt` AND the retrying key is attempt-only. Now the key
    is the constant "retry:0" for every beat, so the 20s/40s/60s texts are
    computed and thrown away: the line freezes on the first beat until a
    conversation switch rebuilds the bubble. This is the reproduction that
    proves the guard is pinned to a REAL defect, not a hypothetical.
    """
    pipeline_src = open(SSE_PIPELINE, encoding='utf-8').read()
    ui_src = open(STREAMING_UI, encoding='utf-8').read()
    line = '        attempt: ev.attempt || 0,\n'
    assert line in pipeline_src and _RENDER_FIX_LINE in ui_src
    p_scratch = _scratch(pipeline_src.replace(line, '', 1))
    u_scratch = _scratch(ui_src.replace(
        _RENDER_FIX_LINE,
        '    _phaseKey = "retry:" + (phase.attempt || 0);', 1))
    try:
        output = _run_raw(u_scratch, p_scratch)
    finally:
        for p in (p_scratch, u_scratch):
            try:
                os.remove(p)
            except OSError:
                pass
    assert 'FAIL beat40_repainted_live' in output, (
        'NEUTER ineffective: the heartbeat still repainted with BOTH layers '
        'broken — the guard would pass on the buggy code too:\n' + output)
    assert 'FAIL beat60_is_not_frozen_on_first_beat' in output, (
        'NEUTER ineffective: expected the text frozen on the first beat '
        '(the owner-reported symptom):\n' + output)


def test_NEUTER_render_key_text_fold_is_load_bearing():
    """NEUTER layer 2: revert the retrying key to attempt-only (dropping the
    resolved-text fold) — the arg-only repaint check MUST go red, proving the
    text fold is what saves a constant-attempt label change."""
    src = open(STREAMING_UI, encoding='utf-8').read()
    assert _RENDER_FIX_LINE in src, (
        'render-layer fix line missing from streaming_ui.js — re-check it.')
    neutered = src.replace(
        _RENDER_FIX_LINE,
        '    _phaseKey = "retry:" + (phase.attempt || 0);', 1)
    assert neutered != src
    scratch = _scratch(neutered)
    try:
        output = _run_raw(scratch, SSE_PIPELINE)
    finally:
        try:
            os.remove(scratch)
        except OSError:
            pass
    assert 'FAIL same_attempt_changed_args_still_repaints' in output, (
        'NEUTER ineffective: a constant-attempt label change still repainted '
        'without the text fold:\n' + output)


def _phase_ingress_literal(path: str, anchor: str) -> str:
    """Slice the `setStreamPhase(convId, {…})` object literal out of a phase
    ingress branch, with comments stripped (charter #24).

    Comment stripping matters concretely here: BOTH ingress sites carry
    explanatory prose naming the very fields these assertions look for (e.g.
    "attempt/statusCode/model carry the first-byte heartbeat's beat"), so an
    unstripped scan would be satisfied by a COMMENT — a guard that passes on a
    whitelist that no longer forwards anything.
    """
    src = open(path, encoding='utf-8').read()
    idx = src.index(anchor)
    call = src.index('setStreamPhase(convId, {', idx)
    end = src.index('});', call)
    literal = src[call:end]
    try:
        import sys
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        from _source_scan import strip_comments  # type: ignore
        return strip_comments(literal, lang='js')
    except Exception:
        return re.sub(r'/\*.*?\*/', '', literal, flags=re.S)


def _literal_fields(literal: str) -> set:
    """The field NAMES a (comment-stripped) object literal actually forwards."""
    return set(re.findall(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:', literal, re.M))


_WORKER_ANCHOR = '} else if (ev.type === "phase") {'
_VU_ANCHOR = '} else if (itype === "phase") {'


def test_vu_ingress_matches_worker_ingress_contract():
    """The Autopilot VU phase ingress (streaming_render.js) rebuilds the same
    phase object and had the SAME field drop. Source-scan both whitelists so
    the two can't drift apart again — a heartbeat during a VU turn must carry
    the beat counter and the localized label too."""
    literal = _phase_ingress_literal(STREAMING_RENDER, _VU_ANCHOR)
    for field in ('attempt:', 'detailKey:', 'detailArgs:'):
        assert field in literal, (
            f'VU phase ingress (streaming_render.js) drops `{field}` — the '
            f'heartbeat freezes / renders raw English on Autopilot turns. '
            f'Keep it in parity with the worker ingress in sse_pipeline.js.\n'
            f'{literal}')


def test_both_ingress_paths_carry_the_repaint_contract_fields():
    """PARITY, actually measured across BOTH sides.

    `test_vu_ingress_matches_worker_ingress_contract` promises parity in its
    NAME but only ever reads the VU file — the worker whitelist could drop
    every one of these fields and it would still pass. (That is not
    hypothetical: the worker side really was missing `attempt` when this epic
    was filed, while the VU side still had it, and the "parity" guard was
    green throughout.) So assert the contract on BOTH literals, from one list.

    Scoped deliberately to the fields with a live consumer:
      attempt    → streaming_ui.js retry repaint key (+ backend contract)
      detailKey  → _phaseDetailText localization
      detailArgs → the per-beat varying payload (elapsed / model / attempt)
    `statusCode` and `model` are NOT asserted: measured across the frontend,
    nothing reads `phase.statusCode` or `phase.model` today, so requiring them
    would pin dead weight rather than a behaviour.
    """
    contract = ('attempt', 'detailKey', 'detailArgs')
    worker = _literal_fields(_phase_ingress_literal(SSE_PIPELINE, _WORKER_ANCHOR))
    vu = _literal_fields(_phase_ingress_literal(STREAMING_RENDER, _VU_ANCHOR))
    missing_worker = [f for f in contract if f not in worker]
    missing_vu = [f for f in contract if f not in vu]
    assert not missing_worker, (
        f'worker ingress (sse_pipeline.js) drops {missing_worker} — the retry '
        f'banner freezes on the first beat. forwards: {sorted(worker)}')
    assert not missing_vu, (
        f'VU ingress (streaming_render.js) drops {missing_vu}. '
        f'forwards: {sorted(vu)}')


def test_backend_heartbeat_still_keys_on_attempt():
    """Cross-layer contract: this whole guard is only meaningful while the
    backend heartbeat actually varies `attempt` per beat. If _on_waiting stops
    doing that, the frontend key must be revisited — fail loudly instead of
    silently guarding a dead contract."""
    path = os.path.join(ROOT, 'lib', 'tasks_pkg', 'manager', '_stream.py')
    src = open(path, encoding='utf-8').read()
    assert 'def _on_waiting(' in src, (
        'the first-byte heartbeat emitter is gone — re-evaluate this guard.')
    body = src[src.index('def _on_waiting('):]
    body = body[:body.index('\n    # ── Consume zero-byte')]
    assert re.search(r'attempt=_beat', body), (
        'the heartbeat no longer sends a per-beat `attempt` — the frontend '
        'repaint key depends on it (see streaming_ui.js retrying branch).')
    assert '_beat = max(1, int(elapsed // max(1, _hb))' in body, (
        'the beat counter no longer advances with elapsed time — the phase '
        'line would freeze again even with the frontend fix.')


if __name__ == '__main__':
    for fn in (test_heartbeat_repaints_live_without_conv_switch,
               test_NEUTER_ingress_attempt_drop_is_load_bearing,
               test_NEUTER_render_key_text_fold_is_load_bearing,
               test_vu_ingress_matches_worker_ingress_contract,
               test_backend_heartbeat_still_keys_on_attempt):
        try:
            fn()
            print('  PASS', fn.__name__)
        except Exception as e:  # noqa: BLE001
            print('  RED ', fn.__name__, '::', str(e)[:400])
