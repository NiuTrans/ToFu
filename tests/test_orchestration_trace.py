"""Direct contracts for the orchestration per-node trace recorder."""

import threading
from pathlib import Path

import pytest

from lib.orchestration_engine import (
    _TRACE_INPUT_CHARS,
    _TRACE_OUTPUT_CHARS,
)
from lib.orchestration_trace import (
    TRACE_ACTIVITY_FIELDS,
    TRACE_CONTRACT_FORMAT,
    TRACE_ERROR_CHARS,
    TRACE_HISTORY_ENTRIES,
    TRACE_INPUT_CHARS,
    TRACE_OUTPUT_CHARS,
    TRACE_STATUS_MAP,
    OrchestrationTraceRecorder,
    trace_activity_snapshot,
    trace_contract,
)

pytestmark = pytest.mark.unit


def _capture(recorder: OrchestrationTraceRecorder, node_id: str, **overrides):
    values = {
        'iteration': 2,
        'brief': 'resolved brief',
        'input_context': 'input context',
        'output': 'output text',
        'status': 'completed',
        'error': '',
        'elapsed': 1.234,
        'emits': 'assistant',
        'isolation': 'fresh',
        'state_changing': 1,
        'exploratory': 2,
        'state_changing_tools': ['write_file'],
        'thinking': 'reasoning',
    }
    values.update(overrides)
    recorder.capture(
        {'id': node_id, 'type': 'role', 'role': 'worker', 'name': 'Worker'},
        **values,
    )


def test_engine_trace_limits_remain_compatibility_aliases():
    assert _TRACE_INPUT_CHARS == TRACE_INPUT_CHARS
    assert _TRACE_OUTPUT_CHARS == TRACE_OUTPUT_CHARS


def test_capture_bounds_fields_and_emits_self_contained_event():
    events = []
    recorder = OrchestrationTraceRecorder(
        emit=events.append,
        input_chars=5,
        output_chars=7,
        error_chars=4,
    )
    _capture(
        recorder,
        'worker',
        brief='123456',
        input_context='abcdefgh',
        output='ABCDEFGHI',
        thinking='thinking-long',
        error='broken',
        subflow=True,
    )

    entry = recorder.snapshot()[0]
    assert entry['seq'] == 1
    assert entry['brief'] == '12345'
    assert entry['brief_truncated'] is True
    assert entry['input'] == 'abcde'
    assert entry['input_truncated'] is True
    assert entry['output'] == 'ABCDEFG'
    assert entry['output_truncated'] is True
    assert entry['thinking'] == 'thinkin'
    assert entry['thinking_truncated'] is True
    assert entry['error'] == 'brok'
    assert entry['error_truncated'] is True
    assert entry['elapsed'] == 1.23
    assert entry['subflow'] is True
    assert entry['state_changing_tools'] == ['write_file']
    assert events == [{'type': 'step_trace', **entry}]


def test_trace_contract_centralizes_text_limits_and_flag_names():
    contract = trace_contract()

    assert contract == {
        'format': TRACE_CONTRACT_FORMAT,
        'historyLimit': TRACE_HISTORY_ENTRIES,
        'statusMap': TRACE_STATUS_MAP,
        'activityFields': TRACE_ACTIVITY_FIELDS,
        'textLimits': {
            'brief': TRACE_INPUT_CHARS,
            'input': TRACE_INPUT_CHARS,
            'output': TRACE_OUTPUT_CHARS,
            'thinking': TRACE_OUTPUT_CHARS,
            'error': TRACE_ERROR_CHARS,
        },
        'truncationFlags': {
            field: f'{field}_truncated'
            for field in ('brief', 'input', 'output', 'thinking', 'error')
        },
    }


def test_activity_snapshot_normalizes_counts_tools_and_wire_names():
    assert trace_activity_snapshot(
        state_changing='2',
        exploratory=-1,
        state_changing_tools=[' write_file ', '', None, 'apply_patch'],
    ) == {
        'state_changing': 2,
        'exploratory': 0,
        'state_changing_tools': ['write_file', 'apply_patch'],
    }
    assert trace_activity_snapshot(
        state_changing='invalid',
        exploratory=float('inf'),
        state_changing_tools='write_file',
    ) == {
        'state_changing': 0,
        'exploratory': 0,
        'state_changing_tools': [],
    }


def test_sequence_is_unique_and_monotonic_under_concurrent_capture():
    events = []
    recorder = OrchestrationTraceRecorder(emit=events.append)
    threads = [
        threading.Thread(target=_capture, args=(recorder, f'node-{index}'))
        for index in range(20)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    entries = recorder.snapshot()
    assert [entry['seq'] for entry in entries] == list(range(1, 21))
    assert sorted(event['seq'] for event in events) == list(range(1, 21))


def test_sink_failure_never_drops_captured_trace_or_escapes():
    def failing_sink(_event):
        raise RuntimeError('sink unavailable')

    recorder = OrchestrationTraceRecorder(emit=failing_sink)
    _capture(recorder, 'worker')
    assert recorder.snapshot()[0]['node_id'] == 'worker'


def test_snapshot_outer_container_is_detached():
    recorder = OrchestrationTraceRecorder(emit=lambda _event: None)
    _capture(recorder, 'worker')
    snapshot = recorder.snapshot()
    snapshot.clear()
    assert len(recorder.snapshot()) == 1


def test_engine_delegates_trace_storage_without_shadow_implementation():
    root = Path(__file__).resolve().parents[1]
    engine = (root / 'lib' / 'orchestration_engine.py').read_text()
    role_runtime = (
        root / 'lib' / 'orchestration_role_runtime.py'
    ).read_text()
    subflow_runtime = (
        root / 'lib' / 'orchestration_subflow_runtime.py'
    ).read_text()
    trace = (root / 'lib' / 'orchestration_trace.py').read_text()

    assert 'OrchestrationTraceRecorder(' in engine
    assert 'self._trace_recorder.capture(' in role_runtime
    assert 'self._trace_recorder.capture(' in subflow_runtime
    assert 'self._trace_recorder.snapshot()' in engine
    assert 'self._trace:' not in engine
    assert 'def _trace_node(' not in engine
    assert 'class OrchestrationTraceRecorder' in trace
    assert 'FlowExecutor' not in trace
    assert engine.count('\n') < 1420
