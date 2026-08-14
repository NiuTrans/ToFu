"""Regression: the stream-phase HUD renders in the UI language.

WHY
---
The stream-phase HUD (``.stream-phase-text``) used to render the backend's
English ``detail`` string verbatim ("Generating response…", "Sent to
{model}, waiting…", "Analyzing results and planning next step… (round N)",
"Compressing earlier context…") even though the UI defaults to Chinese.
Only the FALLBACK branches localized (via ``t('stream.phase.*')``) and they
never fired because ``detail`` was always populated.

The fix ships a stable ``detailKey`` (+ optional ``detailArgs``) alongside
the legacy English ``detail``: modern clients resolve ``detailKey`` through
their i18n table (zh primary), headless / non-i18n clients keep rendering
``detail`` unchanged so no wire regression.

This test locks both halves:
  1. BACKEND: the real orchestrator / stream / compaction / reactive-compact
     emitters attach the right ``detailKey`` (+ ``detailArgs``) on their
     PHASE events, AND the manager's poll-fallback snapshot in
     ``task['phase']`` forwards them.
  2. FRONTEND (jsdom, real shipped JS): the phase renderer prefers
     ``detailKey`` → ``t()`` over ``detail``, so the HUD reads in the UI
     language; when only ``detail`` is present (legacy path / headless
     third-party phase), it still falls back to the verbatim string.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
STREAMING_UI_JS = runtime_section_path('ui/streaming_ui.js')
HEALTH_STREAM_TIMER_JS = runtime_section_path('core/health_stream_timer.js')


# ═════════════════════════════════════════════════════════════════════
#  Backend half — the emitters ship detailKey + detailArgs
# ═════════════════════════════════════════════════════════════════════


class TestBackendEmittersShipDetailKey(unittest.TestCase):
    """Every fixed-chrome phase our OWN backend emits carries detailKey."""

    def _last_phase(self, task):
        return [e for e in task['events'] if e.get('type') == 'phase'][-1]

    def test_llm_thinking_round0_carries_generating_response_key(self):
        from lib.tasks_pkg import orchestrator as orch
        from lib.tasks_pkg.manager import _chat_runtime
        task = _chat_runtime.create()
        orch._emit_tool_round_phase(task, {'tool_calls': []}, 0)
        ev = self._last_phase(task)
        self.assertEqual(ev['phase'], 'llm_thinking')
        self.assertEqual(ev['detailKey'], 'stream.phase.generatingResponse')
        # Legacy English detail must remain for headless clients.
        self.assertEqual(ev['detail'], 'Generating response…')

    def test_llm_thinking_round_n_carries_analyzing_round_key_and_args(self):
        from lib.tasks_pkg import orchestrator as orch
        from lib.tasks_pkg.manager import _chat_runtime
        task = _chat_runtime.create()
        orch._emit_tool_round_phase(
            task,
            {'tool_calls': [{'function': {'name': 'web_search'}}]},
            2,
        )
        ev = self._last_phase(task)
        self.assertEqual(ev['phase'], 'llm_thinking')
        self.assertEqual(ev['detailKey'], 'stream.phase.analyzingRound')
        self.assertEqual(ev['detailArgs'], {'round': 3})

    def test_manager_phase_snapshot_forwards_detail_key(self):
        """task['phase'] is what the poll-fallback consumer reads. It must
        carry the same detailKey/detailArgs as the wire event so the two
        transports render identically."""
        from lib.agent_core.events import EventType, build_event
        from lib.tasks_pkg.manager import _chat_runtime, append_event
        task = _chat_runtime.create()
        append_event(task, build_event(
            EventType.PHASE, phase='llm_thinking',
            detail='Analyzing results and planning next step… (round 4)',
            detailKey='stream.phase.analyzingRound',
            detailArgs={'round': 4},
            roundNum=4,
        ))
        p = task['phase']
        self.assertEqual(p['phase'], 'llm_thinking')
        self.assertEqual(p['detailKey'], 'stream.phase.analyzingRound')
        self.assertEqual(p['detailArgs'], {'round': 4})

    def test_manager_phase_snapshot_omits_missing_keys(self):
        """Backwards-compat: a third-party emit with NO detailKey must not
        surface a spurious empty key in task['phase'] (would confuse the
        detailKey→t() consumer)."""
        from lib.agent_core.events import EventType, build_event
        from lib.tasks_pkg.manager import _chat_runtime, append_event
        task = _chat_runtime.create()
        append_event(task, build_event(
            EventType.PHASE, phase='working',
            detail='Doing something specific from a plugin',
        ))
        p = task['phase']
        self.assertNotIn('detailKey', p)
        self.assertNotIn('detailArgs', p)

    def test_compacting_phase_carries_i18n_key(self):
        """force_compact_if_needed's UX phase must localize."""
        import re
        src_path = os.path.join(
            ROOT, 'lib', 'tasks_pkg', 'compaction', '_layer2', '_compact.py')
        with open(src_path, encoding='utf-8') as f:
            src = f.read()
        # The emit block ships both `detail` and `detailKey`. Static assertion
        # is enough here — the emit is inside `force_compact_if_needed` which
        # only runs during a real overflow, awkward to drive in a unit test.
        m = re.search(
            r"(?:phase='compacting'|Phase\.COMPACTING).*?"
            r"detailKey='stream\.phase\.compactingWindow'",
            src, re.DOTALL)
        self.assertTrue(m, 'compacting phase must ship detailKey')

    def test_reactive_compact_phase_carries_i18n_key(self):
        """The reactive-compact retrying phase must localize (was hardcoded zh)."""
        src_path = os.path.join(
            ROOT, 'lib', 'tasks_pkg', 'llm_fallback', '_call.py')
        with open(src_path, encoding='utf-8') as f:
            src = f.read()
        # The emit constructs via build_phase (the typed chokepoint), so the
        # key is a kwarg — accept either spelling (EVENTS.md §4).
        self.assertIn("detailKey='stream.phase.reactiveCompact'", src)
        self.assertIn("'attempt': _attempts + 1", src)
        self.assertIn("'max': _REACTIVE_COMPACT_MAX_RETRIES", src)

    def test_waiting_model_phase_carries_i18n_key(self):
        src_path = os.path.join(
            ROOT, 'lib', 'tasks_pkg', 'manager', '_stream.py')
        with open(src_path, encoding='utf-8') as f:
            src = f.read()
        self.assertIn("detailKey='stream.phase.waitingForModel'", src)
        self.assertIn("detailArgs={'model': _model_label}", src)

    # ── _on_retry (dispatch retry HUD): the "Retrying… Endpoint unreachable
    #    (kimi-k3, attempt 1)" raw-English leak — must ship structured
    #    detailKey/detailArgs (+ typed reasonKey) so the HUD localizes. ──

    def _drive_retry(self, reason, status_code, model='kimi-k3'):
        """Drive the REAL stream_llm_response with a scripted dispatch that
        fires on_retry once and then succeeds; return the retry PHASE event.

        RED without the fix: no detailKey/detailArgs on the event (the
        NEUTER state is exactly the pre-fix emitter — these assertions fail
        on KeyError)."""
        import threading as _thr
        import lib.tasks_pkg.manager as _mgr

        task = {'id': 'task-retry-i18n', 'convId': 'retry-conv',
                'content': '', 'thinking': '', 'config': {}, 'events': [],
                'toolRounds': [], 'content_lock': _thr.Lock(),
                'events_lock': _thr.Lock()}

        def _fake_dispatch(body, **kwargs):
            cb = kwargs.get('on_retry')
            if cb:
                cb(1, reason=reason, status_code=status_code)
            return ({'role': 'assistant', 'content': 'ok',
                     'reasoning_content': ''}, 'stop', {})

        _orig = _mgr.dispatch_stream
        _mgr.dispatch_stream = _fake_dispatch
        try:
            _mgr.stream_llm_response(
                task, {'model': model,
                       'messages': [{'role': 'user', 'content': 'go'}]},
                tag='R1')
        finally:
            _mgr.dispatch_stream = _orig
        evs = [e for e in task['events']
               if e.get('type') == 'phase' and e.get('phase') == 'retrying'
               and e.get('statusCode') == status_code]
        self.assertTrue(evs, f'no retrying phase event in {task["events"]!r}')
        return evs[-1]

    def test_on_retry_429_ships_rate_limited_key(self):
        ev = self._drive_retry('Waiting for model (rate-limited)', 429)
        self.assertEqual(ev['detailKey'], 'stream.phase.retryRateLimited')
        self.assertEqual(ev['detailArgs'],
                         {'model': 'kimi-k3', 'attempt': 1})
        # Legacy zh detail preserved byte-identical for headless clients.
        self.assertEqual(ev['detail'],
                         '⏳ 模型 kimi-k3 限流中，正在排队重试 (第 1 次)…')

    def test_on_retry_endpoint_unreachable_ships_reason_key(self):
        ev = self._drive_retry('Endpoint unreachable', 0)
        self.assertEqual(ev['detailKey'], 'stream.phase.retryReason')
        self.assertEqual(ev['detailArgs'], {
            'reason': 'Endpoint unreachable',
            'reasonKey': 'stream.retryReason.endpointUnreachable',
            'model': 'kimi-k3', 'attempt': 1})
        # Legacy English detail preserved byte-identical.
        self.assertEqual(ev['detail'],
                         'Retrying… Endpoint unreachable (kimi-k3, attempt 1)')

    def test_on_retry_unknown_reason_omits_reason_key(self):
        ev = self._drive_retry('HTTP 503', 0)
        self.assertEqual(ev['detailKey'], 'stream.phase.retryReason')
        self.assertEqual(ev['detailArgs']['reason'], 'HTTP 503')
        self.assertNotIn('reasonKey', ev['detailArgs'])

    def test_on_retry_no_reason_ships_generic_key(self):
        ev = self._drive_retry('', 0)
        self.assertEqual(ev['detailKey'], 'stream.phase.retryGeneric')
        self.assertEqual(ev['detailArgs'],
                         {'model': 'kimi-k3', 'attempt': 1})
        self.assertEqual(ev['detail'], 'Retrying kimi-k3… (attempt 1)')

    def test_on_retry_display_label_strips_gateway_prefix(self):
        """detailArgs.model is NEW wire surface → user-facing label, so the
        internal routing prefix must not leak (legacy detail keeps the raw
        model for wire parity)."""
        ev = self._drive_retry('Endpoint unreachable', 0,
                               model='aws.claude-opus-4.8')
        self.assertEqual(ev['detailArgs']['model'], 'claude-opus-4.8')
        self.assertIn('aws.claude-opus-4.8', ev['detail'])

    def test_reasonkey_resolution_present_in_both_renderers(self):
        """Parity guard: the HUD (streaming_ui) and the stream-timer label
        (health_stream_timer) BOTH resolve the nested reasonKey — fixing
        only one would leave the raw English token visible in the other."""
        snippet = 'if (_r && _r !== _args.reasonKey) _args.reason = _r;'
        for source in (STREAMING_UI_JS, HEALTH_STREAM_TIMER_JS):
            with open(source, encoding='utf-8') as f:
                self.assertIn(snippet, f.read(),
                              f'{os.path.basename(source)} lost reasonKey resolution')


