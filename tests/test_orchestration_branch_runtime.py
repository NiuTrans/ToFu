"""Direct contracts for the focused orchestration branch router."""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.orchestration_branch_runtime import OrchestrationBranchRuntime


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


class _Navigator:
    labels = {'a': 'Alpha path', 'b': 'Beta path'}

    def node_label(self, node_id):
        return self.labels[node_id]


def _runtime(*, successors, classifier=None, params=None):
    events = []
    calls = []

    def run_classifier(node, context):
        calls.append((node, context))
        return classifier or ''

    runtime = OrchestrationBranchRuntime(
        navigator=_Navigator(),
        nodes={
            'branch': {
                'id': 'branch',
                'type': 'control',
                'kind': 'branch',
                'name': 'Route work',
                'params': params or {},
            },
        },
        successors=lambda _node_id: list(successors),
        run_classifier=run_classifier,
        emit=events.append,
    )
    return runtime, events, calls


def test_branch_runtime_uses_classifier_output_not_upstream_context():
    runtime, events, calls = _runtime(
        successors=['a', 'b'],
        classifier='Choose Beta path',
        params={'classifier': 'router', 'tier': 'standard'},
    )

    # Alpha appearing upstream must not beat the classifier's Beta answer.
    assert runtime.run('branch', 'Prior discussion chose Alpha path') == 'b'
    classifier_node, context = calls[0]
    assert context == 'Prior discussion chose Alpha path'
    assert classifier_node['role'] == 'router'
    assert classifier_node['params']['tier'] == 'standard'
    assert "'Alpha path', 'Beta path'" in classifier_node['params']['objective']
    assert events == [{
        'type': 'branch_pick',
        'node_id': 'branch',
        'chosen': 'b',
        'options': 2,
        'how': 'classifier',
    }]


def test_branch_runtime_keeps_deterministic_fallback_and_empty_projection():
    runtime, events, calls = _runtime(successors=['a', 'b'])
    assert runtime.run('branch', 'seed') == 'a'
    assert not calls
    assert events[0]['how'] == 'first-edge'

    empty, empty_events, empty_calls = _runtime(successors=[])
    assert empty.run('branch', 'seed') is None
    assert not empty_calls
    assert empty_events == [{
        'type': 'branch_pick',
        'node_id': 'branch',
        'chosen': None,
        'options': 0,
    }]


def test_engine_delegates_branch_policy_to_the_focused_runtime():
    engine = (ROOT / 'lib' / 'orchestration_engine.py').read_text()
    branch = (ROOT / 'lib' / 'orchestration_branch_runtime.py').read_text()
    role = (ROOT / 'lib' / 'orchestration_role_runtime.py').read_text()

    method = engine.split('    def _run_branch(', 1)[1].split(
        '\n    def ', 1)[0]
    assert 'return self._branch_runtime.run(bid, context)' in method
    assert 'classifier_role' not in engine
    assert "'type': 'branch_pick'" in branch
    assert 'def run_output(' in role
