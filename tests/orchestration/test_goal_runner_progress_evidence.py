"""Actual invocation runner must retain tool result evidence, not just names."""

import pytest

from lib.orchestration.agent_runner import (
    OrchestrationAgentRunnerConfig, OrchestrationInvocationRunner,
)
from lib.tools.meta import build_project_tool_meta
from tests.orchestration.test_orchestration_agent_runner import _parent

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('name,args,content,expected', [
    ('run_command', {'command': 'git status'}, '$ git status\n[exit code: 0]', 0),
    ('run_command', {'command': 'false'}, '$ false\n[exit code: 1]', 0),
    ('run_command', {'command': 'true'}, '$ true\n[exit code: 0]', 0),
    ('write_file', {'path': 'new.txt', 'content': 'new'}, 'File created: new.txt', 1),
    ('apply_diff', {'path': 'a', 'search': 'old', 'replace': 'new'}, 'Applied diff to a', 1),
    ('apply_diff', {'path': 'a', 'search': 'old', 'replace': 'old'}, 'Applied diff to a', 0),
])
def test_invocation_runner_passes_settled_tool_receipts(monkeypatch, name, args, content, expected):
    import lib.tasks_pkg.child_invocation as child_module

    parent = _parent()

    # This seam tests tool receipts, not the legacy fixture's intentionally
    # malformed native model selection (rejected before invocation today).
    parent['config'].pop('modelRef', None)
    def execute(task, *, policy):
        assert policy.is_child
        task.update(status='done', finishReason='stop', content='worker done',
                    toolRounds=[{
                        'round': 1, 'toolName': name, 'toolArgs': args, 'status': 'done',
                        'toolContent': content,
                        'results': [build_project_tool_meta(name, args, content)],
                    }])

    monkeypatch.setattr(child_module, 'execute_invocation', execute)
    result = OrchestrationInvocationRunner(
        OrchestrationAgentRunnerConfig(
            parent_task=parent, model='chosen', model_routing_policy='selected'),
        emit=lambda _event: None, abort_check=lambda: False,
    )({'id': 'worker', 'type': 'role', 'role': 'worker', 'params': {}}, 'objective', 1)
    assert result.status == 'completed'
    assert result.tool_usage.reported is True
    assert result.tool_usage.state_changing_count == expected
    assert result.tool_usage.exploratory_count == 1 - expected