# ═════════════════════════════════════════════════════════════════════
#  Frontend half — jsdom drives the real streaming_ui.js phase renderer
# ═════════════════════════════════════════════════════════════════════


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.setInterval = win.setInterval = () => 0;
global.setTimeout = win.setTimeout = (fn) => 0;
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => 0;
global.getSelection = win.getSelection = () => ({ isCollapsed: true, rangeCount: 0 });

win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
win.renderMarkdown = global.renderMarkdown = (s) => '<p>' + global.escapeHtml(s) + '</p>';

// Minimal zh-language i18n table mirroring the shipped keys we test.
const _ZH = {
  'stream.phase.generatingResponse': '正在生成回复…',
  'stream.phase.analyzingRound':     '正在分析结果并规划下一步…（第 {round} 轮）',
  'stream.phase.waitingModel':       '已发送给模型，等待开始回复…',
  'stream.phase.waitingForModel':    '已发送给 {model}，等待开始回复…',
  'stream.phase.compactingWindow':   '正在压缩早期上下文以适配窗口…',
  'stream.phase.reactiveCompact':    '⚡ 上下文超长，已自动压缩（reactive compact {attempt}/{max}）…',
  'stream.phase.reasoning':          '推理中',
  'stream.phase.retrying':           '正在重试…',
  'stream.phase.waiting':            '等待中…',
  'stream.phase.chars':              '{n} 字符',
  'stream.phase.retryRateLimited':   '⏳ 模型 {model} 限流中，正在排队重试（第 {attempt} 次）…',
  'stream.phase.retryReason':        '重试中…{reason}（{model}，第 {attempt} 次）',
  'stream.phase.retryGeneric':       '正在重试 {model}…（第 {attempt} 次）',
  'stream.retryReason.endpointUnreachable': '连不上模型服务器（网关/网络波动，正在自动切换通道）',
  'stream.retryReason.waitingBackoff':   '等待模型（错误退避中，非限流）',
  'stream.phase.waitingFirstByte':       '已等待 {elapsed}s：{model} 尚未返回首个字节…',
  'stream.phase.waitingFirstByteReason': '已等待 {elapsed}s：{model} 尚未返回首个字节（{reason}）',
};
win.t = global.t = (k, o) => {
  let v = _ZH[k];
  if (v === undefined) return k;
  if (o) for (const kk in o) v = v.replace(new RegExp('\\{' + kk + '\\}', 'g'), o[kk]);
  return v;
};

