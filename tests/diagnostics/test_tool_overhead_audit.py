"""Focused contract for debug/tool_overhead_audit.py (shipped read-only diagnostic).

Synthetic event logs pin how each interval is derived from the committed
event fields (docs/contracts/EVENTS.md) on the task execution clock.
"""
from __future__ import annotations

import json

import pytest

from debug import tool_overhead_audit as audit

pytestmark = pytest.mark.unit


def _round_events(number, start, *, calls, stream_end, tool_exec, round_end):
    events = [{'type': 'model_request_start', 'requestLevel': 'logical',
               'roundNum': number, 'executionAtMs': start}]
    for call in calls:
        events.append({'type': 'tool_start', 'toolCallId': call['id'],
                       'toolName': call['tool'], 'executionAtMs': call['announced']})
    events += [
        {'type': 'model_request_complete', 'requestLevel': 'provider_attempt',
         'status': 'succeeded', 'roundNum': number, 'executionAtMs': stream_end},
        {'type': 'model_request_complete', 'requestLevel': 'logical',
         'status': 'succeeded', 'roundNum': number, 'executionAtMs': stream_end + 20},
        {'type': 'phase', 'phase': 'tool_exec', 'executionAtMs': tool_exec},
    ]
    for call in calls:
        if 'effect' in call:
            events.append({'type': 'tool_effect', 'toolCallId': call['id'],
                           'executionEffect': {'state': 'entered'},
                           'executionAtMs': call['effect']})
        if 'admission' in call:
            events.append({'type': 'workspace_admission_wait', 'toolCallId': call['id'],
                           'waitedMs': call['admission'], 'executionAtMs': call['effect'] + 1})
        if 'exec_start' in call:
            events.append({'type': 'tool_progress', 'toolCallId': call['id'],
                           'execStartTs': call['exec_start'],
                           'executionAtMs': call['exec_start'] + 300})
        events += [
            {'type': 'tool_result', 'toolCallId': call['id'], 'tEnd': call['end'],
             'executionAtMs': call['end'] + 10},
            {'type': 'tool_complete', 'toolCallId': call['id'],
             'executionAtMs': call['complete']},
        ]
    events.append({'type': 'round_end', 'roundNum': number, 'executionAtMs': round_end})
    return events


def _two_round_log():
    first = _round_events(1, 0, stream_end=1000, tool_exec=1600, round_end=3700, calls=[
        {'id': 'c1', 'tool': 'run_command', 'announced': 900, 'effect': 1650,
         'admission': 100, 'exec_start': 2000, 'end': 3000, 'complete': 3050}])
    second = _round_events(2, 4500, stream_end=5000, tool_exec=5400, round_end=6000, calls=[
        {'id': 'c2', 'tool': 'read_files', 'announced': 4800, 'end': 4900, 'complete': 5450},
        {'id': 'c3', 'tool': 'read_files', 'announced': 4950, 'end': 5600, 'complete': 5700}])
    closing = [{'type': 'model_request_start', 'requestLevel': 'logical',
                'roundNum': 3, 'executionAtMs': 6800}]
    return first + second + closing


def test_intervals_follow_committed_event_fields():
    rows = {row['toolCallId']: row for row in audit.tool_overhead_rows(_two_round_log())}

    run = rows['c1']
    assert run['lead'] == 100                # stream end 1000 - announce 900
    assert run['dispatch'] == 600            # tool_exec 1600 - stream end
    assert run['entry'] == 50                # effect 1650 - tool_exec
    assert run['spawn'] == 250               # exec 2000 - effect 1650 - admission 100
    assert run['admission'] == 100
    assert run['span'] == 2000               # tEnd 3000 - ready 1000
    assert run['settle'] == 50               # complete 3050 - tEnd 3000
    assert run['close'] == 650               # round_end 3700 - complete 3050
    assert run['next'] == 800                # next start 4500 - round_end 3700

    # A call finished by streaming prefetch waits for dispatch before settling.
    prefetched = rows['c2']
    assert prefetched['span'] == -100
    assert prefetched['settle'] == 50        # complete 5450 - tool_exec 5400
    assert prefetched['entry'] is None and prefetched['spawn'] is None
    assert prefetched['close'] is None       # round intervals live on the last call
    last = rows['c3']
    assert last['dispatch'] == 400
    assert last['close'] == 300 and last['next'] == 800


def test_retry_frames_do_not_move_round_boundaries():
    events = _two_round_log()
    # A retried request for round 2 repeats its logical start and closes an
    # attempt early; neither is the next round nor the tool round's end.
    retry_index = next(index for index, event in enumerate(events)
                       if event.get('roundNum') == 2)
    events[retry_index + 1:retry_index + 1] = [
        {'type': 'round_end', 'roundNum': 2, 'executionAtMs': 4600},
        {'type': 'model_request_start', 'requestLevel': 'logical', 'roundNum': 2,
         'executionAtMs': 4700}]
    rows = {row['toolCallId']: row for row in audit.tool_overhead_rows(events)}
    assert rows['c1']['next'] == 800
    assert rows['c3']['close'] == 300


def test_summary_quantiles_and_cli_json(tmp_path, capsys):
    path = tmp_path / 'events.json'
    path.write_text(json.dumps({'events': _two_round_log()}), encoding='utf-8')
    assert audit.main(['--events', str(path), '--json', '--rows']) == 0
    report = json.loads(capsys.readouterr().out)
    reads = report['summary']['read_files']
    assert reads['calls'] == 2
    assert reads['intervals']['dispatch'] == {
        'n': 2, 'p50': 400, 'p90': 400, 'mean': 400, 'sum': 800}
    assert list(report['summary']) == ['read_files', 'run_command']
    assert len(report['rows']) == 3

    assert audit.main(['--events', str(path)]) == 0
    text = capsys.readouterr().out
    assert 'run_command' in text and 'dispatch' in text


def test_api_paging_keeps_only_the_fields_the_audit_reads():
    events = _two_round_log()
    for event in events:
        event['messages'] = ['x' * 1000]

    class _Pages:
        def __init__(self):
            self.paths = []

        def get(self, path):
            self.paths.append(path)
            cursor = int(path.rsplit('=', 1)[1])
            batch = events[cursor:cursor + 4]
            return {'events': batch, 'next_cursor': cursor + len(batch),
                    'caught_up': cursor + len(batch) >= len(events)}

    api = _Pages()
    fetched = audit.fetch_task_events(api, 'task/1')
    assert api.paths[0] == '/api/v4/tasks/task%2F1/events?cursor=0'
    assert len(fetched) == len(events)
    assert all('messages' not in event for event in fetched)
    assert audit.tool_overhead_rows(fetched) == audit.tool_overhead_rows(events)


def test_unreadable_source_fails_without_partial_report(tmp_path, capsys):
    path = tmp_path / 'events.json'
    path.write_text('{"events": 3}', encoding='utf-8')
    assert audit.main(['--events', str(path)]) == 1
    assert 'cannot read events' in capsys.readouterr().err
