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


@pytest.mark.unit
class TestRequestScopedEnvelopeKind:
    """404 and 422 render through DIFFERENT envelope kinds (2026-09-02):
    the shared bad_request bucket labeled every 404 with the hardcoded
    "HTTP 400" title while the wire statusCode said 404."""

    def test_404_envelope_kind_is_not_found(self):
        from lib.error_envelope import from_exception
        from lib.llm_errors import RequestScopedError
        envelope = from_exception(
            RequestScopedError('API HTTP 404: {"detail":"Not Found"}',
                               status_code=404),
            model='gpt-5.6-sol', context='request-rejected',
            source='llm-stream')
        assert envelope['kind'] == 'not_found'
        assert envelope['titleKey'] == 'err.k.not_found.title'
        assert envelope['hintKey'] == 'err.k.not_found.hint'
        assert envelope['statusCode'] == 404
        assert envelope['retryable'] is False
        assert envelope['severity'] == 'error'
        assert 'HTTP 404' in envelope['message']
        assert 'HTTP 400' not in envelope['message']

    def test_422_envelope_kind_stays_bad_request(self):
        from lib.error_envelope import from_exception
        from lib.llm_errors import RequestScopedError
        envelope = from_exception(
            RequestScopedError('API HTTP 422: unprocessable', status_code=422))
        assert envelope['kind'] == 'bad_request'

    def test_protocol_defect_subclass_stays_bad_request(self):
        """Guard: Continue-protocol defects (status_code=422, custom
        _user_message) must not drift into the 404 bucket."""
        from lib.error_envelope import from_exception
        from lib.tasks_pkg.message_builder._tool_history import (
            ContinueToolHistoryProtocolError)
        envelope = from_exception(ContinueToolHistoryProtocolError('broken'))
        assert envelope['kind'] == 'bad_request'


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


@pytest.mark.unit
def test_local_request_preparation_never_switches_or_pool_rescues(monkeypatch):
    import lib.tasks_pkg.llm_fallback._call as fallback
    from lib.llm_errors import LocalRequestPreparationError

    error = LocalRequestPreparationError(
        'ptc projection signature mismatch', stage='wire_projection')
    stream_calls = []
    rescue_calls = []

    def reject(_task, body, **_kwargs):
        stream_calls.append(body.get('model'))
        raise error

    monkeypatch.setattr(fallback, 'stream_llm_response', reject)
    monkeypatch.setattr(fallback, '_get_fallback_model', lambda _task: 'glm-5.3')
    monkeypatch.setattr(
        fallback, '_attempt_pool_rescue',
        lambda *_args, **_kwargs: rescue_calls.append(True))
    task = {
        'id': 'local-prepare-task', 'convId': 'local-prepare-conv',
        'config': {}, 'content': '', 'thinking': '', 'events': [],
    }

    with pytest.raises(LocalRequestPreparationError) as raised:
        fallback._llm_call_with_fallback(
            task, {'model': 'kimi-k3'}, 'kimi-k3', 0, 512,
            False, None, [{'role': 'user', 'content': 'hi'}],
            'low', False, {}, [])

    assert raised.value is error
    assert stream_calls == ['kimi-k3']
    assert rescue_calls == []
    assert '_fallback_model' not in task
    assert error._user_message['kind'] == 'internal'