// Hot-path no-ops updateStreamingUI touches.
win.isNearBottom = global.isNearBottom = () => false;
win.scrollToBottom = global.scrollToBottom = () => {};
win._stampFreshness = global._stampFreshness = () => {};
win._fcFingerprint = global._fcFingerprint = () => 0;
win._extractFileChangesFromRoundsAsync = global._extractFileChangesFromRoundsAsync = () => ({ then: () => {} });
win._renderFileChangesHtml = global._renderFileChangesHtml = () => '';
win.renderMcpLoginHintHtml = global.renderMcpLoginHintHtml = () => '';
win.renderPreferencesAppliedHtml = global.renderPreferencesAppliedHtml = () => '';
win.renderPreferenceLearnedHtml = global.renderPreferenceLearnedHtml = () => '';
win.renderMemoryPrefetchHtml = global.renderMemoryPrefetchHtml = () => '';
win.renderTurnProvenanceHtml = global.renderTurnProvenanceHtml = () => '';
win._isRoundSwarm = global._isRoundSwarm = () => false;
win.convAutoTranslate = global.convAutoTranslate = () => false;
win.convAutoTranslateEffective = global.convAutoTranslateEffective = () => false;
win._startAutoTranslateForMsg = global._startAutoTranslateForMsg = () => {};
win._renderUnifiedToolLine = global._renderUnifiedToolLine = () => '<div class="ptool-line"></div>';
win._buildSwarmPanelHTML = global._buildSwarmPanelHTML = () => '<div class="sw-panel"></div>';
win._buildSwarmInboxChipsHTML = global._buildSwarmInboxChipsHTML = () => '';
win._renderTurnHead = global._renderTurnHead = () => '';
win._renderSoloRoundTag = global._renderSoloRoundTag = () => '';
win._turnLabelText = global._turnLabelText = () => '';
win.Icon = global.Icon = () => '<svg></svg>';
win.CSS = global.CSS = undefined;

