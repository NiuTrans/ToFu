"""Report stream replay, draft withdrawal and terminal authority at the worker."""

import pytest

from lib.llm.llm_errors import AbortedError


pytestmark = pytest.mark.unit
FINAL_BODY = '# Final report\n\nVerified evidence.'


@pytest.fixture
def report_case(monkeypatch):
    from tests.support.paper_report import isolated_report_case

    with isolated_report_case(monkeypatch) as case:
        yield case


@pytest.mark.parametrize('tool_drafts', [[], ['first draft', '', 'second draft']])
@pytest.mark.parametrize('streamed', [FINAL_BODY, FINAL_BODY * 2, '', 'superseded prose'])
def test_terminal_body_converges_without_losing_round_events(report_case, tool_drafts, streamed):
    run, _, _ = report_case
    calls = []

    def dispatch(messages, *, on_content, on_thinking, **kwargs):
        rnd = len(calls)
        calls.append(rnd)
        on_thinking(f'thought {rnd}')
        body = tool_drafts[rnd] if rnd < len(tool_drafts) else streamed
        if body:
            on_content(body)
        tool_calls = ([{'id': f'tool-{rnd}', 'function': {
            'name': 'web_search', 'arguments': '{"queries":"evidence"}'}}]
            if rnd < len(tool_drafts) else [])
        return ({'role': 'assistant', 'content': body if tool_calls else FINAL_BODY,
                 'tool_calls': tool_calls}, 'tool_calls' if tool_calls else 'stop', {})

    task, published = run(dispatch)
    expected = []
    for rnd, draft in enumerate(tool_drafts):
        expected.append({'type': 'thinking', 'delta': f'thought {rnd}', 'llmRound': rnd})
        if draft:
            expected.extend([
                {'type': 'delta', 'delta': draft, 'llmRound': rnd},
                {'type': 'delta_reset', 'llmRound': rnd},
            ])
    rnd = len(tool_drafts)
    expected.append({'type': 'thinking', 'delta': f'thought {rnd}', 'llmRound': rnd})
    if streamed:
        expected.append({'type': 'delta', 'delta': streamed, 'llmRound': rnd})
    if streamed != FINAL_BODY:
        expected.extend([{'type': 'delta_reset'}, {'type': 'delta', 'delta': FINAL_BODY}])
    projected = [{key: event[key] for key in ('type', 'delta', 'llmRound') if key in event}
                 for event in task['events']
                 if event['type'] in ('thinking', 'delta', 'delta_reset')]
    assert projected == expected
    assert task['status'] == 'done'
    assert task['full_text'] == task['result'] == FINAL_BODY
    assert [report.report for report in published] == [FINAL_BODY]
    assert [event['llmRound'] for event in task['events']
            if event['type'] == 'tool_done'] == list(range(len(tool_drafts)))
    assert [event['type'] for event in task['events']].count('done') == 1


@pytest.mark.parametrize('terminal', ['', None])
def test_empty_terminal_does_not_promote_streamed_prose(report_case, terminal):
    run, _, _ = report_case

    def dispatch(messages, *, on_content, **kwargs):
        on_content('noncanonical partial')
        return ({'role': 'assistant', 'content': terminal, 'tool_calls': []}, 'stop', {})

    task, published = run(dispatch)
    assert task['status'] == 'error'
    assert task['full_text'] == 'noncanonical partial'
    assert 'non-empty body' in task['error']['detail']
    assert not published
    assert not any(e['type'] in ('delta_reset', 'done') for e in task['events'])


@pytest.mark.parametrize('stop', ['event', 'exception'])
def test_stop_retains_streamed_partial_without_terminal_adoption(report_case, stop):
    run, task, _ = report_case

    def dispatch(messages, *, on_content, **kwargs):
        on_content('partial before stop')
        if stop == 'exception':
            raise AbortedError('owner stop')
        task['abort_event'].set()
        return ({'role': 'assistant', 'content': FINAL_BODY, 'tool_calls': []}, 'stop', {})

    task, published = run(dispatch)
    assert task['status'] == 'aborted'
    assert task['full_text'] == 'partial before stop'
    assert [e['partial'] for e in task['events'] if e['type'] == 'aborted'] == [
        'partial before stop']
    assert not published
    assert not any(e['type'] in ('delta_reset', 'done') for e in task['events'])


@pytest.mark.parametrize('failed_event', ['delta', 'delta_reset'])
def test_projection_failure_keeps_updated_partial_and_never_publishes(
        report_case, monkeypatch, failed_event):
    run, _, worker = report_case
    append = worker._append_report_event

    def emit(task, event):
        if event['type'] == failed_event:
            raise RuntimeError('projection unavailable')
        append(task, event)

    monkeypatch.setattr(worker, '_append_report_event', emit)

    def dispatch(messages, *, on_content, **kwargs):
        on_content('stale stream')
        return ({'role': 'assistant', 'content': FINAL_BODY, 'tool_calls': []}, 'stop', {})

    task, published = run(dispatch)
    assert task['status'] == 'error'
    assert task['full_text'] == ('stale stream' if failed_event == 'delta' else FINAL_BODY)
    assert 'projection unavailable' in task['error']['detail']
    assert not published
    assert not any(e['type'] == 'done' for e in task['events'])
