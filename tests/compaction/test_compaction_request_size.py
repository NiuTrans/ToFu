"""A compaction's Turn row reports the complete request it leaves behind.

The compactor's own receipt counts its message list only. Recomposition then
restores managed context that rides on the very next request, so the row
settles on that rebuilt request, measured with the counter the gate uses.
"""
import pytest

from lib.tasks_pkg.compaction import _pipeline as pipeline
from lib.tasks_pkg.compaction._layer2 import _compact as compact
from lib.tasks_pkg.compaction._tokens import _estimate_total_tokens

pytestmark = pytest.mark.unit

RESTORED = {'role': 'user', '_contextComposer': True, '_isMeta': True,
            'content': 'RESTORED-MANAGED-CONTEXT ' * 2000}


def _text(message):
    value = message.get('content') or ''
    return value if isinstance(value, str) else ''.join(p.get('text', '') for p in value)


def _request_tokens(messages, *args, **kwargs):
    """Stand-in for the canonical counter: a fixed tool-schema floor plus text."""
    return 16000 + sum(len(_text(message)) for message in messages)


def _conversation():
    messages = [{'role': 'system', 'content': 'Required policy'},
                {'role': 'user', 'content': 'Investigate'}]
    for index in range(8):
        messages.append({'role': 'user', '_isInboxInject': True,
                         '_containsHumanSteer': False, 'content': f'agent-{index} ' * 1800})
        messages.extend([
            {'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': f'call-{index}', 'type': 'function',
                'function': {'name': 'read_files', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': f'call-{index}', 'content': f'evidence-{index}'},
        ])
    return messages


@pytest.fixture
def events(monkeypatch):
    published = []
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event',
                        lambda task, event: published.append(event))
    counter = lambda messages, *a, **k: (_request_tokens(messages), 'test_request')
    monkeypatch.setattr('lib.tasks_pkg.compaction._tokens._count_tokens_authoritative', counter)
    monkeypatch.setattr(pipeline, '_count_tokens_authoritative', counter)
    monkeypatch.setattr(compact, '_archive_transcript', lambda *a, **k: 'archive-1')
    monkeypatch.setattr(compact, '_persist_compaction_candidate',
                        lambda *a, **k: {'status': 'completed'})
    monkeypatch.setattr('lib.tasks_pkg.context_composer.compose_task_context',
                        lambda messages, **kwargs: messages.append(dict(RESTORED)))
    return published


def _compact(messages, task, monkeypatch, summary):
    monkeypatch.setattr(compact, '_generate_query_aware_summary', lambda *a, **k: summary)
    return compact.force_compact_if_needed(
        messages, task=task, force=True, preserve_budget_tokens=16000,
        _compaction_trigger='dispatch_guard', _dispatch_prompt_target=50000,
        _dispatch_prompt_measure=_request_tokens, _result_meta={})


def _task():
    return {'id': 'compact-owner', '_userId': 7, 'convId': 'conversation',
            'config': {'model': 'gpt-6-astra'}}


def _admit(messages, task, monkeypatch):
    from lib.tasks_pkg.orchestrator._root_agent_loop import admit_round_context
    monkeypatch.setattr('lib.tasks_pkg.context_composer.execution_allowance.notice_decision',
                        lambda *a, **k: None)
    monkeypatch.setattr('lib.tasks_pkg.compaction._prompt_admission.enforce_dispatch_prompt_limit',
                        lambda messages, *a, **k: {'totalTokens': _request_tokens(messages),
                                                   'toolSchemaTokens': 16000})
    return admit_round_context(task, messages, [], round_num=1, model='gpt-6-astra')


def test_compaction_row_settles_on_the_rebuilt_request(monkeypatch, events):
    messages, task = _conversation(), _task()
    request_before = _request_tokens(messages)
    assert _compact(messages, task, monkeypatch, 'complete')
    compacted = list(messages)
    done = [event for event in events if event['type'] == 'compaction_done']
    assert [event['archiveId'] for event in done] == ['archive-1']
    # The receipt still describes the compactor's message list, not the request.
    assert done[0]['tokensAfter'] == _estimate_total_tokens(compacted)

    pipeline.recompose_context_after_compaction(messages, task=task)
    assert not [event for event in events if event['type'] == 'compaction_request_size']
    # Inbox and per-round input arrive after the compaction/composer stage.
    late = {'role': 'user', 'content': 'New owner steering after compaction. ' * 200}
    messages.append(late)
    # Final settlement must reuse the admitted count, not scan the prompt again.
    monkeypatch.setattr(pipeline, '_count_tokens_authoritative',
                        lambda *a, **k: pytest.fail('redundant final measurement'))
    _admit(messages, task, monkeypatch)
    sizes = [event for event in events if event['type'] == 'compaction_request_size']
    assert len(sizes) == 1
    size = sizes[0]
    assert size['archiveId'] == 'archive-1' and size['tokenCountKind'] == 'estimated'
    assert size['tokensBefore'] == request_before
    assert size['tokensAfter'] == _request_tokens(compacted) + len(RESTORED['content']) + len(late['content'])
    assert size['msgsAfter'] == len(messages) and RESTORED in messages
    assert size['reductionPct'] == round(
        (1 - size['tokensAfter'] / size['tokensBefore']) * 100, 1)

    # One compaction is reported once; a later restoration pass adds nothing.
    pipeline.recompose_context_after_compaction(messages, task=task)
    _admit(messages, task, monkeypatch)
    assert len([event for event in events if event['type'] == 'compaction_request_size']) == 1


def test_refused_final_admission_does_not_report_a_completed_request_size(monkeypatch, events):
    from lib.tasks_pkg.orchestrator._root_agent_loop import admit_round_context
    messages, task = _conversation(), _task()
    assert _compact(messages, task, monkeypatch, 'complete')
    pipeline.recompose_context_after_compaction(messages, task)
    monkeypatch.setattr('lib.tasks_pkg.context_composer.execution_allowance.notice_decision',
                        lambda *a, **k: None)
    def refuse(*args, **kwargs):
        raise ValueError('final request not admitted')
    monkeypatch.setattr('lib.tasks_pkg.compaction._prompt_admission.enforce_dispatch_prompt_limit', refuse)
    with pytest.raises(ValueError, match='not admitted'):
        admit_round_context(task, messages, [], round_num=1, model='gpt-6-astra')
    assert not [event for event in events if event['type'] == 'compaction_request_size']
    assert task['_compactionRequestSize']['archiveId'] == 'archive-1'


def test_size_diagnostic_failure_does_not_break_a_prepared_retry(monkeypatch, events):
    messages, task = _conversation(), _task()
    task['_compactionRequestSize'] = {'archiveId': 'archive-1', 'tokensBefore': 100000}
    def unavailable(*args, **kwargs):
        raise RuntimeError('counter unavailable')
    monkeypatch.setattr(pipeline, '_count_tokens_authoritative', unavailable)
    pipeline.settle_compaction_request_size(messages, task)
    assert not [event for event in events if event['type'] == 'compaction_request_size']
    assert task['_compactionRequestSize']['archiveId'] == 'archive-1'


def test_unadopted_compaction_reports_no_request_size(monkeypatch, events):
    messages, task = _conversation(), _task()
    assert not _compact(messages, task, monkeypatch, '')
    assert '_compactionRequestSize' not in task
    pipeline.recompose_context_after_compaction(messages, task=task)
    assert not [event for event in events if event['type'] == 'compaction_request_size']