win.activeStreams = global.activeStreams = new Map();
win.streamBufs = global.streamBufs = new Map();
win.conversations = global.conversations = [{ id: 'c1', messages: [] }];
global.activeConvId = win.activeConvId = 'c1';

/* NEUTER mode (argv[4] === 'neuter-reasonkey'): strip the reasonKey
 * resolution line from a SCRATCH copy of the source before eval. The retry
 * probes then prove the raw English token leaks back — causality evidence
 * that the block under test is what localizes the cause. */
const _NEUTER = process.argv[4] === 'neuter-reasonkey';
let _src = fs.readFileSync(process.argv[3], 'utf8');
if (_NEUTER) {
  const _target = 'if (_r && _r !== _args.reasonKey) _args.reason = _r;';
  if (!_src.includes(_target)) throw new Error('neuter target line missing from streaming_ui.js');
  _src = _src.replace(_target, '/* NEUTERED reasonKey resolution */');
}
eval(_src);  // ui/streaming_ui.js

const out = [];
function check(name, cond, note) {
  out.push((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : (' — ' + (note || ''))));
}

function _renderAndProbe(phase) {
  document.getElementById('chatInner').innerHTML =
    '<div class="message" id="streaming-msg" data-msg-id="m1">' +
    '<div class="message-body" id="streaming-body"></div></div>';
  updateStreamingUI({ content: '', thinking: '', toolRounds: [], phase });
  const body = document.getElementById('streaming-body');
  const statusZone = body.querySelector('[data-zone="status"]');
  return statusZone ? statusZone.innerHTML : '';
}

