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

from tests.support.chat_tasks import (
    chat_task_fixture_guard,
    chat_task_registry,
)

import pytest

from tests._runtime_sections import native_module_path, runtime_section_path

pytestmark = pytest.mark.unit


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CLASSIC_RENDERERS_TS = os.path.join(
    ROOT, 'frontend', 'src', 'conversation', 'ui',
    'classic-conversation-renderers.ts')
TURN_STORE_JS = runtime_section_path('main/conversation_turn_store.js')


# ═════════════════════════════════════════════════════════════════════
#  Backend half — the emitters ship detailKey + detailArgs
# ═════════════════════════════════════════════════════════════════════


class TestBackendEmittersShipDetailKey(unittest.TestCase):
    """Every fixed-chrome phase our OWN backend emits carries detailKey."""

    def _last_phase(self, task):
        return [e for e in task['events'] if e.get('type') == 'phase'][-1]

    def test_llm_thinking_round0_carries_generating_response_key(self):
        import lib.tasks_pkg.orchestrator._finalize as orch
        from lib.tasks_pkg.manager.runtime import chat_task_runtime
        task = chat_task_runtime.create(user_id=1)
        orch._emit_tool_round_phase(task, {'tool_calls': []}, 0)
        ev = self._last_phase(task)
        self.assertEqual(ev['phase'], 'llm_thinking')
        self.assertEqual(ev['detailKey'], 'stream.phase.generatingResponse')
        # Legacy English detail must remain for headless clients.
        self.assertEqual(ev['detail'], 'Generating response…')

    def test_llm_thinking_round_n_carries_analyzing_round_key_and_args(self):
        import lib.tasks_pkg.orchestrator._finalize as orch
        from lib.tasks_pkg.manager.runtime import chat_task_runtime
        task = chat_task_runtime.create(user_id=1)
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
        from lib.tasks_pkg.manager import append_event
        from lib.tasks_pkg.manager.runtime import chat_task_runtime
        task = chat_task_runtime.create(user_id=1)
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

    def test_manager_phase_snapshot_forwards_model_route(self):
        from lib.agent_core.events import EventType, build_event
        from lib.tasks_pkg.manager import append_event
        from lib.tasks_pkg.manager.runtime import chat_task_runtime
        task = chat_task_runtime.create(user_id=1)
        route = {
            'selectedModel': 'kimi-k3',
            'resolvedModel': 'deepseek-v4-pro',
            'role': 'worker',
            'tier': 'heavy',
            'kind': 'role_tier',
        }
        append_event(task, build_event(
            EventType.PHASE, phase='working',
            detail='Model routing: kimi-k3 → deepseek-v4-pro',
            detailKey='stream.phase.modelRouted',
            detailArgs={
                'from': 'kimi-k3', 'to': 'deepseek-v4-pro',
                'role': 'worker', 'tier': 'heavy',
            },
            model='deepseek-v4-pro', modelRoute=route,
        ))

        self.assertEqual(task['phase']['model'], 'deepseek-v4-pro')
        self.assertEqual(task['phase']['modelRoute'], route)

    def test_manager_phase_snapshot_omits_missing_keys(self):
        """Backwards-compat: a third-party emit with NO detailKey must not
        surface a spurious empty key in task['phase'] (would confuse the
        detailKey→t() consumer)."""
        from lib.agent_core.events import EventType, build_event
        from lib.tasks_pkg.manager import append_event
        from lib.tasks_pkg.manager.runtime import chat_task_runtime
        task = chat_task_runtime.create(user_id=1)
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
        self.assertIn("'attempt': attempts + 1", src)
        self.assertIn("'max': _REACTIVE_COMPACT_MAX_RETRIES", src)

    def test_waiting_model_phase_carries_i18n_key(self):
        src_path = os.path.join(
            ROOT, 'lib', 'tasks_pkg', 'manager', '_stream.py')
        with open(src_path, encoding='utf-8') as f:
            src = f.read()
        self.assertIn("detailKey='stream.phase.waitingForModel'", src)
        self.assertIn("detailArgs={'model': _model_label}", src)

    def test_swarm_waiting_model_phase_carries_i18n_key(self):
        """The swarm sub-agent emitter must ship the same structured fields
        as the main chat path — a bare English `detail` renders raw in the
        worker bubble and never names the model."""
        src_path = os.path.join(ROOT, 'lib', 'swarm', 'agent.py')
        with open(src_path, encoding='utf-8') as f:
            src = f.read()
        self.assertIn("detailKey='stream.phase.waitingForModel'", src)
        self.assertIn("detailArgs={'model': _model_label}", src)
        self.assertNotIn("'Sent to the model, waiting for it to '", src)

    # ── _on_retry (dispatch retry HUD): the "Retrying… Endpoint unreachable
    #    (kimi-k3, attempt 1)" raw-English leak — must ship structured
    #    detailKey/detailArgs (+ typed reasonKey) so the HUD localizes. ──

    def _drive_retry(self, reason, status_code, model='kimi-k3',
                     retry_context=None):
        """Drive the REAL stream_llm_response with a scripted dispatch that
        fires on_retry once and then succeeds; return the retry PHASE event.

        RED without the fix: no detailKey/detailArgs on the event (the
        NEUTER state is exactly the pre-fix emitter — these assertions fail
        on KeyError)."""
        import threading as _thr
        import lib.tasks_pkg.manager._stream as _mgr

        task = {'id': 'task-retry-i18n', 'convId': 'retry-conv',
                '_userId': 1, 'status': 'running',
                'content': '', 'thinking': '', 'config': {'userId': 1}, 'events': [],
                'toolRounds': [], 'content_lock': _thr.Lock(),
                'events_lock': _thr.Lock()}

        def _fake_dispatch(body, **kwargs):
            cb = kwargs.get('on_retry')
            if cb:
                cb(1, reason=reason, status_code=status_code,
                   **(retry_context or {}))
            return ({'role': 'assistant', 'content': 'ok',
                     'reasoning_content': ''}, 'stop', {})

        _orig = _mgr.dispatch_stream
        _mgr.dispatch_stream = _fake_dispatch
        with chat_task_fixture_guard:
            chat_task_registry[task['id']] = task
        try:
            _mgr.stream_llm_response(
                task, {'model': model,
                       'messages': [{'role': 'user', 'content': 'go'}]},
                tag='R1')
        finally:
            _mgr.dispatch_stream = _orig
            with chat_task_fixture_guard:
                chat_task_registry.pop(task['id'], None)
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
        self.assertIn('claude-opus-4.8', ev['detail'])
        self.assertNotIn('aws.', ev['detail'])

    def test_pool_rescue_names_physical_candidate_without_hold_claim(self):
        ev = self._drive_retry(
            'Upstream error', 503, model='glm-5.3', retry_context={
                'attempt_model': 'gemini-2.5-flash-lite',
                'provider_id': 'google-aigc',
                'strict_model': False,
            })

        self.assertEqual(ev['model'], 'gemini-2.5-flash-lite')
        self.assertEqual(ev['providerId'], 'google-aigc')
        self.assertEqual(ev['dispatchMode'], 'pool_rescue')
        self.assertIn('池救援候选 gemini-2.5-flash-lite', ev['detail'])
        self.assertNotIn('模型保持', ev['detail'])

    def test_reasonkey_resolution_present_in_both_renderers(self):
        """The native HUD forwards structured args and its retained i18n port
        resolves a nested reasonKey before formatting the outer phase."""
        with open(CLASSIC_RENDERERS_TS, encoding='utf-8') as handle:
            renderer = handle.read()
        with open(TURN_STORE_JS, encoding='utf-8') as handle:
            bridge = handle.read()
        self.assertIn('value.detailArgs', renderer)
        self.assertIn('localizedValues?.reasonKey', bridge)
        self.assertIn('localizedValues.reason = reason', bridge)


