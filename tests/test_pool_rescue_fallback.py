"""Pool-rescue tests (owner directive 2026-08-03).

"Why does a 401/403 interrupt the turn instead of rotating to keys that DO
have permission? An error may surface ONLY when ALL keys are unavailable."

The fallback chain (primary → configured fallback model) covers two models.
When both fail — the incident: kimi-k3 (the fallback) is pinned by
``key_access`` to a single key whose AppId the vendor rejects with a durable
401 — the turn used to die with a "check your API keys" envelope while the
pool's other models sat healthy. ``_attempt_pool_rescue`` makes ONE more
pool-wide dispatch (non-strict, configured-default preferred, failed models excluded)
before any envelope may surface.

Pins:
  * fallback-model 401 + healthy pool → the RESCUE completes the round
    (pool_wide=True, failed models excluded), badge names the rescue model;
  * pool empty beyond the failed models → the original envelope path
    (unchanged);
  * disableModelFallback (explicit per-request opt-out) → NO rescue;
  * rescue dispatch itself failing → the original envelope path;
  * manager seam: ``stream_llm_response(pool_wide=True)`` dispatches
    non-strict with the soft preference and forwards exclude_models.

Run:  pytest tests/test_pool_rescue_fallback.py -m unit
"""
from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _base_task(cfg=None):
    return {'id': 't-rescue01', 'convId': 'conv-rescue',
            'config': cfg if cfg is not None else {},
            'content': '', 'thinking': '',
            'content_lock': threading.Lock(),
            'events': [], 'events_lock': threading.Lock()}


def _patch_common(monkeypatch, stream_impl, fallback='kimi-k3',
                  patch_fallback=True, rescue='kimi-k3'):
    import lib.tasks_pkg.llm_fallback._call as fb_pkg
    events = []
    monkeypatch.setattr(fb_pkg, 'stream_llm_response', stream_impl)
    if patch_fallback:
        monkeypatch.setattr(fb_pkg, '_get_fallback_model', lambda task: fallback)
    monkeypatch.setattr(
        fb_pkg, '_get_pool_rescue_model',
        lambda failed: rescue if rescue and rescue not in set(failed or ()) else '')
    monkeypatch.setattr(fb_pkg, '_flag_empty_stop_for_retry',
                        lambda *a, **kw: False)
    monkeypatch.setattr(fb_pkg, '_emit_round_usage', lambda *a, **kw: None)
    monkeypatch.setattr(fb_pkg, 'append_event',
                        lambda task, ev: events.append(ev))
    return events


class _Gate:
    def __init__(self, has):
        self._has = has
        self.calls = []

    def has_capable_slots(self, *a, **kw):
        self.calls.append(kw)
        return self._has


def _set_gate(monkeypatch, has):
    gate = _Gate(has)
    monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher',
                        lambda: gate)
    return gate


