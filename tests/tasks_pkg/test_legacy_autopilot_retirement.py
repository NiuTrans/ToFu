"""Current task admission never enters the standalone virtual-user interpreter."""
import ast
from pathlib import Path
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.unit


def test_retired_carrier_settles_without_running_model(monkeypatch):
    from lib.tasks_pkg.orchestrator._task_open import check_autopilot_kick
    persist = Mock()
    emit = Mock()
    monkeypatch.setattr('lib.tasks_pkg.manager.persist_task_result', persist)
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event', emit)
    task = {'id': 'retired', '_autopilot_kick': True}
    assert check_autopilot_kick(task) is True
    assert task['status'] == 'error'
    assert task['_abort_reason'] == 'legacy_autopilot_retired'
    persist.assert_called_once_with(task)
    assert emit.call_args.args[1]['type'] == 'done'
    assert check_autopilot_kick({'id': 'ordinary'}) is False


def test_live_execution_has_no_legacy_interpreter_imports():
    root = Path(__file__).resolve().parents[2]
    for relative in ('lib/tasks_pkg/orchestrator', 'lib/tasks_pkg/handlers',
                     'lib/tasks_pkg/tool_dispatch', 'lib/orchestration',
                     'lib/tasks_pkg/compaction'):
        for path in (root / relative).rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom):
                    assert not (node.module or '').startswith('lib.tasks_pkg.autopilot'), path
