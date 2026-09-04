"""Bounded attended translation on the chat-send path."""

from __future__ import annotations

from concurrent.futures import Future
import threading
import time

import pytest


pytestmark = pytest.mark.unit


def _force_real_translation(monkeypatch, turn_builder, translate_result):
    monkeypatch.setattr(
        turn_builder, '_should_translate_input', lambda _text, _config: True)
    import lib.translate as translate
    monkeypatch.setattr(
        translate,
        '_translate_freetext',
        lambda *_args, **_kwargs: translate_result,
    )


def test_send_translation_carries_owner_without_request_local_threads(
    monkeypatch,
):
    from lib.chat import turn_builder
    from lib.translate import execution

    _force_real_translation(
        monkeypatch,
        turn_builder,
        ('translated input', {'_dispatch': {'model': 'translator'}}),
    )
    observed = {}

    def submit(job_id, *, owner_user_id, function):
        observed['job_id'] = job_id
        observed['owner_user_id'] = owner_user_id
        future = Future()
        future.set_result(function())
        return future

    monkeypatch.setattr(execution, 'submit_attended_translation', submit)
    monkeypatch.setattr(
        execution,
        'cancel_attended_translation',
        lambda _job_id: pytest.fail('completed work was cancelled'),
    )

    def forbid_request_thread(*_args, **_kwargs):
        raise AssertionError('send path created a request-local thread')

    monkeypatch.setattr(turn_builder.threading, 'Thread', forbid_request_thread)
    message = turn_builder.build_user_msg_from_payload(
        {'text': '需要翻译的输入'},
        {'autoTranslate': True},
        user_id=73,
        conv_id='conversation-1',
    )

    assert observed['job_id'].startswith('send:conversation-1:')
    assert observed['owner_user_id'] == 73
    assert message['content'] == 'translated input'
    assert message['originalContent'] == '需要翻译的输入'
    assert message['_translateModel'] == 'translator'


def test_send_translation_timeout_cancels_pending_work(
    monkeypatch,
):
    from lib.chat import turn_builder
    from lib.translate import execution

    _force_real_translation(monkeypatch, turn_builder, ('unused', {}))
    never_finishes = Future()
    cancelled = []
    monkeypatch.setattr(turn_builder, '_TRANSLATE_SEND_TIMEOUT', 0.03)
    monkeypatch.setattr(
        execution,
        'submit_attended_translation',
        lambda *_args, **_kwargs: never_finishes,
    )

    def cancel(job_id):
        cancelled.append(job_id)
        return never_finishes.cancel()

    monkeypatch.setattr(execution, 'cancel_attended_translation', cancel)
    translated = turn_builder.auto_translate_user(
        '需要翻译的输入',
        {'autoTranslate': True},
        user_id=9,
        conv_id='conversation-timeout',
    )

    assert translated == ('需要翻译的输入', None, None, 'timed_out')
    assert len(cancelled) == 1
    assert never_finishes.cancelled() is True


def test_send_translation_timeout_propagates_abort_to_running_provider(
    monkeypatch,
):
    from lib.agent_core.fair_work_lane import OwnerFairWorkLane
    from lib.chat import turn_builder
    import lib.translate as translate
    from lib.translate import execution

    lane = OwnerFairWorkLane(
        max_workers=1,
        queue_capacity=2,
        idle_seconds=0.05,
        thread_name_prefix='test-send-translation',
        metric_pool='test-send-translation',
    )
    monkeypatch.setattr(execution, '_translation_lane', lane)
    monkeypatch.setattr(
        turn_builder, '_should_translate_input', lambda _text, _config: True)
    monkeypatch.setattr(turn_builder, '_TRANSLATE_SEND_TIMEOUT', 0.03)
    provider_started = threading.Event()
    provider_stopped = threading.Event()

    def wait_for_abort(*_args, **kwargs):
        provider_started.set()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if kwargs['abort_check']():
                provider_stopped.set()
                raise RuntimeError('provider observed attended abort')
            time.sleep(0.001)
        raise AssertionError('running translation never received abort')

    monkeypatch.setattr(translate, '_translate_freetext', wait_for_abort)
    translated = turn_builder.auto_translate_user(
        '需要翻译的输入',
        {'autoTranslate': True},
        user_id=12,
        conv_id='conversation-running-timeout',
    )

    assert provider_started.is_set()
    assert translated == ('需要翻译的输入', None, None, 'timed_out')
    assert provider_stopped.wait(1)
    lane.shutdown()


def test_send_translation_queue_saturation_falls_back_immediately(monkeypatch):
    from lib.agent_core.fair_work_lane import FairWorkLaneQueueFull
    from lib.chat import turn_builder
    from lib.translate import execution

    _force_real_translation(monkeypatch, turn_builder, ('unused', {}))
    def saturated(*_args, **_kwargs):
        raise FairWorkLaneQueueFull('full')

    monkeypatch.setattr(execution, 'submit_attended_translation', saturated)
    translated = turn_builder.auto_translate_user(
        '需要翻译的输入',
        {'autoTranslate': True},
        user_id=11,
        conv_id='conversation-busy',
    )

    assert translated == ('需要翻译的输入', None, None, 'server_busy')