@pytest.mark.unit
class TestPoolRescue:
    def test_fallback_401_rescued_by_healthy_pool(self, monkeypatch):
        """Primary and configured fallback fail while the configured default
        remains healthy, so rescue tries that default before arbitrary slots."""
        from lib.llm import PermissionError_
        from lib.tasks_pkg.llm_fallback._call import _llm_call_with_fallback

        calls = []

        def _stream(task, body, tag='', on_tool_call_ready=None, **kw):
            calls.append((body.get('model'), tag, kw))
            if len(calls) == 1:
                raise PermissionError_('429-saturated primary, whatever')
            if len(calls) == 2:
                raise PermissionError_('API HTTP 401: 无效的AppId: YOUR_API_KEY_HERE')
            return ({'role': 'assistant', 'content': 'rescued'}, 'stop',
                    {'prompt_tokens': 5, 'completion_tokens': 3,
                     '_dispatch': {'model': 'kimi-k3', 'key': 'k9'}})

        events = _patch_common(
            monkeypatch, _stream, fallback='glm-5.3', rescue='kimi-k3')
        gate = _set_gate(monkeypatch, has=True)

        task = _base_task()
        usage_acc, api_rounds = {}, []
        res = _llm_call_with_fallback(
            task, {'model': 'claude-opus-5',
                   'messages': [{'role': 'user', 'content': 'hi'}]},
            'claude-opus-5', 0, 512, False, None,
            [{'role': 'user', 'content': 'hi'}],
            'low', False, usage_acc, api_rounds)

        assert len(calls) == 3, f'primary+fallback+rescue: {calls}'
        assert calls[1][2].get('max_429_attempts') == 3
        rescue_kw = calls[2][2]
        assert rescue_kw.get('pool_wide') is True, (
            'the rescue must dispatch NON-strict across the whole pool')
        assert rescue_kw.get('exclude_models') == {'claude-opus-5', 'glm-5.3'}, (
            'models already proven dead in this chain must not be re-tried '
            f'(got {rescue_kw.get("exclude_models")})')
        assert rescue_kw.get('max_429_attempts') == 3
        assert rescue_kw.get('pool_prefer_model') == 'kimi-k3'
        assert gate.calls and gate.calls[0].get('exclude_models') == {
            'claude-opus-5', 'glm-5.3'}

        assert res['_loop_action'] is None
        assert res['model'] == 'kimi-k3'
        assert res['finish_reason'] == 'stop'
        assert res['assistant_msg']['content'] == 'rescued'
        assert 'error' not in task, 'a rescued turn must not carry an envelope'

        # Badge: names the rescue model, keeps the original primary as from.
        assert task['_fallback_model'] == 'kimi-k3'
        assert task['_fallback_from'] == 'claude-opus-5'
        assert task['_fallback_kind'] == 'permission'
        assert 'permission' in task['_fallback_reason']

        # Honest accounting: the rescue round is billed into api_rounds.
        assert usage_acc.get('prompt_tokens') == 5
        assert any(r.get('model') == 'kimi-k3' and r.get('tag') == 'R1-RESCUE'
                   for r in api_rounds)
        assert any('其它可用模型' in (e.get('detail') or '') for e in events), (
            f'no rescue phase event: {events}')
        switches = [
            event for event in events
            if event.get('type') == 'model_fallback'
        ]
        switch = dict(switches[-1])
        # lib.agent_core.events stamps low-frequency boundaries with
        # emittedAt at construction; compare the semantic payload and pin
        # the stamp's shape separately.
        emitted_at = switch.pop('emittedAt', None)
        assert isinstance(emitted_at, (int, float)) and emitted_at > 0
        assert switch == {
            'type': 'model_fallback',
            'fallbackModel': 'kimi-k3',
            'fallbackFrom': 'glm-5.3',
            'fallbackKind': 'pool_rescue',
            'fallbackReason': task['_fallback_reason'],
        }

    def test_pool_empty_falls_through_to_envelope(self, monkeypatch):
        """No healthy slot beyond the failed models → original give-up."""
        from lib.llm import PermissionError_
        from lib.tasks_pkg.llm_fallback._call import _llm_call_with_fallback

        calls = []

        def _stream(task, body, tag='', on_tool_call_ready=None, **kw):
            calls.append(kw)
            raise PermissionError_('401 everywhere')

        _patch_common(monkeypatch, _stream)
        _set_gate(monkeypatch, has=False)

        task = _base_task()
        res = _llm_call_with_fallback(
            task, {'model': 'claude-opus-5'}, 'claude-opus-5', 2, 512,
            True, None, [{'role': 'user', 'content': 'hi'}],
            'low', False, {}, [])

        assert len(calls) == 2, 'no third (rescue) dispatch when pool is empty'
        assert res['_loop_action'] == 'break'
        assert res['finish_reason'] == 'error'
        assert task.get('error'), 'the original envelope must still surface'

    def test_disable_model_fallback_opts_out_of_rescue(self, monkeypatch):
        """An explicit per-request opt-out must skip the rescue entirely."""
        from lib.llm import PermissionError_
        from lib.tasks_pkg.llm_fallback._call import _llm_call_with_fallback

        calls = []

        def _stream(task, body, tag='', on_tool_call_ready=None, **kw):
            calls.append(kw)
            raise PermissionError_('401')

        # patch_fallback=False → the REAL _get_fallback_model reads the
        # task's disableModelFallback flag and returns '' (no fallback).
        _patch_common(monkeypatch, _stream, patch_fallback=False)
        _set_gate(monkeypatch, has=True)

        task = _base_task(cfg={'disableModelFallback': True})
        res = _llm_call_with_fallback(
            task, {'model': 'claude-opus-5'}, 'claude-opus-5', 0, 512,
            True, None, [{'role': 'user', 'content': 'hi'}],
            'low', False, {}, [])

        assert len(calls) == 1, 'opt-out must die on the first failure'
        assert not any(kw.get('pool_wide') for kw in calls)
        assert res['_loop_action'] == 'break'
        assert task.get('error')

    def test_rescue_dispatch_failure_keeps_original_envelope(self, monkeypatch):
        """Rescue tried and also failed → original give-up, honest envelope."""
        from lib.llm import PermissionError_
        from lib.tasks_pkg.llm_fallback._call import _llm_call_with_fallback

        calls = []

        def _stream(task, body, tag='', on_tool_call_ready=None, **kw):
            calls.append(kw)
            raise PermissionError_('401 all the way down')

        _patch_common(monkeypatch, _stream)
        _set_gate(monkeypatch, has=True)

        task = _base_task()
        res = _llm_call_with_fallback(
            task, {'model': 'claude-opus-5'}, 'claude-opus-5', 0, 512,
            True, None, [{'role': 'user', 'content': 'hi'}],
            'low', False, {}, [])

        assert len(calls) == 3, 'rescue was attempted before giving up'
        assert calls[2].get('pool_wide') is True
        assert res['_loop_action'] == 'break'
        assert res['finish_reason'] == 'error'
        assert task.get('error')

    def test_gate_probe_failure_still_attempts_rescue(self, monkeypatch):
        """A gate probe exception must not suppress the bounded rescue."""
        from lib.llm import PermissionError_
        from lib.tasks_pkg.llm_fallback._call import _llm_call_with_fallback

        calls = []

        def _stream(task, body, tag='', on_tool_call_ready=None, **kw):
            calls.append(kw)
            if len(calls) < 3:
                raise PermissionError_('401')
            return ({'role': 'assistant', 'content': 'ok'}, 'stop', {})

        _patch_common(monkeypatch, _stream)

        def _boom():
            raise RuntimeError('dispatcher unavailable')

        monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher', _boom)

        task = _base_task()
        res = _llm_call_with_fallback(
            task, {'model': 'claude-opus-5'}, 'claude-opus-5', 0, 512,
            False, None, [{'role': 'user', 'content': 'hi'}],
            'low', False, {}, [])
        assert len(calls) == 3 and calls[2].get('pool_wide') is True
        assert res['assistant_msg']['content'] == 'ok'


