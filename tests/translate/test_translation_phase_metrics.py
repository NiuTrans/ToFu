"""Cross-layer translation evidence uses real boundaries, not invented TTFT."""
from concurrent.futures import Future
import importlib
from types import SimpleNamespace

import pytest
from lib.llm.diagnostic_timing import DiagnosticTiming, bind_timing, timed_call, timing_stage
from lib.translate.observability import observed_turn_command

pytestmark = pytest.mark.unit


def test_engine_attaches_phase_snapshot_without_changing_translation(monkeypatch):
    from lib.translate.engine import _engine
    now = [0.0]
    monkeypatch.setattr('lib.mt_provider.is_mt_configured', lambda: False)
    monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher', lambda: object())
    monkeypatch.setattr(_engine.translate_cache, 'put', lambda *a, **k: None)
    def dispatch(*_args, **_kwargs):
        with timing_stage('engine_attempt'):
            now[0] += 30.7
            return '这是一条忠实的中文翻译。', {'_dispatch': {'model': 'actual-translator'}}
    monkeypatch.setattr(_engine, '_dispatch_translation_candidate', dispatch)
    timing = DiagnosticTiming(kind='synthetic', clock=lambda: now[0], emit=lambda _record: None)
    with bind_timing(timing):
        translated, usage = _engine._translate_one_chunk(
            'This is a short sentence.', 'Translate faithfully.',
            source='English', target='Chinese', use_cache=False)
    assert translated == '这是一条忠实的中文翻译。'
    snapshot = usage['_translate_trace']['timing']
    assert snapshot['timing_id'] == timing.timing_id
    assert snapshot['stages']['engine_attempt']['total_ms'] == 30700
    assert snapshot['stages']['quality_validation']['count'] == 1
    assert snapshot['active_spans'] == []
    assert 'nonstream_first_token' in snapshot['unobserved']


def test_stream_first_content_is_observed_not_completion_upper_bound(monkeypatch):
    module = importlib.import_module('lib.llm.stream')
    from lib.llm.stream_result import ProviderStreamResult
    now, received = [0.0], []
    def attempt(_body, **kwargs):
        now[0] = 1
        # Reasoning/pings are not mistaken for first visible content.
        if kwargs.get('on_thinking'):
            kwargs['on_thinking']('reasoning')
        now[0] = 3
        kwargs['on_content']('first')
        now[0] = 5
        kwargs['on_content']('second')
        now[0] = 6
        return ProviderStreamResult.from_legacy({'content': 'firstsecond'}, 'stop', {})
    monkeypatch.setattr(module, '_stream_chat_once_impl', attempt)
    timing = DiagnosticTiming(kind='synthetic', clock=lambda: now[0], emit=lambda _record: None)
    with bind_timing(timing):
        result = module._stream_chat_once({'model': 'actual-model', 'messages': []}, on_content=received.append)
    assert result.message['content'] == 'firstsecond' and received == ['first', 'second']
    sample = next(item for item in timing.snapshot()['samples'] if item['phase'] == 'provider_stream')
    assert sample['first_content_ms'] == 3000
    assert sample['after_first_content_ms'] == 3000
    assert sample['duration_ms'] == 6000


def test_command_scope_remains_open_through_persistence(monkeypatch):
    from lib.chat import turn_builder
    from lib.translate import execution
    now, records = [0.0], []
    monkeypatch.setattr(turn_builder, '_should_translate_input', lambda *_args: True)
    monkeypatch.setattr('lib.translate._translate_freetext', lambda *_args, **_kwargs:
        ('translated', {'_dispatch': {'model': 'actual-model'}}))
    def submit(_job, *, function, **_kwargs):
        future = Future(); future.set_result(function()); return future
    monkeypatch.setattr(execution, 'submit_attended_translation', submit)
    class Service:
        @observed_turn_command
        def create_turn(self, conversation_id, user_id, body):
            turn_builder.auto_translate_user('原文', body['config'], user_id=user_id, conv_id=conversation_id)
            def persist():
                now[0] = 60
            timed_call('turn_persist', persist)
            return SimpleNamespace(value={'conversationId': conversation_id})
    timing = DiagnosticTiming(kind='synthetic', clock=lambda: now[0], emit=records.append)
    with bind_timing(timing):
        result = Service().create_turn('conv-id', 7, {'config': {'autoTranslate': True}})

        assert not any(row['event'] == 'summary' for row in records)
    timing.complete('command_accepted')
    assert result.value == {'conversationId': 'conv-id'}
    assert timing.snapshot()['stages']['turn_persist']['total_ms'] == 60000
    summaries = [row for row in records if row['event'] == 'summary']
    assert len(summaries) == 1 and summaries[0]['outcome'] == 'command_accepted'
    assert not any(row['event'].startswith('late_') for row in records)


def test_real_command_service_observes_persistence_and_receipt_reads(monkeypatch):
    from lib.conversation_sync import command_service as commands
    now = [0.0]
    monkeypatch.setattr(commands, '_capture_task_principal_tenant',
                        lambda config, *_args: dict(config))
    def persist(*_args, **_kwargs):
        now[0] += 12
        return {'_needsStart': False, 'turn': {'turnId': 'output'},
                'attempt': {'attemptId': 'attempt'}}
    monkeypatch.setattr(commands, 'create_turn_pair', persist)
    def read_turn(*_args, **_kwargs):
        now[0] += 2
        return {'turnId': 'output'}
    monkeypatch.setattr(commands, 'get_turn', read_turn)
    monkeypatch.setattr(commands, 'get_attempt', lambda *_a, **_k: {'attemptId': 'attempt'})
    monkeypatch.setattr(commands, 'get_conversation_revision', lambda *_a, **_k: 3)
    service = commands.ConversationTurnCommandService(
        build_user_message=lambda *_args: {'content': 'already translated'},
        was_aborted_after=lambda *_args: False,
        start_task=lambda *_args: ('', None))
    timing = DiagnosticTiming(kind='synthetic', clock=lambda: now[0], emit=lambda _record: None)
    with bind_timing(timing):
        result = service.create_turn('conv', 7,
            {'commandId': 'command', 'message': {'text': 'original'},
             'config': {'autoTranslate': True}}, request_started_at=1)
    snapshot = timing.snapshot()
    assert result.value['conversationRevision'] == 3
    assert snapshot['stages']['turn_command_total']['total_ms'] == 14000
    assert snapshot['stages']['turn_persist']['total_ms'] == 12000
    assert snapshot['stages']['executor_start']['count'] == 1
    assert snapshot['stages']['acknowledgement_read']['count'] == 3
    assert snapshot['active_spans'] == []
