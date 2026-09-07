#!/usr/bin/env python3
"""tests/test_gateway_strict_no_fallback.py — strict-mode gateway-5xx rules.

Owner directive 2026-08-18: a 502-class error (RateLimitError with
``is_gateway=True`` — HTTP 502/503/504 plus vendor-transient outages wrapped
in 4xx bodies) is WAITABLE, never a reason to switch models. The dispatcher
already cycles WITHIN the pinned model's slots until the outage budget
expires; after that the failure must surface as an honest upstream_error
envelope — NOT a configured-fallback switch and NOT a pool-wide rescue onto
an arbitrary model. The user interrupts and switches models themselves.

Pins:

  1. retry_i18n — the dispatcher's gateway token ('Upstream error') maps to
     the dedicated ``stream.phase.retryGateway`` HUD key (says "backend
     gateway issue, waiting it out, no model switch").
  2. manager/_stream._on_retry — the legacy zh detail for the gateway token
     names the backend problem + the no-switch promise.
  3. llm_fallback/_call — a gateway error NEVER reaches the fallback switch
     or the pool rescue, at either hop (primary / configured-fallback).
  4. Regression: NON-gateway errors (e.g. PermissionError_) with no fallback
     configured still take the 2026-08-03 pool-rescue path.
  5. Locales — both new keys exist in zh + en.

Run:  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_gateway_strict_no_fallback.py -m unit
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import threading
import unittest

from tests.support.chat_tasks import (
    chat_task_fixture_guard,
    chat_task_registry,
)

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit

from lib.llm_errors import RateLimitError  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _gateway_exc(status=502):
    return RateLimitError(f'HTTP {status}: bad gateway', is_gateway=True,
                          status_code=status)


# ── 1. shared retry-HUD mapping ──────────────────────────────────────

@pytest.mark.unit
class TestRetryPhaseGatewayBranch(unittest.TestCase):

    def test_gateway_token_gets_dedicated_key_with_status(self):
        from lib.llm_dispatch.retry_i18n import retry_phase_fields
        f = retry_phase_fields(model='kimi-k3', attempt=2,
                               reason='Upstream error', status_code=502,
                               legacy_detail='X')
        self.assertEqual(f['detailKey'], 'stream.phase.retryGateway')
        self.assertEqual(f['detailArgs'],
                         {'model': 'kimi-k3', 'attempt': 2, 'status': 502})
        self.assertEqual(f['detail'], 'X')  # legacy passthrough untouched

    def test_gateway_token_without_status_omits_it(self):
        """A vendor-transient wrapped 4xx carries a status that would read as
        an auth error — the arg is optional and omitted when absent."""
        from lib.llm_dispatch.retry_i18n import retry_phase_fields
        f = retry_phase_fields(model='kimi-k3', attempt=1,
                               reason='Upstream error', status_code=0,
                               legacy_detail='X')
        self.assertEqual(f['detailKey'], 'stream.phase.retryGateway')
        self.assertNotIn('status', f['detailArgs'])

    def test_429_still_wins_over_gateway_token(self):
        """Branch-order pin: a real 429 status keeps the rate-limited shape
        even if a caller passed the gateway token."""
        from lib.llm_dispatch.retry_i18n import retry_phase_fields
        f = retry_phase_fields(model='m', attempt=1, reason='Upstream error',
                               status_code=429)
        self.assertEqual(f['detailKey'], 'stream.phase.retryRateLimited')

    def test_reason_token_map_unchanged(self):
        from lib.llm_dispatch.retry_i18n import RETRY_REASON_KEYS
        self.assertEqual(RETRY_REASON_KEYS.get('Upstream error'),
                         'stream.retryReason.upstreamError')


# ── 2. main-chat emitter (_on_retry) ─────────────────────────────────

@pytest.mark.unit
class TestOnRetryGatewayLegacyDetail(unittest.TestCase):
    """Drive the REAL stream_llm_response with a scripted dispatch that
    fires on_retry once with the gateway token, then succeeds."""

    def _drive_retry(self, reason, status_code, model='kimi-k3'):
        import lib.tasks_pkg.manager._stream as _mgr

        task = {'id': 'task-gw-retry', 'convId': 'gw-conv', '_userId': 1,
                'status': 'running', '_transientRuntime': True,
                'content': '', 'thinking': '', 'config': {}, 'events': [],
                'toolRounds': [], 'content_lock': threading.Lock(),
                'events_lock': threading.Lock()}

        def _fake_dispatch(body, **kwargs):
            cb = kwargs.get('on_retry')
            if cb:
                cb(1, reason=reason, status_code=status_code)
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

    def test_gateway_cycle_names_backend_and_no_switch(self):
        ev = self._drive_retry('Upstream error', 502)
        self.assertEqual(ev['detailKey'], 'stream.phase.retryGateway')
        self.assertEqual(ev['detailArgs'],
                         {'model': 'kimi-k3', 'attempt': 1, 'status': 502})
        # Legacy zh detail: backend problem + explicit no-model-switch.
        self.assertIn('后端网关暂时不可用', ev['detail'])
        self.assertIn('kimi-k3', ev['detail'])
        self.assertIn('不会自动切换', ev['detail'])

    def test_display_label_strips_gateway_prefix(self):
        ev = self._drive_retry('Upstream error', 503,
                               model='aws.claude-opus-4.8')
        self.assertEqual(ev['detailArgs']['model'], 'claude-opus-4.8')

    def test_non_gateway_reason_untouched(self):
        """The generic reason branch keeps its pre-existing shape — only the
        gateway token takes the new branch."""
        ev = self._drive_retry('Endpoint unreachable', 0)
        self.assertEqual(ev['detailKey'], 'stream.phase.retryReason')
        self.assertIn('endpointUnreachable', ev['detailArgs'].get('reasonKey', ''))


# ── 3. llm_fallback: gateway errors never switch models ─────────────

def _make_task():
    return {'id': 't-gw-0001', 'convId': 'conv-gw', '_userId': 1, 'config': {},
            'content': '', 'thinking': '', 'events': [],
            'events_lock': threading.Lock()}


@pytest.mark.unit
class TestGatewayNeverSwitchesModel(unittest.TestCase):

    def test_no_fallback_switch_no_pool_rescue_breaks_with_envelope(
            self):
        """Gateway error + configured fallback + prior tool calls: the turn
        stops on the ORIGINAL model with an upstream_error envelope — no
        second stream call, no fallback stamps, no rescue."""
        from lib.tasks_pkg.llm_fallback._call import _llm_call_with_fallback
        import lib.tasks_pkg.llm_fallback._call as fb_pkg
        import lib.tasks_pkg.llm_fallback._call as call_mod

        events = []
        exc = _gateway_exc(502)
        calls = {'streams': 0, 'rescues': 0}

        def _fake_stream(task, body, tag='', on_tool_call_ready=None, **kw):
            calls['streams'] += 1
            raise exc

        def _no_rescue(*a, **kw):
            calls['rescues'] += 1
            return None

        _orig = (fb_pkg.stream_llm_response, fb_pkg._get_fallback_model,
                 fb_pkg._flag_empty_stop_for_retry, fb_pkg._emit_round_usage,
                 fb_pkg.append_event, call_mod._attempt_pool_rescue)
        fb_pkg.stream_llm_response = _fake_stream
        fb_pkg._get_fallback_model = lambda task: 'claude-opus-5'
        fb_pkg._flag_empty_stop_for_retry = lambda *a, **kw: False
        fb_pkg._emit_round_usage = lambda *a, **kw: None
        fb_pkg.append_event = lambda task, ev: events.append(ev)
        call_mod._attempt_pool_rescue = _no_rescue
        try:
            task = _make_task()
            res = _llm_call_with_fallback(
                task, {'model': 'kimi-k3'}, 'kimi-k3', 2, 512,
                True, None, [{'role': 'user', 'content': 'hi'}],
                'low', False, {}, [])
        finally:
            (fb_pkg.stream_llm_response, fb_pkg._get_fallback_model,
             fb_pkg._flag_empty_stop_for_retry, fb_pkg._emit_round_usage,
             fb_pkg.append_event, call_mod._attempt_pool_rescue) = _orig

        self.assertEqual(calls['streams'], 1,
                         'a second (fallback) stream call happened')
        self.assertEqual(calls['rescues'], 0, 'pool rescue was attempted')
        self.assertEqual(res['model'], 'kimi-k3')
        self.assertEqual(res['_loop_action'], 'break')
        self.assertIn('gateway_outage', res['_loop_exit_reason'])
        self.assertEqual(task['error']['kind'], 'upstream_error')
        self.assertTrue(task['error']['retryable'])
        self.assertNotIn('_fallback_model', task)
        final = [e for e in events
                 if e.get('detailKey') == 'stream.phase.gatewayOutageFinal']
        self.assertTrue(final, f'no gatewayOutageFinal phase: {events}')
        self.assertEqual(final[0]['detailArgs']['status'], 502)

    def test_no_tool_calls_reraises_with_envelope_attached(self):
        from lib.tasks_pkg.llm_fallback._call import _llm_call_with_fallback
        import lib.tasks_pkg.llm_fallback._call as fb_pkg

        events = []
        exc = _gateway_exc(503)
        _orig = (fb_pkg.stream_llm_response, fb_pkg._get_fallback_model,
                 fb_pkg._flag_empty_stop_for_retry, fb_pkg._emit_round_usage,
                 fb_pkg.append_event)
        fb_pkg.stream_llm_response = (
            lambda *a, **kw: (_ for _ in ()).throw(exc))
        fb_pkg._get_fallback_model = lambda task: 'claude-opus-5'
        fb_pkg._flag_empty_stop_for_retry = lambda *a, **kw: False
        fb_pkg._emit_round_usage = lambda *a, **kw: None
        fb_pkg.append_event = lambda task, ev: events.append(ev)
        try:
            task = _make_task()
            with self.assertRaises(RateLimitError) as ei:
                _llm_call_with_fallback(
                    task, {'model': 'kimi-k3'}, 'kimi-k3', 0, 512,
                    False, None, [{'role': 'user', 'content': 'hi'}],
                    'low', False, {}, [])
        finally:
            (fb_pkg.stream_llm_response, fb_pkg._get_fallback_model,
             fb_pkg._flag_empty_stop_for_retry, fb_pkg._emit_round_usage,
             fb_pkg.append_event) = _orig
        self.assertIs(ei.exception, exc, 'must re-raise the ORIGINAL error')
        env = getattr(exc, '_user_message', None)
        self.assertIsNotNone(env, 'typed envelope must ride the exception')
        self.assertEqual(env['kind'], 'upstream_error')
        self.assertEqual(env['context'], 'gateway-outage')

    def test_gateway_on_fallback_hop_skips_pool_rescue(self):
        """Primary fails non-gateway → configured fallback runs → the
        FALLBACK dies with a gateway error: the pool rescue must NOT fire
        (arbitrary-model switch); the honest error surfaces instead."""
        from lib.tasks_pkg.llm_fallback._call import _llm_call_with_fallback
        import lib.tasks_pkg.llm_fallback._call as fb_pkg
        import lib.tasks_pkg.llm_fallback._call as call_mod
        from lib.llm import PermissionError_

        events = []
        calls = {'n': 0, 'rescues': 0}

        def _fake_stream(task, body, tag='', on_tool_call_ready=None, **kw):
            calls['n'] += 1
            if calls['n'] == 1:
                raise PermissionError_('401 from primary')
            raise _gateway_exc(502)

        def _no_rescue(*a, **kw):
            calls['rescues'] += 1
            return None

        _orig = (fb_pkg.stream_llm_response, fb_pkg._get_fallback_model,
                 fb_pkg._flag_empty_stop_for_retry, fb_pkg._emit_round_usage,
                 fb_pkg.append_event, call_mod._attempt_pool_rescue)
        fb_pkg.stream_llm_response = _fake_stream
        fb_pkg._get_fallback_model = lambda task: 'claude-opus-5'
        fb_pkg._flag_empty_stop_for_retry = lambda *a, **kw: False
        fb_pkg._emit_round_usage = lambda *a, **kw: None
        fb_pkg.append_event = lambda task, ev: events.append(ev)
        call_mod._attempt_pool_rescue = _no_rescue
        try:
            task = _make_task()
            res = _llm_call_with_fallback(
                task, {'model': 'kimi-k3'}, 'kimi-k3', 1, 512,
                True, None, [{'role': 'user', 'content': 'hi'}],
                'low', False, {}, [])
        finally:
            (fb_pkg.stream_llm_response, fb_pkg._get_fallback_model,
             fb_pkg._flag_empty_stop_for_retry, fb_pkg._emit_round_usage,
             fb_pkg.append_event, call_mod._attempt_pool_rescue) = _orig
        self.assertEqual(calls['rescues'], 0, 'pool rescue was attempted')
        self.assertEqual(res['_loop_action'], 'break')
        self.assertEqual(task['error']['kind'], 'upstream_error')
        # The failed decision-time fallback stamps are cleared.
        self.assertNotIn('_fallback_model', task)

    def test_non_gateway_error_still_pool_rescues(self):
        """Regression pin for owner directive 2026-08-03: a NON-gateway
        error (durable 401) with no fallback configured still reaches the
        pool-wide rescue before giving up."""
        from lib.tasks_pkg.llm_fallback._call import _llm_call_with_fallback
        import lib.tasks_pkg.llm_fallback._call as fb_pkg
        import lib.tasks_pkg.llm_fallback._call as call_mod
        from lib.llm import PermissionError_

        events = []
        exc = PermissionError_('401 invalid app id')
        calls = {'rescues': 0}

        _orig = (fb_pkg.stream_llm_response, fb_pkg._get_fallback_model,
                 fb_pkg._flag_empty_stop_for_retry, fb_pkg._emit_round_usage,
                 fb_pkg.append_event, call_mod._attempt_pool_rescue)
        fb_pkg.stream_llm_response = (
            lambda *a, **kw: (_ for _ in ()).throw(exc))
        fb_pkg._get_fallback_model = lambda task: None
        fb_pkg._flag_empty_stop_for_retry = lambda *a, **kw: False
        fb_pkg._emit_round_usage = lambda *a, **kw: None
        fb_pkg.append_event = lambda task, ev: events.append(ev)

        def _rescue(*a, **kw):
            calls['rescues'] += 1
            return None

        call_mod._attempt_pool_rescue = _rescue
        try:
            task = _make_task()
            with self.assertRaises(PermissionError_):
                _llm_call_with_fallback(
                    task, {'model': 'kimi-k3'}, 'kimi-k3', 0, 512,
                    False, None, [{'role': 'user', 'content': 'hi'}],
                    'low', False, {}, [])
        finally:
            (fb_pkg.stream_llm_response, fb_pkg._get_fallback_model,
             fb_pkg._flag_empty_stop_for_retry, fb_pkg._emit_round_usage,
             fb_pkg.append_event, call_mod._attempt_pool_rescue) = _orig
        self.assertEqual(calls['rescues'], 1,
                         'non-gateway errors must keep the 2026-08-03 rescue')


# ── 5. locale catalogs ────────────────────────────────────────────────

@pytest.mark.unit
class TestLocaleCatalog(unittest.TestCase):

    def test_new_keys_exist_both_locales(self):
        locale_root = ROOT / 'frontend' / 'src' / 'i18n' / 'locales'
        for path in locale_root.glob('*.json'):
            catalog = json.loads(path.read_text(encoding='utf-8'))
            for key in ('stream.phase.retryGateway',
                        'stream.phase.gatewayOutageFinal'):
                self.assertIn(key, catalog, f'{path.name} missing {key}')

    def test_zh_gateway_retry_promises_no_switch(self):
        catalog = json.loads(
            (ROOT / 'frontend' / 'src' / 'i18n' / 'locales' / 'zh.json')
            .read_text(encoding='utf-8'))
        self.assertIn('不会自动切换',
                      catalog['stream.phase.retryGateway'])
        self.assertIn('未自动切换',
                      catalog['stream.phase.gatewayOutageFinal'])


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_storage
    guard_standalone_storage(
        'test_gateway_strict_no_fallback.__main__', start_authority=False)
    unittest.main(verbosity=2)
