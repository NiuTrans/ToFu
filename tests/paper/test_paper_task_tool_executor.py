"""Full-catalog task adapter preserves events, executor authority and feedback."""

import threading
from types import SimpleNamespace

import pytest

from lib.llm.llm_errors import AbortedError
from lib.paper.tools import make_paper_task_tool_executor

pytestmark = pytest.mark.unit


def _adapter(executor):
    task = {'round_counter': 4, 'tool_rounds': [], '_worldVersion': 'world-2'}
    messages, events, feedback = [], [], []
    abort = threading.Event()
    shim = object()

    def append(target, **kwargs):
        assert target is messages
        feedback.append(kwargs)
        target.append({'role': 'tool', 'content': 'bounded-feedback'})
        return 'bounded-feedback'

    execute = make_paper_task_tool_executor(
        task, messages, user_question='declared question',
        abort_signal=abort, exec_shim=shim,
        result_budget=SimpleNamespace(append=append),
        paper_tool_executor=executor, on_tool_event=events.append,
        log_prefix='[Paper:Test]',
    )
    return execute, task, messages, events, feedback, abort, shim


@pytest.mark.parametrize('rejected', [False, True])
def test_tool_events_and_bounded_feedback_preserve_executor_result(rejected):
    calls = []
    content = 'result-' + 'x' * 5000
    raw = '{"queries":"one query"}'

    def executor(name, args, **kwargs):
        calls.append((name, args, kwargs))
        if rejected:
            kwargs['round_entry'].update(
                status='rejected', contractError={'code': 'bad_argument'})
        return content, [{'title': 'evidence'}], {'source': 'diag'}, {'web': 1}, ['web']

    execute, task, messages, events, feedback, abort, shim = _adapter(executor)
    assert execute(2, {'id': 'call-1', 'function': {
        'name': 'web_search', 'arguments': raw}}) == 'bounded-feedback'
    name, args, kwargs = calls[0]
    assert (name, args) == ('web_search', raw)
    assert kwargs['exec_shim'] is shim
    assert kwargs['abort']() is False
    abort.set()
    assert kwargs['abort']() is True
    assert kwargs['user_question'] == 'declared question'
    row = task['tool_rounds'][0]
    assert kwargs['round_entry'] is row
    assert row['roundNum'] == 5 and row['llmRound'] == 2
    assert row['toolArgs'] == raw
    assert row['status'] == ('rejected' if rejected else 'done')
    assert [event['type'] for event in events] == ['tool_start', 'tool_done']
    assert events[0]['toolCallId'] == events[1]['toolCallId'] == 'call-1'
    assert events[1]['toolContent'] == content[:4000]
    assert events[1]['status'] == row['status']
    assert events[1]['searchDiag'] == {'source': 'diag'}
    assert events[1]['engineBreakdown'] == row['engineBreakdown'] == {'web': 1}
    assert events[1]['verticals'] == row['verticals'] == ['web']
    if rejected:
        assert events[1]['contractError'] == {'code': 'bad_argument'}
    assert feedback[0]['content'] == content
    assert feedback[0]['tool_arguments']['queries'] == ['one query']
    assert feedback[0]['world_version'] == 'world-2'
    assert feedback[0]['round_entry'] is row
    assert messages == [{'role': 'tool', 'content': 'bounded-feedback'}]


@pytest.mark.parametrize('error', [RuntimeError('tool failed'), AbortedError('stop')])
def test_tool_failure_propagates_without_false_done_or_feedback(error):
    def executor(*args, **kwargs):
        raise error

    execute, task, messages, events, feedback, _, _ = _adapter(executor)
    with pytest.raises(type(error)) as caught:
        execute(0, {'id': 'call-error', 'function': {
            'name': 'run_command', 'arguments': {'command': 'echo test'}}})
    assert caught.value is error
    assert [event['type'] for event in events] == ['tool_start']
    assert events[0]['toolName'] == 'code_exec'
    assert task['tool_rounds'][0]['status'] == 'searching'
    assert messages == feedback == []