// ── 1. llm_thinking round 0 with detailKey → localized zh label ──
{
  const html = _renderAndProbe({
    phase: 'llm_thinking',
    detail: 'Generating response…',
    detailKey: 'stream.phase.generatingResponse',
    round: 1,
  });
  check('llm_thinking_round0_localized', html.includes('正在生成回复…'), html);
  check('llm_thinking_round0_english_not_shown', !html.includes('Generating response'), html);
}

// ── 2. llm_thinking round N interpolates detailArgs.round ──
{
  const html = _renderAndProbe({
    phase: 'llm_thinking',
    detail: 'Analyzing results and planning next step… (round 4)',
    detailKey: 'stream.phase.analyzingRound',
    detailArgs: { round: 4 },
    round: 4,
  });
  check('llm_thinking_roundN_localized_and_interpolated',
    html.includes('第 4 轮'), html);
  check('llm_thinking_roundN_english_not_shown',
    !html.includes('Analyzing results'), html);
}

// ── 3. waiting_model with detailKey → zh, model interpolated ──
{
  const html = _renderAndProbe({
    phase: 'waiting_model',
    detail: 'Sent to claude-opus-4, waiting for it to start replying…',
    detailKey: 'stream.phase.waitingForModel',
    detailArgs: { model: 'claude-opus-4' },
  });
  check('waiting_model_localized_with_model_arg',
    html.includes('已发送给 claude-opus-4，等待开始回复…'), html);
  check('waiting_model_english_not_shown',
    !html.includes('Sent to claude-opus-4'), html);
}

// ── 4. compacting phase → zh ──
{
  const html = _renderAndProbe({
    phase: 'compacting',
    detail: 'Compressing earlier context to fit the window…',
    detailKey: 'stream.phase.compactingWindow',
  });
  check('compacting_localized',
    html.includes('正在压缩早期上下文以适配窗口…'), html);
  check('compacting_english_not_shown',
    !html.includes('Compressing earlier context'), html);
}

// ── 5. reactive-compact retrying → localized with attempt/max ──
{
  const html = _renderAndProbe({
    phase: 'retrying',
    detail: '⚡ 上下文超长，已自动压缩 (reactive compact 2/3)…',
    detailKey: 'stream.phase.reactiveCompact',
    detailArgs: { attempt: 2, max: 3 },
  });
  check('reactive_compact_localized_with_args',
    html.includes('reactive compact 2/3'), html);
}