@pytest.mark.unit
class TestFallbackTerminalSettlement:
    @pytest.mark.parametrize('fallback_error_factory', [
        lambda: RuntimeError('fallback transport exploded'),
        lambda: __import__(
            'lib.llm_dispatch._api_errors',
            fromlist=['DispatchRateLimitBudgetExceeded'],
        ).DispatchRateLimitBudgetExceeded(
            __import__('lib.llm_errors', fromlist=['RateLimitError'])
            .RateLimitError('glm throttled', status_code=429),
            attempts=3,
            limit=3,
        ),
    ])
    def test_any_failed_fallback_returns_normal_done_error_path(
            self, monkeypatch, fallback_error_factory):
        from lib.llm import PermissionError_
        from lib.tasks_pkg.llm_fallback._call import _llm_call_with_fallback
        from lib.tasks_pkg.orchestrator._finalize import _build_done_event_base
        from lib.tasks_pkg.turn_retry import should_auto_retry_turn

        fallback_error = fallback_error_factory()
        calls = []

        def _stream(task, body, tag='', on_tool_call_ready=None, **kwargs):
            calls.append((tag, kwargs))
            if len(calls) == 1:
                raise PermissionError_('primary denied')
            raise fallback_error

        _patch_common(monkeypatch, _stream, fallback='glm-5.3')
        _set_gate(monkeypatch, has=False)
        task = _base_task()

        result = _llm_call_with_fallback(
            task, {'model': 'kimi-k3'}, 'kimi-k3', 0, 512,
            False, None, [{'role': 'user', 'content': 'hi'}],
            'low', False, {}, [])

        assert len(calls) == 2
        assert calls[1][1].get('max_429_attempts') == 3
        assert result['_loop_action'] == 'break'
        assert result['finish_reason'] == 'error'
        assert result['model'] == 'glm-5.3'
        assert task['error']['autoRetryExhausted'] is True
        assert task['error']['fallbackFailed'] is True
        assert task['error']['fallbackFrom'] == 'kimi-k3'
        assert task['error']['fallbackModel'] == 'glm-5.3'
        assert should_auto_retry_turn(task['error'], 0, {}) == (False, 0.0)

        done = _build_done_event_base(
            task,
            last_finish_reason=result['finish_reason'],
            last_stream_result=None,
            accumulated_usage={},
            last_usage=None,
            model=result['model'],
            thinking_depth=None,
        )
        assert done['type'] == 'done'
        assert done['finishReason'] == 'error'
        assert done['error'] is task['error']

        if hasattr(fallback_error, 'attempts'):
            assert task['error']['attempts'] == 3
            assert task['error']['limit'] == 3

    def test_vlm_to_text_fallback_marks_projection_source(
            self, monkeypatch):
        from lib.llm import PermissionError_
        import lib.tasks_pkg.llm_fallback._call as fallback

        calls = {'streams': 0, 'build_kwargs': None}

        def _stream(task, body, tag='', on_tool_call_ready=None, **kwargs):
            calls['streams'] += 1
            if calls['streams'] == 1:
                raise PermissionError_('primary denied')
            return ({'role': 'assistant', 'content': 'projected'}, 'stop', {})

        def _build(model, messages, **kwargs):
            calls['build_kwargs'] = kwargs
            return {'model': model, 'messages': list(messages)}

        _patch_common(monkeypatch, _stream, fallback='text-fallback')
        monkeypatch.setattr(fallback, 'build_body', _build)
        monkeypatch.setattr(
            fallback, 'model_supports_vision',
            lambda model: model == 'vision-primary')
        task = _base_task()

        result = fallback._llm_call_with_fallback(
            task, {'model': 'vision-primary'}, 'vision-primary', 0, 512,
            False, None, [{'role': 'user', 'content': 'hi'}],
            'low', False, {}, [])

        assert result['assistant_msg']['content'] == 'projected'
        assert calls['build_kwargs']['vision_fallback_from'] == \
            'vision-primary'

    def test_fallback_429_budget_config_is_clamped(self):
        from lib.tasks_pkg.llm_fallback._retry import (
            _fallback_max_429_attempts,
        )

        assert _fallback_max_429_attempts({}) == 3
        assert _fallback_max_429_attempts(
            {'config': {'fallbackMax429Attempts': '7'}}) == 7
        assert _fallback_max_429_attempts(
            {'config': {'fallbackMax429Attempts': 0}}) == 1
        assert _fallback_max_429_attempts(
            {'config': {'fallbackMax429Attempts': 999}}) == 16
        assert _fallback_max_429_attempts(
            {'config': {'fallbackMax429Attempts': True}}) == 3


