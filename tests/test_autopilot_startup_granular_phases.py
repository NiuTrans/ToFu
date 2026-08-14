#!/usr/bin/env python3
"""Regression coverage for localized orchestration startup phases.

Large conversations can spend many seconds in pre-LLM preparation.  The
orchestrator therefore emits one canonical ``working`` phase at each real
boundary: configuration, tools, history, and context.  The same events serve
ordinary tasks and are transformed into the synthetic-user bubble for VU
subtasks.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
import time

import pytest

from lib.agent_core.events import Phase


pytestmark = pytest.mark.unit


_STARTUP_PHASES = [
    ('config', 'Resolving model and workspace settings…',
     'stream.phase.startupConfig'),
    ('tools', 'Preparing tools and workspace…',
     'stream.phase.startupTools'),
    ('history', 'Restoring conversation and tool history…',
     'stream.phase.startupHistory'),
    ('context', 'Loading project context and relevant memory…',
     'stream.phase.startupContext'),
]


@pytest.fixture
def emitted(monkeypatch):
    """Capture events at the typed phase seam used by the real helper."""
    import lib.tasks_pkg.orchestrator._run as run

    frames = []

    def _capture(task, phase, **fields):
        frames.append((task.get('id'), phase, fields))

    monkeypatch.setattr(run, 'emit_phase', _capture)
    return frames


def test_vu_subtask_startup_emits_localized_working_phase(emitted):
    from lib.tasks_pkg.orchestrator._run import _emit_startup_phase

    _emit_startup_phase({'id': 'vu-task', '_vu_subtask': True}, 'config')

    assert emitted == [
        ('vu-task', Phase.WORKING, {
            'detail': _STARTUP_PHASES[0][1],
            'detailKey': _STARTUP_PHASES[0][2],
        }),
    ]


def test_ordinary_worker_gets_the_same_startup_feedback(emitted):
    from lib.tasks_pkg.orchestrator._run import _emit_startup_phase

    for stage, _detail, _key in _STARTUP_PHASES:
        _emit_startup_phase({'id': 'worker-task'}, stage)

    assert [fields['detail'] for _tid, _phase, fields in emitted] == [
        detail for _stage, detail, _key in _STARTUP_PHASES
    ]
    assert [fields['detailKey'] for _tid, _phase, fields in emitted] == [
        key for _stage, _detail, key in _STARTUP_PHASES
    ]
    assert all(phase == Phase.WORKING for _tid, phase, _fields in emitted)


def test_startup_phase_emit_never_raises_into_the_run(monkeypatch):
    import lib.tasks_pkg.orchestrator._run as run

    def _boom(*args, **kwargs):
        raise RuntimeError('push channel down')

    monkeypatch.setattr(run, 'emit_phase', _boom)
    run._emit_startup_phase({'id': 'worker-task'}, 'tools')


def test_all_four_startup_steps_are_wired_in_execution_order():
    import lib.tasks_pkg.orchestrator._run as run

    src = inspect.getsource(run.run_task)
    positions = [
        src.index(f"_emit_startup_phase(task, '{stage}')")
        for stage, _detail, _key in _STARTUP_PHASES
    ]
    assert positions == sorted(positions)
    assert len(set(positions)) == len(_STARTUP_PHASES)


def test_context_phase_precedes_the_slow_context_injection():
    import lib.tasks_pkg.orchestrator._run as run

    src = inspect.getsource(run.run_task)
    phase_at = src.index("_emit_startup_phase(task, 'context')")
    inject_at = src.index('inject_context_and_emit_chips(')
    assert phase_at < inject_at


def test_vu_context_injection_reports_real_boundaries(monkeypatch):
    """The two potentially slow context boundaries must reach the VU stream,
    in order, rather than leaving it on the previous generic phase."""
    import lib.tasks_pkg.orchestrator._context_inject as context_inject

    trace = []

    def _fake_inject(*args, **kwargs):
        trace.append('inject')

    class _Prefetch:
        def shutdown(self, wait=False):
            trace.append(('shutdown', wait))

    def _capture_phase(detail, **fields):
        trace.append(('phase', detail, fields))

    monkeypatch.setattr(context_inject, '_inject_system_contexts', _fake_inject)
    task = {'id': 'vu-context', 'convId': 'c1'}
    context_inject.inject_context_and_emit_chips(
        task=task,
        messages=[],
        cfg={},
        project_path=None,
        project_enabled=False,
        memory_enabled=False,
        search_enabled=False,
        swarm_enabled=False,
        has_real_tools=False,
        model='test-model',
        tool_list=[],
        prefetch_executor=_Prefetch(),
        tid='vu-conte',
        t_run_start=time.time(),
        vu_phase=_capture_phase,
    )

    phases = [item for item in trace if isinstance(item, tuple)
              and item[0] == 'phase']
    assert [item[2]['detail_key'] for item in phases] == [
        'stream.phase.vuInjectContext',
        'stream.phase.vuContextReady',
    ]
    assert trace.index(phases[0]) < trace.index('inject') < trace.index(phases[1])


def test_run_task_wires_vu_context_phase_adapter():
    import lib.tasks_pkg.orchestrator._run as run

    src = inspect.getsource(run.run_task)
    assert '_vu_phase = make_vu_phase(task)' in src
    assert 'vu_phase=_vu_phase' in src


def test_vu_round_zero_names_reply_composition(monkeypatch):
    import lib.tasks_pkg.orchestrator._finalize as finalize

    emitted = []
    monkeypatch.setattr(finalize, 'append_event',
                        lambda task, event: emitted.append(event))
    finalize._emit_tool_round_phase(
        {'id': 'vu-round', '_vu_subtask': True}, {}, 0)

    assert emitted == [{
        'type': 'phase',
        'phase': 'llm_thinking',
        'detail': 'Autopilot is composing your reply…',
        'detailKey': 'autopilot.composing',
        'roundNum': 1,
    }]


def test_vu_phase_keys_exist_in_both_locales():
    root = Path(__file__).resolve().parents[1]
    keys = {
        'stream.phase.vuVerifyAssistant',
        'stream.phase.vuAssembleContext',
        'stream.phase.vuInjectContext',
        'stream.phase.vuContextReady',
        # Reuse the established Autopilot composing key rather than adding a
        # duplicate stream-phase translation for the same user-visible state.
        'autopilot.composing',
    }
    locale_dir = root / 'frontend' / 'src' / 'i18n' / 'locales'
    if locale_dir.is_dir():
        # Vite-native source after the i18n migration.
        for locale in ('en', 'zh'):
            path = locale_dir / f'{locale}.json'
            data = json.loads(path.read_text(encoding='utf-8'))
            assert keys <= data.keys(), (
                f'{locale} is missing {sorted(keys - data.keys())}')
        return

    # Compatibility while the locale split is landing on shared HEAD: the
    # classic source stores both languages under each key.
    legacy = (root / 'static' / 'js' / 'i18n.js').read_text(encoding='utf-8')
    for key in keys:
        assert f"'{key}'" in legacy, f'legacy i18n is missing {key}'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