@pytest.mark.parametrize('workflow', ['qa', 'deepen'])
@pytest.mark.parametrize('outcome', ['done', 'abort', 'error'])
def test_workers_preserve_draft_reset_feedback_and_terminal_policy(
    monkeypatch, workflow, outcome,
):
    import uuid
    import lib.paper.qa_engine as qa
    import lib.paper.deepen_engine as deepen
    from lib.paper.qa_runtime import _new_qa_task

    worker = qa if workflow == 'qa' else deepen
    task_id = 'adapter-' + uuid.uuid4().hex
    paper_hash = 'synthetic-' + uuid.uuid4().hex
    config = {'tools': {'schemaBudgetTokens': 0}}
    if workflow == 'qa':
        task = _new_qa_task(
            task_id, paper_hash, 'en', 'kimi-k3', question='declared question',
            user_id=1, config=config)
    else:
        task = deepen._new_deepen_task(
            task_id, paper_hash, 'en', 'kimi-k3', section_idx=0,
            mode='deeper', section_heading='declared heading', user_id=1,
            config=config)
        monkeypatch.setattr(deepen, '_write_deepen_cache', lambda *a, **kw: True)
        monkeypatch.setattr(deepen, '_accumulate_deepen_cost', lambda *a, **kw: None)

    calls = []
    requests = []
    messages = [{'role': 'system', 'content': 'preserved policy'},
                {'role': 'user', 'content': 'grounded source question'}]

    def dispatch(history, on_content, **kwargs):
        requests.append((list(history), kwargs))
        assert kwargs['strict_model'] is True
        assert kwargs['prefer_model'] == 'kimi-k3'
        assert kwargs['max_tokens'] == (8000 if workflow == 'qa' else 16000)
        assert kwargs['thinking_enabled'] is False
        if len(requests) == 1:
            on_content('interim draft')
            return ({'role': 'assistant', 'content': 'interim draft',
                     'tool_calls': [{'id': 'call-worker', 'function': {
                         'name': 'web_search',
                         'arguments': '{"queries":"grounded query"}'}}]},
                    'tool_calls', {})
        assert outcome == 'done'
        assert any(message.get('role') == 'tool' and
                   'tool evidence' in message.get('content', '')
                   for message in history)
        assert 'preserved policy' in history[0]['content']
        on_content('final answer')
        return ({'role': 'assistant', 'content': 'final answer',
                 'tool_calls': []}, 'stop', {})

    def executor(name, raw, **kwargs):
        calls.append((name, raw, kwargs))
        assert kwargs['user_question'] == (
            'declared question' if workflow == 'qa' else 'declared heading')
        assert kwargs['exec_shim'] is not None
        assert kwargs['abort']() is False
        if outcome == 'error':
            raise RuntimeError('injected tool failure')
        if outcome == 'abort':
            task['abort_event'].set()
            assert kwargs['abort']() is True
            raise AbortedError('injected owner stop')
        return 'tool evidence', [], None, None, None

    monkeypatch.setattr(worker, 'dispatch_stream', dispatch)
    monkeypatch.setattr(worker, 'execute_paper_tool', executor)
    if workflow == 'qa':
        qa._run_qa_task(task, messages)
    else:
        deepen._run_deepen_task(
            task, messages, paper_hash=paper_hash,
            section={'heading': 'declared heading', 'hash': 'section-hash'},
            ui_lang='en')
    assert len(calls) == 1
    assert calls[0][0] == 'web_search'
    assert task['tool_rounds'][0]['toolCallId'] == 'call-worker'
    assert task['agentUsageV1']
    # QA historically maps a thrown tool AbortedError to error, while Deepen
    # has an explicit aborted settlement. Do not flatten their outer policy.
    expected_status = ('done' if outcome == 'done' else
                       'aborted' if outcome == 'abort' and workflow == 'deepen'
                       else 'error')
    assert task['status'] == expected_status, task.get('error')
    assert task['full_text'] == ('final answer' if outcome == 'done' else '')
    if outcome == 'done':
        assert task['tool_rounds'][0]['status'] == 'done'
        assert len(requests) == 2
    else:
        assert task['tool_rounds'][0]['status'] == 'searching'
        assert len(requests) == 1
