"""Direct contracts for orchestration shared feedback state."""

from pathlib import Path

import pytest

import lib.orchestration_feedback as feedback_module
from lib.orchestration_feedback import OrchestrationFeedbackState

pytestmark = pytest.mark.unit


def test_shared_context_is_bounded_and_channel_advances_as_one_unit():
    state = OrchestrationFeedbackState(attempt_chars=5, feedback_chars=4)
    state.complete_role('worker', 'attempt123', shared=True, verifier=False)
    state.complete_role('critic', 'feedback123', shared=False, verifier=True)
    state.set_directive('ship a concrete edit')

    context = state.compose_shared_context('worker', 'UPSTREAM')
    assert context == (
        'UPSTREAM\n\n## Your previous attempt\npt123'
        '\n\n## Reviewer feedback to address\nk123'
        '\n\n## ⚠️ Directive\nship a concrete edit'
    )

    state.complete_role('worker', 'next-attempt', shared=True, verifier=False)
    assert state.pending_feedback() == ''
    assert state.pending_directive() == ''
    assert state.node_memory_snapshot() == {'worker': 'next-attempt'}


def test_loop_reset_keeps_node_memory_but_clears_loop_local_state():
    state = OrchestrationFeedbackState()
    state.complete_role('worker', 'attempt', shared=True, verifier=False)
    state.complete_role('critic', 'review', shared=False, verifier=True)
    state.set_directive('act')
    state.append_verifier_feedback('review')
    state.record_virtual_user_progress(
        '[PROGRESS: resolved=1 remaining=2]',
        {'names': ['write_file']},
    )

    state.reset_loop()

    assert state.node_memory_snapshot() == {'worker': 'attempt'}
    assert state.pending_feedback() == ''
    assert state.pending_directive() == ''
    assert state.history_snapshot() == []
    assert state.vu_progress_snapshot() == []


def test_vu_progress_uses_cumulative_delta_and_deduplicated_targets():
    state = OrchestrationFeedbackState()
    first = state.record_virtual_user_progress(
        'turn one',
        {'names': ['write_file', 'apply_diff', 'write_file']},
        progress_parser=lambda _text: (2, 3),
    )
    second = state.record_virtual_user_progress(
        'turn two',
        {'names': ['write_file']},
        progress_parser=lambda _text: (2, 3),
    )
    missing = state.record_virtual_user_progress(
        'unstructured',
        {'names': ['write_file']},
        progress_parser=lambda _text: (None, None),
    )

    assert first == {
        'resolved_delta': 2,
        'cum_resolved': 2,
        'targets': ['apply_diff', 'write_file'],
    }
    assert second['resolved_delta'] == 0
    assert missing['resolved_delta'] is None
    assert missing['cum_resolved'] == 2


def test_feedback_repetition_window_depends_on_verifier_role():
    repeated = 'please fix the same concrete unresolved issue again'
    state = OrchestrationFeedbackState()
    state.append_verifier_feedback(repeated)
    state.append_verifier_feedback(repeated)

    assert state.detects_stuck(verifier_role='critic') is True
    assert state.detects_stuck(verifier_role='virtual_user') is False

    state.append_verifier_feedback(repeated)
    assert state.detects_stuck(verifier_role='virtual_user') is True


def test_no_progress_window_is_fail_open_and_uses_owned_ledger(monkeypatch):
    monkeypatch.setattr(
        feedback_module,
        'autopilot_progress_window',
        lambda: 2,
    )
    state = OrchestrationFeedbackState()
    for _index in range(2):
        state.record_virtual_user_progress(
            'stalled',
            {'names': ['write_file']},
            progress_parser=lambda _text: (0, 2),
        )
    assert state.no_progress_window() == 2

    state.record_virtual_user_progress(
        'missing signal',
        {'names': ['write_file']},
        progress_parser=lambda _text: (None, None),
    )
    assert state.no_progress_window() == 0


def test_snapshots_are_detached_and_replace_seams_are_explicit():
    state = OrchestrationFeedbackState()
    state.replace_node_memory({'worker': 'attempt'})
    state.replace_pending_feedback('review')
    state.replace_pending_directive('act')
    state.replace_history(['one', 'two'])
    state.replace_vu_progress([{'resolved_delta': 0, 'targets': ['x']}])

    memory = state.node_memory_snapshot()
    history = state.history_snapshot()
    progress = state.vu_progress_snapshot()
    memory.clear()
    history.clear()
    progress[0]['resolved_delta'] = 9

    assert state.node_memory_snapshot() == {'worker': 'attempt'}
    assert state.pending_feedback() == 'review'
    assert state.pending_directive() == 'act'
    assert state.history_snapshot() == ['one', 'two']
    assert state.vu_progress_snapshot()[0]['resolved_delta'] == 0


def test_engine_delegates_feedback_state_without_shadow_storage():
    root = Path(__file__).resolve().parents[1]
    engine = (root / 'lib' / 'orchestration_engine.py').read_text()
    runtime = (root / 'lib' / 'orchestration_role_runtime.py').read_text()
    loop_runtime = (root / 'lib' / 'orchestration_loop_runtime.py').read_text()
    feedback = (root / 'lib' / 'orchestration_feedback.py').read_text()

    assert 'OrchestrationFeedbackState(lock=self._lock)' in engine
    assert 'self._feedback.complete_role(' in runtime
    assert 'self._feedback.reset_loop()' in loop_runtime
    assert 'self._feedback.record_virtual_user_progress(' in loop_runtime
    assert 'self._feedback.no_progress_window()' in loop_runtime
    assert 'self._node_memory: dict' not in engine
    assert 'self._feedback_history.append(' not in engine
    assert 'self._vu_progress.append(' not in engine
    assert 'class OrchestrationFeedbackState' in feedback
    assert 'FlowExecutor' not in feedback
    assert engine.count('\n') < 1320
