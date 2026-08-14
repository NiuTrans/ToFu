"""Pool-rescue tests (owner directive 2026-08-03).

"Why does a 401/403 interrupt the turn instead of rotating to keys that DO
have permission? An error may surface ONLY when ALL keys are unavailable."

The fallback chain (primary → configured fallback model) covers two models.
When both fail — the incident: kimi-k3 (the fallback) is pinned by
``key_access`` to a single key whose AppId the vendor rejects with a durable
401 — the turn used to die with a "check your API keys" envelope while the
pool's other models sat healthy. ``_attempt_pool_rescue`` makes ONE more
pool-wide dispatch (non-strict, no preferred model, failed models excluded)
before any envelope may surface.

Pins:
  * fallback-model 401 + healthy pool → the RESCUE completes the round
    (pool_wide=True, failed models excluded), badge names the rescue model;
  * pool empty beyond the failed models → the original envelope path
    (unchanged);
  * disableModelFallback (explicit per-request opt-out) → NO rescue;
  * rescue dispatch itself failing → the original envelope path;
  * manager seam: ``stream_llm_response(pool_wide=True)`` dispatches
    non-strict with prefer_model=None and forwards exclude_models.

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
                  patch_fallback=True):
    import lib.tasks_pkg.llm_fallback as fb_pkg
    events = []
    monkeypatch.setattr(fb_pkg, 'stream_llm_response', stream_impl)
    if patch_fallback:
        monkeypatch.setattr(fb_pkg, '_get_fallback_model', lambda task: fallback)
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
        """The incident shape: primary fails, fallback (kimi-k3) 401s, the
        pool still has healthy models → the rescue completes the round."""
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
                     '_dispatch': {'model': 'glm-5.1', 'key': 'k9'}})

        events = _patch_common(monkeypatch, _stream)
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
        rescue_kw = calls[2][2]
        assert rescue_kw.get('pool_wide') is True, (
            'the rescue must dispatch NON-strict across the whole pool')
        assert rescue_kw.get('exclude_models') == {'claude-opus-5', 'kimi-k3'}, (
            'models already proven dead in this chain must not be re-tried '
            f'(got {rescue_kw.get("exclude_models")})')
        assert gate.calls and gate.calls[0].get('exclude_models') == {
            'claude-opus-5', 'kimi-k3'}

        assert res['_loop_action'] is None
        assert res['model'] == 'glm-5.1'
        assert res['finish_reason'] == 'stop'
        assert res['assistant_msg']['content'] == 'rescued'
        assert 'error' not in task, 'a rescued turn must not carry an envelope'

        # Badge: names the rescue model, keeps the original primary as from.
        assert task['_fallback_model'] == 'glm-5.1'
        assert task['_fallback_from'] == 'claude-opus-5'
        assert task['_fallback_kind'] == 'permission'
        assert 'permission' in task['_fallback_reason']

        # Honest accounting: the rescue round is billed into api_rounds.
        assert usage_acc.get('prompt_tokens') == 5
        assert any(r.get('model') == 'glm-5.1' and r.get('tag') == 'R1-RESCUE'
                   for r in api_rounds)
        assert any('其它可用模型' in (e.get('detail') or '') for e in events), (
            f'no rescue phase event: {events}')

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
class TestStreamPoolWideSeam:
    """manager._stream.stream_llm_response forwards pool_wide correctly."""

    def _task(self):
        return {'id': 't-seam-001', 'convId': 'c-seam', 'config': {},
                'content': '', 'thinking': '',
                'content_lock': threading.Lock(),
                'events': [], 'events_lock': threading.Lock()}

    def _record_dispatch(self, monkeypatch):
        import lib.tasks_pkg.manager as mgr
        rec = {}

        def _ds(body, **kw):
            rec.update(kw)
            return ({'role': 'assistant', 'content': 'ok'}, 'stop', {})

        monkeypatch.setattr(mgr, 'dispatch_stream', _ds)
        return rec

    def test_pool_wide_dispatches_non_strict(self, monkeypatch):
        rec = self._record_dispatch(monkeypatch)
        from lib.tasks_pkg.manager._stream import stream_llm_response
        stream_llm_response(
            self._task(),
            {'model': 'kimi-k3',
             'messages': [{'role': 'user', 'content': 'hi'}]},
            pool_wide=True, exclude_models={'kimi-k3'})
        assert rec.get('prefer_model') is None, (
            'pool-wide rescue must not pin a model')
        assert rec.get('strict_model') is False
        assert rec.get('exclude_models') == {'kimi-k3'}

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


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
