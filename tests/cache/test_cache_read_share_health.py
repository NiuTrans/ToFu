"""Same-route read-share splits are measured, warned about and labelled live.

On 2026-10-23..28 the root lane on the Sankuai Claude route read back ~3% of
its prompt while sub-agents on the same key, URL and model read ~96%. Every
cold round was labelled "upstream" because its own prefix was byte-identical,
so the split stayed unexplained for five days. The split itself is the signal:
``_health`` measures it per record dimension, warns once while it persists and
labels the cold side's rounds; round records carry the tool surface actually
sent so the discriminating property is visible.
"""
from __future__ import annotations

import json

import pytest

from lib.tasks_pkg.cache_tracking import _health

pytestmark = pytest.mark.unit

MODEL = 'claude-opus-5.5'


def _rec(lane, read, write, *, call=5, name_bytes=None, model=MODEL):
    rec = {'call': call, 'model': model, 'lane': lane,
           'cache_read': read, 'cache_write': write}
    if name_bytes is not None:
        rec['wire_tool_name_bytes'] = name_bytes
    return rec


class _Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def _captured(monkeypatch, logger, level):
    lines = []
    monkeypatch.setattr(logger, level, lambda fmt, *args: lines.append(fmt % args))
    return lines


def _seed_split(monitor, cold_lane, warm_lane, rounds=25):
    for _ in range(rounds):
        monitor.observe(_rec(cold_lane, 3_000, 97_000, name_bytes=628))
        monitor.observe(_rec(warm_lane, 95_000, 5_000, name_bytes=150))


def test_parse_round_record_keeps_payloads_and_rejects_torn_stamps():
    line = ('2026-10-28 04:10:41 [INFO] x: [CacheRoundRecord] '
            '{"call": 2, "lane": "conversation"}')
    assert _health.parse_round_record(line) == (
        '2026-10-28 04:10:41', {'call': 2, 'lane': 'conversation'})
    stamp, rec = _health.parse_round_record('\x00' * 19 + line[19:])
    assert stamp == '' and rec['call'] == 2
    assert _health.parse_round_record('2026-10-28 04:10:41 [INFO] other') is None
    assert _health.parse_round_record('[CacheRoundRecord] [1, 2]') is None
    assert _health.parse_round_record('[CacheRoundRecord] {broken') is None


def test_tool_name_bytes_split_names_the_cold_and_warm_groups():
    records = ([_rec('conversation', 3_000, 97_000, name_bytes=628)] * 25
               + [_rec('isolated', 95_000, 5_000, name_bytes=150)] * 25)
    split = _health.read_share_split(records, 'tool_name_bytes')
    (divergence,) = split['divergences']
    assert divergence['cold_group'] == '513-1024'
    assert divergence['warm_group'] == '129-256'
    assert divergence['cold_share'] == pytest.approx(0.03)
    assert split['divergent_models'] == [MODEL]
    text = _health.describe_divergence('tool_name_bytes', divergence)
    assert 'tool_name_bytes=513-1024 read back 3%' in text
    assert 'tool_name_bytes=129-256 read 95%' in text


def test_tally_folds_every_dimension_and_skips_cold_starts():
    tally = _health.ReadShareTally()
    for _ in range(30):
        tally.add(_rec('conversation', 3_000, 97_000, name_bytes=628))
        tally.add(_rec('isolated', 95_000, 5_000, name_bytes=150))
        tally.add(_rec('isolated', 0, 100_000, call=1, name_bytes=150))
    assert tally.split('lane')['divergent_models'] == [MODEL]
    assert tally.split('tool_name_bytes')['divergent_models'] == [MODEL]
    # No record carries a logical tool count, so that dimension cannot split.
    assert tally.split('tool_surface')['divergent_models'] == []
    assert {row['group']: row['rounds'] for row in tally.split('lane')['rows']} == {
        'conversation': 30, 'isolated': 30}


def test_monitor_warns_once_per_split_and_labels_only_cold_rounds(monkeypatch):
    warnings = _captured(monkeypatch, _health.logger, 'warning')
    clock = _Clock()
    monitor = _health.ReadShareMonitor(clock=clock)
    for _ in range(25):
        monitor.observe(_rec('conversation', 3_000, 97_000, name_bytes=628))
        monitor.observe(_rec('isolated', 95_000, 5_000, name_bytes=150))
        clock.now += 30
    (warning,) = warnings
    assert warning.startswith(f'[CacheHealth] same-route prompt-cache split on model={MODEL}')
    assert 'lane=conversation read back 3%' in warning
    assert 'tool_name_bytes=513-1024' in warning
    assert 'scripts/cache_ab_probe.py' in warning
    assert monitor.cold_group(_rec('conversation', 0, 0, name_bytes=628)) == (
        'lane=conversation,tool_name_bytes=513-1024')
    assert monitor.cold_group(_rec('isolated', 0, 0, name_bytes=150)) == ''
    assert monitor.cold_group(_rec('conversation', 0, 0, model='other-model')) == ''
    assert monitor.cold_group(_rec('conversation', 0, 0, call=1)) == ''


def test_monitor_repeats_a_persisting_split_once_per_window(monkeypatch):
    warnings = _captured(monkeypatch, _health.logger, 'warning')
    clock = _Clock()
    monitor = _health.ReadShareMonitor(window_s=600, clock=clock)
    _seed_split(monitor, 'conversation', 'isolated')
    clock.now += 300
    _seed_split(monitor, 'conversation', 'isolated', rounds=5)
    assert len(warnings) == 1
    clock.now += 301
    _seed_split(monitor, 'conversation', 'isolated')
    assert len(warnings) == 2


