"""Direct contracts for orchestration producer progress accounting."""

import threading
from pathlib import Path

import pytest

from lib.orchestration_progress import (
    REPLAN_SUMMARY_CHARS,
    OrchestrationProgressLedger,
)

pytestmark = pytest.mark.unit


def _snapshot(
    node_id: str,
    *,
    state_changing: int = 0,
    exploratory: int = 0,
    names=None,
    reported: bool = True,
) -> dict:
    return {
        'node_id': node_id,
        'role': 'worker',
        'sc_count': state_changing,
        'explore_count': exploratory,
        'names': list(names or []),
        'reported': reported,
    }


def test_record_updates_latest_and_iteration_with_detached_views():
    ledger = OrchestrationProgressLedger()
    source = _snapshot('worker', state_changing=1, names=['write_file'])
    ledger.record_producer(source)
    source['sc_count'] = 99

    assert ledger.latest_snapshot()['sc_count'] == 1
    rows = ledger.iteration_snapshot()
    assert rows == [_snapshot('worker', state_changing=1, names=['write_file'])]
    rows[0]['sc_count'] = 42
    assert ledger.iteration_snapshot()[0]['sc_count'] == 1


def test_parallel_aggregate_sums_work_and_preserves_branch_order():
    ledger = OrchestrationProgressLedger()
    ledger.replace_iteration([
        _snapshot('left', exploratory=2, names=[], reported=True),
        _snapshot(
            'right',
            state_changing=3,
            names=['write_file', 'apply_diff', 'write_file'],
            reported=True,
        ),
    ])

    assert ledger.aggregate_iteration() == {
        'node_id': 'left,right',
        'role': 'parallel',
        'sc_count': 3,
        'explore_count': 2,
        'names': ['write_file', 'apply_diff', 'write_file'],
        'reported': True,
    }


def test_reset_iteration_keeps_latest_for_outside_loop_verifier():
    ledger = OrchestrationProgressLedger()
    latest = _snapshot('worker', state_changing=1, names=['apply_diff'])
    ledger.record_producer(latest)
    ledger.reset_iteration()

    assert ledger.aggregate_iteration() == {}
    assert ledger.latest_snapshot() == latest
    rendered = ledger.append_deliverables_snapshot('CTX', in_loop=False)
    assert '1 state-changing' in rendered
    assert 'apply_diff' in rendered


def test_loop_verifier_uses_aggregate_instead_of_racy_latest_slot():
    ledger = OrchestrationProgressLedger()
    ledger.replace_iteration([
        _snapshot('left', state_changing=2,
                  names=['write_file', 'apply_diff']),
        _snapshot('right', state_changing=1, exploratory=1,
                  names=['insert_content']),
    ])
    ledger.replace_latest(_snapshot(
        'right',
        state_changing=1,
        exploratory=1,
        names=['insert_content'],
    ))

    rendered = ledger.append_deliverables_snapshot('CTX', in_loop=True)
    assert rendered.startswith('CTX\n\n───── Deliverables Snapshot')
    assert '3 state-changing, 1 exploratory' in rendered
    for name in ('write_file', 'apply_diff', 'insert_content'):
        assert name in rendered


def test_unreported_tools_do_not_inject_false_zero_work_guidance():
    ledger = OrchestrationProgressLedger()
    ledger.record_producer(_snapshot('worker', reported=False))
    assert ledger.append_deliverables_snapshot('CTX', in_loop=True) == 'CTX'


def test_parallel_recording_is_lossless_under_concurrency():
    ledger = OrchestrationProgressLedger()
    threads = [
        threading.Thread(
            target=ledger.record_producer,
            args=(_snapshot(
                f'worker-{index}',
                state_changing=1,
                names=['write_file'],
            ),),
        )
        for index in range(30)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    aggregate = ledger.aggregate_iteration()
    assert aggregate['sc_count'] == 30
    assert len(aggregate['names']) == 30
    assert len(ledger.iteration_snapshot()) == 30


def test_replan_summary_excludes_verifiers_flattens_and_bounds_output():
    ledger = OrchestrationProgressLedger()
    transcript = [
        {
            'role': 'worker',
            'state_changing': 2,
            'output': 'changed api\nand tests',
        },
        {
            'role': 'critic',
            'state_changing': 0,
            'output': 'review feedback must not leak',
        },
        {
            'role': 'researcher',
            'state_changing': 0,
            'output': 'inspected current behavior',
        },
    ]

    summary = ledger.build_replan_summary(
        transcript,
        verifier_roles=frozenset({'critic'}),
        limit=80,
    )

    assert len(summary) <= 80
    assert 'review feedback must not leak' not in summary
    assert 'researcher: 0 state-changing calls' in summary
    assert '\nchanged api\n' not in summary
    assert REPLAN_SUMMARY_CHARS == 2000


def test_engine_keeps_compatibility_proxies_without_ledger_logic():
    root = Path(__file__).resolve().parents[1]
    engine = (root / 'lib' / 'orchestration_engine.py').read_text()
    runtime = (root / 'lib' / 'orchestration_role_runtime.py').read_text()
    loop_runtime = (root / 'lib' / 'orchestration_loop_runtime.py').read_text()
    replan_runtime = (
        root / 'lib' / 'orchestration_replan_runtime.py').read_text()
    progress = (root / 'lib' / 'orchestration_progress.py').read_text()

    assert 'OrchestrationProgressLedger(lock=self._lock)' in engine
    assert 'return self._progress.aggregate_iteration()' in engine
    assert 'self._progress.record_producer({' in runtime
    assert 'self._progress.reset_iteration()' in loop_runtime
    assert 'self._progress.build_replan_summary(' in replan_runtime
    assert 'names.extend(producer.get' not in engine
    assert 'Deliverables Snapshot (engine-injected)' not in engine
    assert "preview = (e.get('output')" not in engine
    assert 'class OrchestrationProgressLedger' in progress
    assert 'FlowExecutor' not in progress
    assert engine.count('\n') < 1310