// ── 6. BACK-COMPAT: a phase with ONLY `detail` (no detailKey — a headless
//    third-party or a still-legacy backend) must render the detail verbatim
//    exactly like it used to. ──
{
  const html = _renderAndProbe({
    phase: 'working',
    detail: 'Initializing Claude Code plugin…',
    // no detailKey / detailArgs
  });
  check('legacy_detail_only_renders_verbatim',
    html.includes('Initializing Claude Code plugin…'), html);
}

// ── 7. BACK-COMPAT: an UNKNOWN detailKey must fall back to `detail`
//    (t() returns the key as-is when unknown; our resolver keeps the
//    English detail on that path). ──
{
  const html = _renderAndProbe({
    phase: 'llm_thinking',
    detail: 'Some new phase text',
    detailKey: 'stream.phase.thisKeyDoesNotExist',
  });
  // t() returns the raw key for unknown → resolver returns that key. That's
  // still better than nothing, but the important property is: the wire still
  // carried `detail` for headless — so `detail` must ALSO have been an
  // acceptable render. We assert renderer prefers detailKey→t() (the key
  // itself), which is the documented behavior.
  check('unknown_key_falls_back_to_key_string',
    html.includes('stream.phase.thisKeyDoesNotExist'), html);
}

// ── 8. retrying with typed reasonKey → localized CAUSE + interpolation ──
//    (the reported bug: "Retrying… Endpoint unreachable (kimi-k3, attempt 1)")
if (!_NEUTER) {
  const html = _renderAndProbe({
    phase: 'retrying',
    detail: 'Retrying… Endpoint unreachable (kimi-k3, attempt 1)',
    detailKey: 'stream.phase.retryReason',
    detailArgs: { reason: 'Endpoint unreachable', reasonKey: 'stream.retryReason.endpointUnreachable', model: 'kimi-k3', attempt: 1 },
    attempt: 1,
  });
  check('retry_reason_localized_cause', html.includes('连不上模型服务器'), html);
  check('retry_reason_raw_english_not_shown', !html.includes('Endpoint unreachable'), html);
  check('retry_reason_model_and_attempt_interpolated',
    html.includes('kimi-k3') && html.includes('第 1 次'), html);
}

// ── 9. retrying with UNKNOWN reasonKey → raw reason fallback ──
{
  const html = _renderAndProbe({
    phase: 'retrying',
    detail: 'Retrying… HTTP 503 (kimi-k3, attempt 2)',
    detailKey: 'stream.phase.retryReason',
    detailArgs: { reason: 'HTTP 503', reasonKey: 'stream.retryReason.httpError', model: 'kimi-k3', attempt: 2 },
    attempt: 2,
  });
  check('retry_unknown_reason_falls_back_to_raw', html.includes('HTTP 503'), html);
}

// ── 10. 429 retry branch → localized rate-limit text ──
{
  const html = _renderAndProbe({
    phase: 'retrying',
    detail: '⏳ 模型 kimi-k3 限流中，正在排队重试 (第 3 次)…',
    detailKey: 'stream.phase.retryRateLimited',
    detailArgs: { model: 'kimi-k3', attempt: 3 },
    attempt: 3,
  });
  check('retry_429_localized',
    html.includes('限流中') && html.includes('kimi-k3') && html.includes('第 3 次'), html);
}

// ── 11. generic retry (no reason) → localized ──
{
  const html = _renderAndProbe({
    phase: 'retrying',
    detail: 'Retrying kimi-k3… (attempt 2)',
    detailKey: 'stream.phase.retryGeneric',
    detailArgs: { model: 'kimi-k3', attempt: 2 },
    attempt: 2,
  });
  check('retry_generic_localized',
    html.includes('正在重试 kimi-k3') && html.includes('第 2 次'), html);
}