# ═════════════════════════════════════════════════════════════════════
#  Frontend half — jsdom drives the typed live-status block renderer
# ═════════════════════════════════════════════════════════════════════


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_NATIVE_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const { JSDOM } = require(path.join(process.argv[3], 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>');
global.window = dom.window;
global.document = dom.window.document;
(0, eval)(fs.readFileSync(process.argv[2], 'utf8'));
const neuter = process.argv[4] === 'neuter-reasonkey';
const zh = {
  'stream.phase.retryReason': '重试中…{reason}（{model}，第 {attempt} 次）',
  'stream.phase.retryCooldownWait': '{reason}（{model}）',
  'stream.phase.modelRouted': '模型路由：{from} → {to}（{role}，{tier} 档）',
  'stream.retryReason.endpointUnreachable': '连不上模型服务器',
  'stream.retryReason.waitingBackoff': '等待模型（错误退避中，非限流）',
};
const localize = (key, fallback, values) => {
  const args = { ...(values || {}) };
  if (!neuter && args.reasonKey && zh[args.reasonKey]) args.reason = zh[args.reasonKey];
  let value = zh[key] || fallback;
  for (const [name, replacement] of Object.entries(args)) {
    value = value.replaceAll(`{${name}}`, String(replacement));
  }
  return value;
};
const renderers = global.createClassicConversationRenderers({
  renderSafeMarkdownHtml: value => value,
  localizedText: localize,
});
const out = [];
const check = (name, condition) => out.push(`${condition ? 'PASS' : 'FAIL'} ${name}`);
const node = document.createElement('div');
renderers.renderBlock(node, {kind: 'live-status', blockId: 'live-status', value: {
  phase: 'retrying', detail: 'Retrying… Endpoint unreachable',
  detailKey: 'stream.phase.retryReason', detailArgs: {
    reason: 'Endpoint unreachable',
    reasonKey: 'stream.retryReason.endpointUnreachable',
    model: 'kimi-k3', attempt: 1,
  },
}}, {});
if (neuter) {
  check('NEUTER_raw_english_leaks', node.textContent.includes('Endpoint unreachable'));
  check('NEUTER_zh_cause_absent', !node.textContent.includes('连不上模型服务器'));
} else {
  check('detail_key_preferred', node.textContent.includes('重试中'));
  check('nested_reason_key_localized', node.textContent.includes('连不上模型服务器'));
  check('raw_english_reason_absent', !node.textContent.includes('Endpoint unreachable'));
  renderers.renderBlock(node, {kind: 'live-status', blockId: 'live-status', value: {
    phase: 'retrying', detail: 'Retrying… Waiting for model (retry backoff)',
    detailKey: 'stream.phase.retryCooldownWait', detailArgs: {
      reason: 'Waiting for model (retry backoff)',
      reasonKey: 'stream.retryReason.waitingBackoff',
      model: 'kimi-k3',
    }, attempt: 20,
  }}, {});
  check('cooldown_wait_is_localized',
    node.textContent.includes('等待模型（错误退避中，非限流）（kimi-k3）')
      && !node.textContent.includes('Waiting for model'));
  check('cooldown_wait_does_not_claim_attempt_count',
    !node.textContent.includes('第 20 次') && !node.textContent.includes('attempt 20'));
  renderers.renderBlock(node, {kind: 'live-status', blockId: 'live-status', value: {
    phase: 'plugin_work', detail: 'Plugin detail without a key',
  }}, {});
  check('legacy_detail_fallback', node.textContent.includes('Plugin detail without a key'));
  renderers.renderBlock(node, {kind: 'live-status', blockId: 'live-status', value: {
    phase: 'retrying', detail: 'Retrying after provider throttling',
    modelRoute: {
      selectedModel: 'kimi-k3', resolvedModel: 'deepseek-v4-pro',
      role: 'worker', tier: 'heavy', kind: 'role_tier',
    },
  }}, {});
  check('role_route_is_disclosed',
    node.textContent.includes('模型路由')
      && node.textContent.includes('kimi-k3')
      && node.textContent.includes('deepseek-v4-pro'));
}
console.log(out.join('\n'));
"""


def _run_harness(neuter=False):
    with tempfile.NamedTemporaryFile(
            'w', suffix='.js', delete=False, encoding='utf-8') as handle:
        handle.write(_NATIVE_HARNESS)
        harness = handle.name
    target = native_module_path(
        'classic-conversation-renderers.js', CLASSIC_RENDERERS_TS)
    try:
        proc = subprocess.run(
            ['node', harness,
             target,                                         # argv[2]
             ROOT,                                           # argv[3]
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
    assert output.count('PASS') == 7, output


def test_generic_waiting_key_retired_from_locales():
    """Owner directive 2026-08-20: the context-free “等待中…” stream status is
    RETIRED — the pre-phase window resolves truthful labels through
    _waitingPhaseText() instead. Pin that the key never comes back and that
    every replacement key ships in BOTH locales (a missing key would render
    the raw key string in the HUD)."""
    for locale in ('zh.json', 'en.json'):
        with open(os.path.join(ROOT, 'frontend', 'src', 'i18n', 'locales', locale),
                  encoding='utf-8') as handle:
            data = json.load(handle)
        assert 'stream.phase.waiting' not in data, (
            f'{locale}: stream.phase.waiting must stay retired — the pre-phase '
            'window resolves truthful labels via _waitingPhaseText()')
        for key in ('stream.phase.sendingRequest',
                    'stream.phase.waitingWorkerStatus',
                    'stream.phase.connectingTask',
                    'stream.phase.resumingTask',
                    'stream.phase.planning',
                    'stream.phase.dispatchQueued'):
            assert key in data, f'{locale} is missing {key}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_stream_phase_retry_reasonkey_neuter():
    """NEUTER proof: bypassing reasonKey resolution in the typed renderer
    makes the raw English dispatcher token
    ("Endpoint unreachable") leak back into the HUD — proving the block
    under test is what localizes the retry cause."""
    output = _run_harness(neuter=True)
    assert 'PASS NEUTER_raw_english_leaks' in output, output
    assert 'PASS NEUTER_zh_cause_absent' in output, output


if __name__ == '__main__':
    import pathlib
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from tests._standalone_guard import guard_standalone_storage
    guard_standalone_storage('test_stream_phase_i18n.py')
    unittest.main(verbosity=2)