def test_monitor_expires_old_rounds_and_reports_the_split_cleared(monkeypatch):
    infos = _captured(monkeypatch, _health.logger, 'info')
    _captured(monkeypatch, _health.logger, 'warning')
    clock = _Clock()
    monitor = _health.ReadShareMonitor(window_s=600, clock=clock)
    _seed_split(monitor, 'conversation', 'isolated')
    assert monitor.cold_group(_rec('conversation', 0, 0)) == 'lane=conversation'
    clock.now += 601
    for _ in range(25):
        monitor.observe(_rec('conversation', 90_000, 10_000))
        monitor.observe(_rec('isolated', 95_000, 5_000))
    assert monitor.cold_group(_rec('conversation', 0, 0)) == ''
    assert any('cleared' in line and MODEL in line for line in infos)


def test_monitor_dampens_a_split_that_flaps_within_the_window(monkeypatch):
    warnings = _captured(monkeypatch, _health.logger, 'warning')
    infos = _captured(monkeypatch, _health.logger, 'info')
    monitor = _health.ReadShareMonitor(clock=_Clock())
    _seed_split(monitor, 'conversation', 'isolated')
    for _ in range(200):
        monitor.observe(_rec('conversation', 95_000, 5_000, name_bytes=628))
    root_round = _rec('conversation', 0, 0, name_bytes=628)
    assert monitor.cold_group(root_round) == ''
    for _ in range(500):
        monitor.observe(_rec('conversation', 3_000, 97_000, name_bytes=628))
    assert monitor.cold_group(root_round) == (
        'lane=conversation,tool_name_bytes=513-1024')
    assert len(warnings) == 1
    assert len([line for line in infos if 'cleared' in line]) == 1


def test_cold_group_ignores_a_split_older_than_the_window(monkeypatch):
    _captured(monkeypatch, _health.logger, 'warning')
    clock = _Clock()
    monitor = _health.ReadShareMonitor(window_s=600, clock=clock)
    _seed_split(monitor, 'conversation', 'isolated')
    assert monitor.cold_group(_rec('conversation', 0, 0)) == 'lane=conversation'
    clock.now += 601
    assert monitor.cold_group(_rec('conversation', 0, 0)) == ''


def test_monitor_ignores_cold_starts_and_thin_groups(monkeypatch):
    warnings = _captured(monkeypatch, _health.logger, 'warning')
    monitor = _health.ReadShareMonitor(clock=_Clock())
    for _ in range(50):
        monitor.observe(_rec('conversation', 0, 100_000, call=1))
    for _ in range(19):
        monitor.observe(_rec('conversation', 3_000, 97_000))
        monitor.observe(_rec('isolated', 95_000, 5_000))
    assert warnings == []
    assert monitor.cold_group(_rec('conversation', 0, 0)) == ''


def test_monitor_state_stays_bounded_by_its_window():
    clock = _Clock()
    monitor = _health.ReadShareMonitor(window_s=600, clock=clock)
    for index in range(500):
        monitor.observe(_rec('conversation', 90_000, 10_000,
                             name_bytes=100 + index % 3 * 200))
        clock.now += 60
    cells = [len(group_cells) for groups in monitor._cells.values()
             for group_cells in groups.values()]
    assert cells and max(cells) <= 10


def _detect_records(monkeypatch, conv_id, usages):
    import lib.tasks_pkg.cache_tracking._detect as det

    records = []
    real_info = det.logger.info

    def _capture(fmt, *args):
        if fmt.startswith('[CacheRoundRecord]'):
            records.append(json.loads(args[0]))
        return real_info(fmt, *args)

    monkeypatch.setattr(det.logger, 'info', _capture)
    for usage in usages:
        det.detect_cache_break(
            conv_id, [{'role': 'user', 'content': 'x'}], None, MODEL, usage,
            user_id=1, durable=False)
    return records


def _usage(read, write, **extra):
    return {'prompt_tokens': 1000, 'completion_tokens': 10,
            'cache_read_input_tokens': read,
            'cache_creation_input_tokens': write, **extra}


def test_round_records_carry_the_sent_tool_surface(monkeypatch):
    monkeypatch.setattr(_health, 'read_share_monitor', _health.ReadShareMonitor())
    surface = {'count': 36, 'name_bytes': 628}
    records = _detect_records(monkeypatch, 'conv-health-surface', [
        _usage(0, 200_000, _wire_tool_surface=surface),
        _usage(195_000, 5_000, _wire_tool_surface=surface)])
    assert [(r['wire_tools'], r['wire_tool_name_bytes']) for r in records] == [
        (36, 628), (36, 628)]
    assert not any('cold_group' in r for r in records)

    bare = _detect_records(monkeypatch, 'conv-health-no-surface', [
        _usage(0, 200_000), _usage(195_000, 5_000)])
    assert bare and not any(
        'wire_tools' in r or 'wire_tool_name_bytes' in r for r in bare)


def test_cold_side_rounds_are_labelled_in_record_and_break_report(monkeypatch):
    import lib.tasks_pkg.cache_tracking._report as report

    _captured(monkeypatch, _health.logger, 'warning')
    monitor = _health.ReadShareMonitor()
    for _ in range(25):
        monitor.observe(_rec('unscoped', 3_000, 97_000))
        monitor.observe(_rec('isolated', 95_000, 5_000))
    monkeypatch.setattr(_health, 'read_share_monitor', monitor)
    break_warnings = _captured(monkeypatch, report.logger, 'warning')

    records = _detect_records(monkeypatch, 'conv-health-cold-side', [
        _usage(0, 200_000), _usage(0, 200_000)])

    assert 'cold_group' not in records[0]
    assert records[-1]['cold_group'] == 'lane=unscoped'
    (warning,) = [line for line in break_warnings if 'NO CACHE REUSE' in line]
    assert 'cold side of a live same-route split (lane=unscoped' in warning