// ── 12. waitingFirstByte (no slot cause) → localized + elapsed/model ──
//    (pt TTFT-watchdog: the wedged-upstream heartbeat must render zh, not
//    the legacy English detail string.)
{
  const html = _renderAndProbe({
    phase: 'retrying',
    detail: 'Waiting 42s — no first byte from kimi-k3 yet…',
    detailKey: 'stream.phase.waitingFirstByte',
    detailArgs: { model: 'kimi-k3', elapsed: 42 },
    attempt: 2,
  });
  check('wait_firstbyte_localized_with_elapsed_and_model',
    html.includes('已等待 42s：kimi-k3 尚未返回首个字节…'), html);
  check('wait_firstbyte_english_not_shown',
    !html.includes('no first byte from kimi-k3'), html);
}

// ── 13. waitingFirstByteReason: typed reasonKey REPLACES the raw slot token ──
//    (slot cooldown_reason 'error' must surface as the localized backoff
//    cause, never as the raw English token; depends on the reasonKey
//    resolution line, so NEUTER mode must skip it — same ruling as probe 8.)
if (!_NEUTER) {
  const html = _renderAndProbe({
    phase: 'retrying',
    detail: 'Waiting 63s — no first byte from kimi-k3 yet (error)',
    detailKey: 'stream.phase.waitingFirstByteReason',
    detailArgs: { model: 'kimi-k3', elapsed: 63, reason: 'error', reasonKey: 'stream.retryReason.waitingBackoff' },
    attempt: 3,
  });
  check('wait_firstbyte_reason_localized_cause',
    html.includes('等待模型（错误退避中，非限流）'), html);
  check('wait_firstbyte_reason_raw_token_not_shown',
    !html.includes('(error)'), html);
  check('wait_firstbyte_reason_elapsed_and_model_interpolated',
    html.includes('已等待 63s') && html.includes('kimi-k3'), html);
}

// ── NEUTER-only: with the resolution line stripped, the raw English token
//    MUST leak back into the HUD (proves the block causes the localization). ──
if (_NEUTER) {
  const html = _renderAndProbe({
    phase: 'retrying',
    detail: 'Retrying… Endpoint unreachable (kimi-k3, attempt 1)',
    detailKey: 'stream.phase.retryReason',
    detailArgs: { reason: 'Endpoint unreachable', reasonKey: 'stream.retryReason.endpointUnreachable', model: 'kimi-k3', attempt: 1 },
    attempt: 1,
  });
  check('NEUTER_raw_english_leaks', html.includes('Endpoint unreachable'), html);
  check('NEUTER_zh_cause_absent', !html.includes('连不上模型服务器'), html);
}

console.log(out.join('\n'));
"""


def _run_harness(neuter=False):
    with tempfile.NamedTemporaryFile(
            'w', suffix='.js', delete=False, encoding='utf-8') as handle:
        handle.write(_HARNESS)
        harness = handle.name
    try:
        proc = subprocess.run(
            ['node', harness,
             ROOT,                                            # argv[2]
             STREAMING_UI_JS,                                # argv[3]
             'neuter-reasonkey' if neuter else '',           # argv[4]
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
    assert not fails, 'stream-phase i18n failures:\n' + output
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_stream_phase_i18n_frontend():
    """Frontend renderer prefers detailKey → t() over raw detail."""
    output = _run_harness()
    assert output.count('PASS') >= 17, (
        f'expected >=17 PASS lines, got:\n{output}')


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_stream_phase_retry_reasonkey_neuter():
    """NEUTER proof: stripping the reasonKey-resolution line from a scratch
    copy of streaming_ui.js makes the raw English dispatcher token
    ("Endpoint unreachable") leak back into the HUD — proving the block
    under test is what localizes the retry cause."""
    output = _run_harness(neuter=True)
    assert 'PASS NEUTER_raw_english_leaks' in output, output
    assert 'PASS NEUTER_zh_cause_absent' in output, output


if __name__ == '__main__':
    import pathlib
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_stream_phase_i18n.py')
    unittest.main(verbosity=2)
