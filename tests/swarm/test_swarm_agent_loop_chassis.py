"""Swarm and Goal schedule the canonical invocation, without private runtimes.

Behavior coverage lives in test_swarm_invocation and the root/Goal integration
suite. This structural guard prevents reconstructing a second preparation,
transport, or tool-execution path behind an otherwise shared loop chassis.
"""
import ast
from pathlib import Path
import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = frozenset({'run_agent_loop', 'build_body', 'dispatch_stream',
    'stream_llm_response', '_execute_tool_one', 'execute_tool_pipeline'})


def private_execution_calls(source):
    violations = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            violations.extend(alias.name for alias in node.names if alias.name in FORBIDDEN)
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, 'attr', '')
            if name in FORBIDDEN:
                violations.append(name)
    return violations


def test_swarm_cannot_own_request_transport_or_tool_execution():
    violations = {str(path.relative_to(ROOT)): private_execution_calls(path.read_text())
                  for path in (ROOT / 'lib/swarm').rglob('*.py')}
    assert not {path: values for path, values in violations.items() if values}
    assert not private_execution_calls((ROOT / 'lib/orchestration/agent_runner.py').read_text())


@pytest.mark.parametrize('name', sorted(FORBIDDEN))
def test_private_execution_guard_rejects_reintroduced_paths(name):
    assert private_execution_calls(f'from fake import {name}\n{name}()')
    assert private_execution_calls(f'other.{name}()')


def test_schedulers_enter_one_child_factory_and_executor():
    for relative in ('lib/swarm/agent.py', 'lib/orchestration/agent_runner.py'):
        tree = ast.parse((ROOT / relative).read_text())
        names = [node.func.id for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
        assert names.count('create_child_invocation') == 1, relative
        # Registered and transient admission use mutually exclusive branches
        # of the same executor and the same child from the one factory above.
        executions = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                      and isinstance(node.func, ast.Name)
                      and node.func.id == 'run_child_invocation']
        assert executions, relative
        assert all(isinstance(call.args[0], ast.Name) and call.args[0].id == 'child'
                   for call in executions), relative