@pytest.mark.unit
class TestStreamPoolWideSeam:
    """manager._stream.stream_llm_response forwards pool_wide correctly."""

    def _task(self):
        return {'id': 't-seam-001', 'convId': 'c-seam', 'config': {},
                '_userId': 1,
                'content': '', 'thinking': '',
                'content_lock': threading.Lock(),
                'events': [], 'events_lock': threading.Lock()}

    def _record_dispatch(self, monkeypatch):
        import lib.tasks_pkg.manager._stream as stream
        rec = {}

        def _ds(body, **kw):
            rec.update(kw)
            return ({'role': 'assistant', 'content': 'ok'}, 'stop', {})

        monkeypatch.setattr(stream, 'dispatch_stream', _ds)
        monkeypatch.setattr(
            stream, 'append_event',
            lambda task, event: task['events'].append(event),
        )
        return rec

    def test_pool_wide_dispatches_non_strict(self, monkeypatch):
        rec = self._record_dispatch(monkeypatch)
        from lib.tasks_pkg.manager._stream import stream_llm_response
        stream_llm_response(
            self._task(),
            {'model': 'kimi-k3',
             'messages': [{'role': 'user', 'content': 'hi'}]},
            pool_wide=True, pool_prefer_model='kimi-k3',
            exclude_models={'glm-5.3'})
        assert rec.get('prefer_model') == 'kimi-k3'
        assert rec.get('strict_model') is False
        assert rec.get('exclude_models') == {'glm-5.3'}

    def test_rescue_default_is_soft_and_skips_failed_models(self, monkeypatch):
        import lib
        from lib.tasks_pkg.llm_fallback._retry import _get_pool_rescue_model

        monkeypatch.setattr(lib, 'LLM_MODEL', 'kimi-k3')
        assert _get_pool_rescue_model({'gpt-5.6-sol', 'glm-5.3'}) == 'kimi-k3'
        assert _get_pool_rescue_model({'kimi-k3'}) == ''

    def test_default_stays_strict_on_body_model(self, monkeypatch):
        rec = self._record_dispatch(monkeypatch)
        from lib.tasks_pkg.manager._stream import stream_llm_response
        stream_llm_response(
            self._task(),
            {'model': 'kimi-k3',
             'messages': [{'role': 'user', 'content': 'hi'}]})
        assert rec.get('prefer_model') == 'kimi-k3'
        assert rec.get('strict_model') is True, (
            'the default user-facing path must stay model-pinned')

    def test_explicit_429_budget_is_forwarded(self, monkeypatch):
        rec = self._record_dispatch(monkeypatch)
        from lib.tasks_pkg.manager._stream import stream_llm_response
        stream_llm_response(
            self._task(),
            {'model': 'glm-5.3',
             'messages': [{'role': 'user', 'content': 'hi'}]},
            max_429_attempts=3)
        assert rec.get('max_429_attempts') == 3


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
