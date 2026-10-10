"""tests/events/test_event_log_invariants.py — Durable event-log invariant checker.

Pins ``lib/tasks_pkg/event_invariants.py``:

* unit level — synthetic windows exercise every rule in both directions
  (clean → ``()``; broken → the named violation; ``allow_open_tail``
  suppresses only end-of-log openness, never ordering mismatches);
* integration level — a REAL scripted ``execute_tool_pipeline`` round
  (harness mirrored from ``tests/events/test_event_schema.py`` §C) yields a
  violation-free emission stream;
* runtime level — ``maybe_check_task_log`` is a no-op without the env flag
  and logs violations at ERROR over a seeded durable log when enabled.
"""

from __future__ import annotations

import threading
import uuid

import pytest

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]


def _check(events, **kw):
    from lib.tasks_pkg.event_invariants import check_event_log_invariants
    return check_event_log_invariants(events, **kw)


def _rules(violations):
    return sorted(v.rule for v in violations)


def _clean_window():
    return [
        {'event_id': 0, 'payload': {'type': 'round_start', 'roundNum': 1}},
        {'event_id': 1, 'payload': {'type': 'model_request_start',
                                    'spanId': 's1', 'roundNum': 1}},
        {'event_id': 2, 'payload': {'type': 'messages_snapshot',
                                    'kind': 'request',
                                    'inspectorRef': {'roundNum': 1}}},
        {'event_id': 3, 'payload': {'type': 'tool_start',
                                    'toolCallId': 'c1', 'roundNum': 1}},
        {'event_id': 4, 'payload': {'type': 'tool_complete',
                                    'toolCallId': 'c1', 'roundNum': 1}},
        {'event_id': 5, 'payload': {'type': 'model_request_complete',
                                    'spanId': 's1'}},
        {'event_id': 6, 'payload': {'type': 'round_end', 'roundNum': 1}},
    ]


def test_clean_window_has_no_violations():
    assert _check(_clean_window()) == ()


def test_seq_regression_flagged():
    rows = [dict(_clean_window()[0]), {'event_id': 0, 'payload': {'type': 'phase'}}]
    assert 'seq-regression' in _rules(_check(rows, allow_open_tail=True))


def test_unknown_type_flagged():
    rows = [{'event_id': 0, 'payload': {'type': 'from_a_newer_runtime'}}]
    assert 'unknown-type' in _rules(_check(rows))


def test_round_pairing_mismatches():
    start_1 = {'type': 'round_start', 'roundNum': 1}
    start_2 = {'type': 'round_start', 'roundNum': 2}
    end_2 = {'type': 'round_end', 'roundNum': 2}
    assert 'round-pairing' in _rules(_check([start_1, start_2],
                                            allow_open_tail=True))
    assert 'round-pairing' in _rules(_check([end_2]))
    assert 'round-pairing' in _rules(_check([start_1, end_2],
                                            allow_open_tail=True))
    # Open at end is a violation only for a settled (non-live, non-abort) log.
    assert 'round-pairing' in _rules(_check([start_1]))
    assert 'round-pairing' not in _rules(_check([start_1],
                                                allow_open_tail=True))


def test_tool_settlement_semantics():
    open_only = [{'type': 'tool_start', 'toolCallId': 'c1', 'roundNum': 1}]
    assert 'tool-settlement' in _rules(_check(open_only))
    assert 'tool-settlement' not in _rules(_check(open_only,
                                                  allow_open_tail=True))
    # A settle without a start is tolerated (non-streamed / replay paths may
    # elide the announcement); the reverse direction is the invariant.
    settle_only = [{'type': 'tool_complete', 'toolCallId': 'c9'}]
    assert _check(settle_only) == ()
    # Occurrence FIFO: two starts, one settle → one occurrence still open.
    recycled = [
        {'type': 'tool_start', 'toolCallId': 'c1', 'roundNum': 1},
        {'type': 'tool_start', 'toolCallId': 'c1', 'roundNum': 2},
        {'type': 'tool_complete', 'toolCallId': 'c1', 'roundNum': 2},
    ]
    assert 'tool-settlement' in _rules(_check(recycled))
    assert _check(recycled, allow_open_tail=True) == ()


def test_model_span_settlement():
    open_span = [{'type': 'model_request_start', 'spanId': 's1'}]
    assert 'model-span-settlement' in _rules(_check(open_span))
    assert 'model-span-settlement' not in _rules(
        _check(open_span, allow_open_tail=True))
    # Complete without a visible start is tolerated (earlier log window).
    assert _check([{'type': 'model_request_complete', 'spanId': 's9'}]) == ()


def test_request_evidence_rule():
    request_without_evidence = [
        {'type': 'model_request_start', 'spanId': 's1', 'roundNum': 3},
        {'type': 'model_request_complete', 'spanId': 's1'},
    ]
    assert 'request-evidence' in _rules(_check(request_without_evidence))
    with_evidence = request_without_evidence + [
        {'type': 'messages_snapshot', 'kind': 'request', 'roundNum': 3},
    ]
    assert _check(with_evidence) == ()
    # A 'state' snapshot is post-tool evidence, never request evidence.
    state_only = request_without_evidence + [
        {'type': 'messages_snapshot', 'kind': 'state', 'roundNum': 3},
    ]
    assert 'request-evidence' in _rules(_check(state_only))


