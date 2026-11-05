"""Trace comparison must validate full evidence and protect supplied event rows."""
from types import SimpleNamespace

import pytest

from benchmarks.turn_trace_compare import verify

pytestmark = pytest.mark.unit


def projection(*, changed=False, mutate=False):
    module = SimpleNamespace(
        _read_trace_rows=lambda _: [],
        compact_trace_document=lambda document, **kwargs: {'spans': []},
    )

    def fold(task_id, *, now_ms, ledger_out):
        if mutate:
            module._read_trace_rows(task_id).append({'type': 'unexpected'})
        return module.compact_trace_document({'spans': [{'status': 'changed' if changed else 'done'}]})

    module._fold_task_trace = fold
    return module


def test_trace_measure_rejects_drift_hidden_by_detail_compaction():
    with pytest.raises(ValueError, match='Trace output drift'):
        verify(projection(), projection(changed=True), [])


def test_trace_measure_rejects_authority_input_mutation():
    with pytest.raises(ValueError, match='mutated authority input'):
        verify(projection(), projection(mutate=True), [])
