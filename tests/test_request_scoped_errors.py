"""Fruit 3 (E2): request-scoped errors (HTTP 400/404/422 — deterministic
request-shape rejections, CLIProxyAPI's ``isRequestScopedResultError``)
must NOT enter slot/model cooldown:

  - 400 is already typed (BadRequestError → slot released, no cooldown,
    no key_stats feed) — pinned as a guard;
  - 404 / 422 today fall through to the generic Exception → generic
    dispatch handler → record_error → consecutive-error 300s lockout +
    model exclusion. They must instead classify as a request-scoped
    error: surfaced to the caller, slot released, NO cooldown, NO
    fallback attempts consumed.

Run:  pytest tests/test_request_scoped_errors.py -m unit
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _classify(status, body):
    from lib.llm_errors import _classify_http_error
    _classify_http_error(status, body, 'some-model', '[t]')


@pytest.mark.unit
class TestRequestScopedClassification:
    def test_404_is_request_scoped(self):
        from lib.llm_errors import RequestScopedError
        with pytest.raises(RequestScopedError) as ei:
            _classify(404, '{"error":{"message":"model not found"}}')
        assert ei.value.status_code == 404

    def test_422_is_request_scoped(self):
        from lib.llm_errors import RequestScopedError
        with pytest.raises(RequestScopedError) as ei:
            _classify(422, '{"error":{"message":"Unprocessable Entity: '
                           'messages.0.content is required"}}')
        assert ei.value.status_code == 422

    def test_400_still_bad_request_error(self):
        """Guard: 400 keeps its existing typed classification (dispatcher
        already releases the slot for it)."""
        from lib.llm_errors import BadRequestError, RequestScopedError
        with pytest.raises(BadRequestError):
            _classify(400, '{"error":{"type":"invalid_request_error",'
                           '"message":"weird payload"}}')
        # …and a 400 must NOT be reclassified as the new generic bucket.
        try:
            _classify(400, '{"error":{"type":"invalid_request_error"}}')
        except RequestScopedError:
            pytest.fail('400 must stay BadRequestError, not RequestScopedError')
        except BadRequestError:
            pass

    def test_429_not_request_scoped(self):
        from lib.llm_errors import RateLimitError
        with pytest.raises(RateLimitError):
            _classify(429, '{"error":{"message":"rate limited"}}')


class _FakeDispatcher:
    def __init__(self, slots):
        self._slots = list(slots)
        self.slots = list(slots)
        self.picks = 0

    def pick_and_reserve(self, **kwargs):
        self.picks += 1
        if not self._slots:
            return None
        slot = self._slots.pop(0)
        if slot is not None:
            slot.record_request()
        return slot

    def has_capable_slots(self, *a, **kw):
        return bool(self._slots)

    def summarize_slots(self, *a, **kw):
        return 'fake-slots'


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr('lib.llm_dispatch.api.time.sleep', lambda *_a, **_k: None)


def _make_slot(key='k0'):
    from lib.llm_dispatch.slot import Slot
    return Slot(key_name=key, api_key='sk-x', model='m0',
                capabilities={'text'})


@pytest.mark.unit
class TestRequestScopedDispatch:
    def test_dispatch_chat_surfaces_without_slot_damage(self, monkeypatch):
        from lib.llm_dispatch import api
        from lib.llm_errors import RequestScopedError

        slot = _make_slot()
        disp = _FakeDispatcher([slot, slot])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        def _fake_chat(**kw):
            raise RequestScopedError('API HTTP 404: model not found',
                                     status_code=404)

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'chat', _fake_chat)

        with pytest.raises(RequestScopedError):
            api.dispatch_chat([{'role': 'user', 'content': 'hi'}],
                              log_prefix='[t]')

        assert disp.picks == 1, 'no fallback attempt consumed'
        assert slot.inflight == 0, 'inflight reservation released'
        assert slot.consecutive_errors == 0, 'no slot-health damage'
        assert slot.cooldown_until == 0, 'no cooldown imposed'

    def test_dispatch_stream_surfaces_without_slot_damage(self, monkeypatch):
        from lib.llm_dispatch import api
        from lib.llm_errors import RequestScopedError

        slot = _make_slot()
        disp = _FakeDispatcher([slot, slot])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        def _fake_stream(body, **kw):
            raise RequestScopedError('API HTTP 422: unprocessable',
                                     status_code=422)

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        with pytest.raises(RequestScopedError):
            api.dispatch_stream([{'role': 'user', 'content': 'hi'}],
                                log_prefix='[t]')

        assert disp.picks == 1
        assert slot.inflight == 0
        assert slot.consecutive_errors == 0
        assert slot.cooldown_until == 0

    def test_generic_error_still_marks_slot(self, monkeypatch):
        """Guard (mutation check): a genuine unexpected error still feeds
        the slot-health channel — only request-scoped 4xx are exempt."""
        from lib.llm_dispatch import api

        slot = _make_slot()
        disp = _FakeDispatcher([slot])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        def _fake_chat(**kw):
            raise RuntimeError('boom')

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'chat', _fake_chat)

        with pytest.raises(RuntimeError):
            api.dispatch_chat([{'role': 'user', 'content': 'hi'}],
                              max_retries=1, log_prefix='[t]')

        assert slot.consecutive_errors == 1


@pytest.mark.unit
@pytest.mark.parametrize('error_factory', [
    lambda: __import__('lib.llm_errors', fromlist=['BadRequestError'])
        .BadRequestError('invalid moonshot tool schema'),
    lambda: __import__('lib.llm_errors', fromlist=['RequestScopedError'])
        .RequestScopedError('unprocessable request', status_code=422),
])
def test_request_rejection_never_switches_model_or_pool_rescues(
        monkeypatch, error_factory):
    import lib.tasks_pkg.llm_fallback._call as fallback

    error = error_factory()
    stream_calls = []
    rescue_calls = []

    def reject(_task, body, **_kwargs):
        stream_calls.append(body.get('model'))
        raise error

    monkeypatch.setattr(fallback, 'stream_llm_response', reject)
    monkeypatch.setattr(fallback, '_get_fallback_model',
                        lambda _task: 'glm-5.3')
    monkeypatch.setattr(
        fallback, '_attempt_pool_rescue',
        lambda *_args, **_kwargs: rescue_calls.append(True))
    task = {
        'id': 'request-rejection-task', 'convId': 'request-rejection-conv',
        'config': {}, 'content': '', 'thinking': '', 'events': [],
    }

    with pytest.raises(type(error)) as raised:
        fallback._llm_call_with_fallback(
            task, {'model': 'kimi-k3'}, 'kimi-k3', 0, 512,
            False, None, [{'role': 'user', 'content': 'hi'}],
            'low', False, {}, [])

    assert raised.value is error
    assert stream_calls == ['kimi-k3']
    assert rescue_calls == []
    assert '_fallback_model' not in task
    assert error._user_message['kind'] == 'bad_request'
