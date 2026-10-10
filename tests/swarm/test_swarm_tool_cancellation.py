"""Regression coverage for Swarm tools that outlive their LLM round."""

from __future__ import annotations

import threading
import time

import pytest

from lib.swarm.agent import SubAgent
from lib.swarm.liveness import ProgressBeacon
from lib.swarm.master import MasterOrchestrator
from lib.swarm.types import SubTaskSpec


pytestmark = pytest.mark.unit
















def test_unattended_code_exec_does_not_install_stdin_waiter(monkeypatch):
    from lib.tasks_pkg.handlers import code_exec

    captured = {}

    def fake_run(name, args, **kwargs):
        captured.update(kwargs)
        return '$ echo ok\nok\n[exit code: 0]'

    monkeypatch.setattr('lib.project_mod.execute_standalone_command', fake_run)
    monkeypatch.setattr(code_exec, '_finalize_tool_round', lambda *a, **k: None)
    task = {'id': 'parent', '_unattended': True, '_suppressEvents': True}

    code_exec._handle_code_exec(
        task, {}, 'run_command', 'tc1', {'command': 'echo ok'}, 1,
        {'toolName': 'run_command'}, {}, '', False)

    assert captured['stdin_callback'] is None


def test_stall_verdict_is_propagated_before_scheduler_shutdown(monkeypatch):
    spec = SubTaskSpec(id='stuck', role='browser', objective='x')
    master = MasterOrchestrator('task', 'conv', [spec], user_id=1)
    master._persist_agent_snapshot = lambda **kwargs: None
    master._beacon = ProgressBeacon(stall_timeout=0.01)
    master._beacon.touch('stuck', 'tool_start')
    time.sleep(0.02)
    seen = {}

    class FakeScheduler:
        running_count = 1

        def iter_completions(self):
            return iter(())

        def shutdown(self):
            seen['stop_at_shutdown'] = master._stop_stalled

    master._scheduler = FakeScheduler()
    monkeypatch.setattr('lib.swarm.persistence.mark_session_terminated',
                        lambda key: None)

    master._start_driver()

    assert master._completion_event.wait(2)
    assert seen['stop_at_shutdown'] is True
    assert master._stop_stalled is True
    assert master._aborted is False