# ═══════════════════════════════════════════════════════════════════
#  Integration: REAL scripted execute_tool_pipeline round (harness mirrors
#  tests/events/test_event_schema.py §C — each suite carries its own harness).
# ═══════════════════════════════════════════════════════════════════

def _mk_task(**over):
    task = {
        'id': f'inv-{uuid.uuid4().hex[:8]}',
        'convId': 'cv-inv-1',
        '_userId': 1,
        'status': 'running',
        'aborted': False,
        'model': 'test-model',
        'config': {'tools': {'resultEnvelope': 'legacy'}},
        'events': [],
        'events_lock': threading.Lock(),
        '_dispatch_heartbeat': 0.0,
        '_t_last_event': 0.0,
        '_attended': False,
    }
    task.update(over)
    return task


def _mk_tc(tc_id: str, fn_name: str, seq: int):
    from lib.tasks_pkg.tool_display import _build_tool_round_entry
    _n, round_entry, _ev = _build_tool_round_entry(
        fn_name, {}, tc_id, '{}', seq, False)
    tc = {'id': tc_id, 'type': 'function',
          'function': {'name': fn_name, 'arguments': '{}'}}
    return (tc, fn_name, tc_id, {}, round_entry['roundNum'], round_entry, None)


@pytest.fixture()
def pipeline_run(monkeypatch):
    """Run one scripted real pipeline round; return the emitted events."""
    emitted: list[dict] = []

    import lib.tasks_pkg.tool_dispatch._heartbeat as heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as pipeline
    from lib.tasks_pkg.executor import _finalize as exec_finalize
    monkeypatch.setattr(pipeline, 'append_event',
                        lambda task, event: emitted.append(dict(event)),
                        raising=False)
    monkeypatch.setattr(heartbeat, 'append_event',
                        lambda task, event: emitted.append(dict(event)),
                        raising=False)
    monkeypatch.setattr(exec_finalize, 'append_event',
                        lambda task, event: emitted.append(dict(event)),
                        raising=False)

    def _fake(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
              cfg, project_path, project_enabled, all_tools=None):
        if fn_name == 'explode_tool':
            raise RuntimeError('boom')
        from lib.tasks_pkg.executor._finalize import _finalize_tool_round
        _finalize_tool_round(
            task, rn, round_entry,
            [{'toolName': fn_name, 'title': fn_name,
              'snippet': 'ok', 'source': 'Test',
              'fetched': True, 'fetchedChars': 2}])
        return tc_id, 'ok', False

    monkeypatch.setattr(heartbeat, '_execute_tool_one', _fake, raising=False)
    monkeypatch.setattr(pipeline, '_execute_tool_one', _fake, raising=False)

    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    execute_tool_pipeline(
        _mk_task(), [_mk_tc('c1', 'read_files', 0), _mk_tc('c2', 'explode_tool', 1)],
        cfg={'autoApply': True}, project_path=None, project_enabled=False,
        tool_list=[], messages=[], all_search_results_text=[], round_num=0,
        model='test-model')
    return emitted


def test_real_pipeline_emission_satisfies_invariants(pipeline_run):
    assert pipeline_run, 'pipeline emitted nothing — harness drifted'
    assert _check(pipeline_run) == ()


# ═══════════════════════════════════════════════════════════════════
#  Runtime audit hook (opt-in TOFU_EVENT_INVARIANTS=1)
# ═══════════════════════════════════════════════════════════════════

def test_maybe_check_task_log_noop_without_env(monkeypatch, caplog):
    import logging
    from lib.tasks_pkg.event_invariants import maybe_check_task_log
    monkeypatch.delenv('TOFU_EVENT_INVARIANTS', raising=False)
    with caplog.at_level(logging.ERROR):
        maybe_check_task_log({'id': f'inv-noop-{uuid.uuid4().hex[:8]}'})
    assert not [r for r in caplog.records
                if '[event-invariants]' in r.message]


def test_maybe_check_task_log_logs_violations(monkeypatch, caplog):
    import logging
    from lib.tasks_pkg.event_invariants import maybe_check_task_log
    from lib.tasks_pkg.event_log import append_persistent_event, flush_pending
    monkeypatch.setenv('TOFU_EVENT_INVARIANTS', '1')
    task_id = f'inv-audit-{uuid.uuid4().hex[:8]}'
    # Seed a settled-turn log that violates request-evidence: a model request
    # with no kind='request' snapshot.
    append_persistent_event(task_id, 0, {
        'type': 'model_request_start', 'spanId': 's1', 'roundNum': 1})
    append_persistent_event(task_id, 1, {
        'type': 'model_request_complete', 'spanId': 's1'})
    flush_pending(task_id)
    with caplog.at_level(logging.ERROR):
        maybe_check_task_log({'id': task_id})
    hits = [r for r in caplog.records
            if '[event-invariants]' in r.message
            and 'request-evidence' in r.message]
    assert hits, 'opt-in audit must log the seeded violation at ERROR'
