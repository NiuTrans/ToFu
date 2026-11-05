"""Per-round cache records name their lane and tool surface; the report splits by both.

A gateway can serve requests on one route from caches that behave differently
by request shape. On 2026-10-23 claude-opus-5.5 requests carrying 53-54 tool
schemas read back 2-6% of their prompt for hours while requests with 11-18
tools on the same key and URL read 93-100%, in the root and the sub-agent lane
alike. ``lane`` and ``tools`` make both dimensions first-class record fields
and the waste report flags a model whose groups diverge on either.
"""

from __future__ import annotations

import importlib.util
import json
import os

import pytest

from lib.tasks_pkg.cache_tracking._health import SPLIT_DIMENSIONS, read_share_split

pytestmark = pytest.mark.unit

_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_PATH = os.path.join(_HERE, '..', '..', 'scripts', 'cache_waste_report.py')


def _records(monkeypatch, task, *, tools=None):
    import lib.tasks_pkg.cache_tracking._detect as det
    from lib.tasks_pkg.cache_tracking._state import cache_tracking_scope

    payloads = []
    real_info = det.logger.info

    def _capture(fmt, *args):
        if fmt.startswith('[CacheRoundRecord]'):
            payloads.append(json.loads(args[0]))
        return real_info(fmt, *args)

    monkeypatch.setattr(det.logger, 'info', _capture)

    def _rounds():
        for read, write in [(0, 200_000), (195_000, 5_000)]:
            det.detect_cache_break(
                task['convId'], [{'role': 'user', 'content': 'x'}], tools, 'm',
                {'prompt_tokens': 1000, 'completion_tokens': 10,
                 'cache_read_input_tokens': read,
                 'cache_creation_input_tokens': write},
                user_id=task['_userId'], durable=False)

    if task.get('id'):
        with cache_tracking_scope(task):
            _rounds()
    else:
        _rounds()
    return payloads


@pytest.mark.parametrize(('extra', 'lane'), [
    ({}, 'conversation'),
    ({'_transientRuntime': True}, 'isolated'),
])
def test_scoped_records_carry_their_lane(monkeypatch, extra, lane):
    task = {'convId': f'conv-lane-{lane}', 'id': f'task-lane-{lane}',
            '_userId': 1, **extra}
    payloads = _records(monkeypatch, task)
    assert len(payloads) == 2
    assert {p['lane'] for p in payloads} == {lane}
    assert not any(task['id'] in json.dumps(p) for p in payloads)


def test_unscoped_records_say_so(monkeypatch):
    payloads = _records(monkeypatch, {'convId': 'conv-lane-unscoped', '_userId': 1})
    assert {p['lane'] for p in payloads} == {'unscoped'}


def test_records_carry_the_tool_count(monkeypatch):
    tools = [{'type': 'function', 'function': {'name': f'tool_{i}', 'parameters': {}}}
             for i in range(3)]
    payloads = _records(monkeypatch, {'convId': 'conv-tools-3', '_userId': 1},
                        tools=tools)
    assert [p['tools'] for p in payloads] == [3, 3]


def test_records_without_tools_omit_the_count(monkeypatch):
    payloads = _records(monkeypatch, {'convId': 'conv-tools-none', '_userId': 1})
    assert payloads and not any('tools' in p for p in payloads)


def _row(lane, read, write, *, model='claude-opus-5', call=5, tools=None):
    record = {'bucket': 'no_break', 'call': call, 'gap_s': 20.0, 'model': model,
              'cache_read': read, 'cache_write': write}
    if lane is not None:
        record['lane'] = lane
    if tools is not None:
        record['tools'] = tools
    return record


def _shares(split):
    return {(row['model'], row['group']): row['read_share'] for row in split['rows']}


def test_lane_split_flags_a_model_whose_root_lane_stopped_reading():
    records = ([_row('conversation', 3_000, 97_000)] * 30
               + [_row('isolated', 95_000, 5_000)] * 30)
    split = read_share_split(records, 'lane')
    assert _shares(split)[('claude-opus-5', 'conversation')] == pytest.approx(0.03)
    assert _shares(split)[('claude-opus-5', 'isolated')] == pytest.approx(0.95)
    assert split['divergent_models'] == ['claude-opus-5']


def test_split_needs_enough_rounds_and_ignores_cold_and_unrecorded_rows():
    records = ([_row('conversation', 3_000, 97_000)] * 5
               + [_row('isolated', 95_000, 5_000)] * 30
               + [_row('conversation', 0, 100_000, call=1)] * 30
               + [_row(None, 0, 100_000)] * 30)
    split = read_share_split(records, 'lane')
    groups = {(row['group'], row['rounds']) for row in split['rows']}
    assert ('conversation', 5) in groups
    assert ('(unrecorded)', 30) in groups
    assert split['divergent_models'] == []


def test_healthy_lanes_are_not_flagged():
    records = ([_row('conversation', 90_000, 10_000)] * 30
               + [_row('isolated', 95_000, 5_000)] * 30)
    assert read_share_split(records, 'lane')['divergent_models'] == []


def test_tool_surface_split_finds_the_discriminator_the_lane_split_cannot():
    cold, warm = (3_000, 97_000), (95_000, 5_000)
    records = ([_row('conversation', *cold, tools=54)] * 20
               + [_row('conversation', *warm, tools=18)] * 20
               + [_row('isolated', *cold, tools=53)] * 20
               + [_row('isolated', *warm, tools=11)] * 20)
    assert read_share_split(records, 'lane')['divergent_models'] == []
    surface = read_share_split(records, 'tool_surface')
    shares = _shares(surface)
    assert shares[('claude-opus-5', '33-64')] == pytest.approx(0.03)
    assert shares[('claude-opus-5', '<=16')] == pytest.approx(0.95)
    assert shares[('claude-opus-5', '17-32')] == pytest.approx(0.95)
    assert surface['divergent_models'] == ['claude-opus-5']


if not os.path.isfile(_MOD_PATH):
    pytest.skip('scripts/cache_waste_report.py not shipped in opensource',
                allow_module_level=True)


def _report_module():
    spec = importlib.util.spec_from_file_location('cache_waste_report', _MOD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_report_carries_every_split_dimension():
    report = _report_module()
    records = [_row('conversation', 3_000, 97_000, tools=54)] * 3
    rep = report.build_report(records, 20_000, 1e-5, 1e-6, 'claude-opus-5')
    assert set(rep['read_share_splits']) == set(SPLIT_DIMENSIONS) == {
        'lane', 'tool_surface', 'tool_name_bytes'}


def test_report_names_the_cold_group_and_the_next_step(capsys):
    report = _report_module()
    records = ([_row('conversation', 3_000, 97_000)] * 30
               + [_row('isolated', 95_000, 5_000)] * 30)
    report.print_report(report.build_report(
        records, 20_000, 1e-5, 1e-6, 'claude-opus-5'))
    out = capsys.readouterr().out
    assert '!! claude-opus-5: lane=conversation read back 3%' in out
    assert 'scripts/cache_ab_probe.py' in out
