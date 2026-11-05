#!/usr/bin/env python3
"""A terminal event must retire the task's phase snapshot (epic
pt_f222e9ed288a44b3 — the multi-hour stale "compressing context…" HUD).

WHY
---
``append_event`` (lib/tasks_pkg/manager/_events.py) tracks ``task['phase']``
for reconnect projection and clears it on ``delta`` events. A task that ends
while its last phase is still up — killed mid-compaction-summary or errored
after a retrying phase — would keep serving that live-looking phase to
cold replay indefinitely (measured 2026-08-01: the 20:10
"compacting" phase was still visible on a bubble at 22:22, with NO compaction
having run for two hours — DB-verified). Two holes, one fix:

  1. ``done`` / ``error`` / ``aborted`` never cleared the snapshot. A finished
     task has no "current phase" — cold projection must see None.
  2. ``compaction_done`` — the compacting phase's OWN terminal — did not fold
     the phase either, so even on the happy path the HUD outlived the
     compaction until the next round happened to emit a phase.

Failing-first: test_done_clears_phase, test_compaction_done_folds_compacting.

Run DIRECTLY (env-guarded):
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python tests/tasks_pkg/test_phase_terminal_clear.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402

pytestmark = pytest.mark.unit


def _mk_task():
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    return chat_task_runtime.create(user_id=1)


@pytest.fixture()
def _no_persist(monkeypatch):
    """Keep the durable event-log write out of unit tests (DB hygiene). The
    persist closure imports append_persistent_event lazily at call time, so
    patching the module attribute is enough."""
    import lib.tasks_pkg.event_log as el
    monkeypatch.setattr(el, 'append_persistent_event',
                        lambda *a, **kw: None, raising=False)
    yield


def _phase(task, name, **extra):
    from lib.tasks_pkg.manager import append_event
    ev = {'type': 'phase', 'phase': name,
          'detail': extra.pop('detail', f'{name}…')}
    ev.update(extra)
    append_event(task, ev)


def test_phase_set_then_delta_clears(_no_persist):
    """Guard (pre-existing behaviour, must not regress): a phase event sets
    task['phase']; a delta clears it."""
    from lib.tasks_pkg.manager import append_event
    task = _mk_task()
    _phase(task, 'compacting', detailKey='stream.phase.compactingWindow')
    assert task['phase'] and task['phase']['phase'] == 'compacting'
    assert task['phase']['detailKey'] == 'stream.phase.compactingWindow'
    append_event(task, {'type': 'delta', 'content': 'x'})
    assert task['phase'] is None


def test_done_clears_phase(_no_persist):
    """FAILING-FIRST: a terminal done must retire the phase snapshot — a
    finished task has no current phase for reconnect projection."""
    from lib.tasks_pkg.manager import append_event
    task = _mk_task()
    _phase(task, 'compacting', detailKey='stream.phase.compactingWindow')
    append_event(task, {'type': 'done'})
    assert task['phase'] is None, (
        f'done must clear the phase snapshot, got {task["phase"]!r} — this '
        f'stale snapshot would make reconnect serve a live HUD for hours')


def test_error_and_aborted_clear_phase(_no_persist):
    from lib.tasks_pkg.manager import append_event
    for terminal in ('error', 'aborted'):
        task = _mk_task()
        _phase(task, 'retrying', detail='Retrying…')
        append_event(task, {'type': terminal})
        assert task['phase'] is None, f'{terminal} must clear the snapshot'


def _compaction_done_ev(**overrides):
    ev = {'type': 'compaction_done', 'archiveId': '1', 'convId': 'c',
          'trigger': 'force', 'tokensBefore': 2000, 'tokensAfter': 100,
          'tokenCountKind': 'estimated', 'msgsBefore': 10, 'msgsAfter': 3,
          'reductionPct': 95.0, 'roundNum': 1, 'receipt': {}}
    ev.update(overrides)
    return ev


def _compaction_start_ev(**overrides):
    ev = {'type': 'compaction', 'archiveId': '1', 'convId': 'c',
          'trigger': 'force', 'roundNum': 1, 'tokensBefore': 2198193,
          'tokensAfter': 0, 'tokenCountKind': 'estimated', 'msgsBefore': 10,
          'msgsAfter': 0, 'model': 'm', 'reason': 'test',
          'snapshotKind': 'pre_compaction_transcript', 'ts': 1}
    ev.update(overrides)
    return ev


def test_compaction_done_folds_compacting(_no_persist):
    """FAILING-FIRST: compaction_done is the compacting phase's OWN terminal —
    it must fold the phase immediately, not leave it up until the next round
    (or forever, if the task dies in between)."""
    from lib.tasks_pkg.manager import append_event
    task = _mk_task()
    _phase(task, 'compacting', detailKey='stream.phase.compactingWindow')
    append_event(task, _compaction_done_ev())
    assert task['phase'] is None, (
        f'compaction_done must fold the compacting phase, got {task["phase"]!r}')


def test_compaction_done_keeps_unrelated_phase(_no_persist):
    """Guard: a compaction_done landing while the turn is in a DIFFERENT phase
    must not clobber it (replay/interleave safety)."""
    from lib.tasks_pkg.manager import append_event
    task = _mk_task()
    _phase(task, 'tool_exec', detail='run_command')
    append_event(task, _compaction_done_ev())
    assert task['phase'] and task['phase']['phase'] == 'tool_exec'


def test_compaction_done_resumes_the_phase_it_interrupted(_no_persist):
    """Compaction runs inside the round being prepared; its own terminal
    resumes that phase. Folding to None blanked the status row until the next
    phase frame (a 16,498-frame capture showed 26 post-compaction gaps of
    0.2–0.9 s reading 'Waiting for an execution status update…')."""
    from lib.tasks_pkg.manager import append_event
    task = _mk_task()
    _phase(task, 'llm_thinking', detailKey='stream.phase.analyzingRound',
           detailArgs={'round': 7})
    interrupted = dict(task['phase'])
    _phase(task, 'compacting', detailKey='stream.phase.compactingWindow')
    # A repeated compacting beat must not replace the interrupted activity.
    _phase(task, 'compacting', detailKey='stream.phase.compactingWindow')
    append_event(task, _compaction_start_ev())
    append_event(task, _compaction_done_ev())
    resumed = task['phase']
    assert resumed['phase'] == 'llm_thinking'
    assert resumed['detailKey'] == 'stream.phase.analyzingRound'
    assert resumed['detailArgs'] == {'round': 7}
    assert resumed['emittedAt'] >= interrupted['emittedAt']
    if 'seq' in interrupted:
        assert resumed.get('seq') != interrupted['seq']


def test_compaction_resumes_only_the_latest_activity(_no_persist):
    """A phase published during compaction becomes the activity a later
    compaction resumes; a delta in between leaves nothing to resume."""
    from lib.tasks_pkg.manager import append_event
    task = _mk_task()
    _phase(task, 'llm_thinking', detailKey='stream.phase.analyzingRound')
    _phase(task, 'compacting', detailKey='stream.phase.compactingWindow')
    _phase(task, 'retrying', detail='Retrying…')
    _phase(task, 'compacting', detailKey='stream.phase.compactingWindow')
    append_event(task, _compaction_done_ev())
    assert task['phase']['phase'] == 'retrying'
    _phase(task, 'compacting', detailKey='stream.phase.compactingWindow')
    append_event(task, {'type': 'delta', 'content': 'x'})
    append_event(task, _compaction_done_ev())
    assert task['phase'] is None


def test_tool_start_retires_only_its_own_preparing_phase(_no_persist):
    """The preparing-tool-call phase ends when that call is announced; another
    call's announcement (the previous call completing as this one opens) keeps
    it."""
    from lib.agent_core.events import EventType, build_event
    from lib.tasks_pkg.manager import append_event
    task = _mk_task()
    _phase(task, 'llm_thinking', detailKey='stream.phase.preparingToolCall',
           detailArgs={'model': 'm', 'tool': 'write_file', 'elapsed': 0},
           toolCallId='call_2')
    assert task['phase']['toolCallId'] == 'call_2'
    append_event(task, build_event(EventType.TOOL_START, roundNum=1, toolCallId='call_1',
                                   toolName='read_files', query='a.py'))
    assert task['phase']['toolCallId'] == 'call_2'
    append_event(task, build_event(EventType.TOOL_START, roundNum=1, toolCallId='call_2',
                                   toolName='write_file', query='b.py'))
    assert task['phase'] is None


def test_compaction_start_keeps_phase(_no_persist):
    """Guard: the compaction START event (archive row inserted, summary still
    running) must NOT fold the phase — the HUD is honestly in progress."""
    from lib.tasks_pkg.manager import append_event
    task = _mk_task()
    _phase(task, 'compacting', detailKey='stream.phase.compactingWindow')
    append_event(task, _compaction_start_ev())
    assert task['phase'] and task['phase']['phase'] == 'compacting'


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
