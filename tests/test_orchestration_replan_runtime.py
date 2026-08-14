"""Direct contracts for the focused structural re-plan runtime."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from lib.orchestration_replan_runtime import OrchestrationReplanRuntime


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


class _Progress:
    def __init__(self, summary='completed file edits'):
        self.summary = summary
        self.calls = []

    def build_replan_summary(
        self, transcript, *, verifier_roles, limit,
    ):
        self.calls.append((transcript, frozenset(verifier_roles), limit))
        return self.summary


class _Transcript:
    def snapshot(self):
        return [{'role': 'worker', 'output': 'done'}]


def _runtime(*, summary='completed file edits'):
    nodes = {
        'planner': {
            'id': 'planner',
            'type': 'role',
            'role': 'planner',
            'params': {
                'objective': 'Plan the release',
                'deliverables': ['Release notes'],
                'acceptance_criteria': ['Tests pass'],
                'tier': 'heavy',
                'isolation': 'shared-context',
                'emits': 'assistant',
            },
        },
    }
    original = deepcopy(nodes)
    calls = []
    progress = _Progress(summary)
    runtime = OrchestrationReplanRuntime(
        nodes=nodes,
        progress=progress,
        transcript=_Transcript(),
        run_role=lambda node, context: calls.append((node, context)) or 'delta',
        verifier_roles=('critic', 'reviewer'),
        summary_limit=321,
    )
    return runtime, nodes, original, progress, calls


def test_replan_runtime_builds_one_immutable_progress_aware_delta_brief():
    runtime, nodes, original, progress, calls = _runtime()

    assert runtime.run('planner', 'upstream', 'missing build step', 2) == 'delta'

    planner, context = calls[0]
    assert nodes == original
    assert context == (
        'upstream\n\n## Structural plan defect to fix\nmissing build step'
        '\n\n## Progress so far (do NOT discard — produce a DELTA, '
        'do not regrow the plan)\ncompleted file edits'
    )
    assert '[RE-PLAN #2]' in planner['params']['objective']
    assert 'Plan the release' in planner['params']['objective']
    assert 'Release notes' in planner['params']['objective']
    assert 'Tests pass' in planner['params']['objective']
    assert 'deliverables' not in planner['params']
    assert 'acceptance_criteria' not in planner['params']
    assert planner['params']['tier'] == 'heavy'
    assert progress.calls == [(
        [{'role': 'worker', 'output': 'done'}],
        frozenset({'critic', 'reviewer'}),
        321,
    )]


def test_replan_runtime_omits_empty_optional_context_sections():
    runtime, _nodes, _original, _progress, calls = _runtime(summary='')

    assert runtime.run('planner', 'upstream only', None, 1) == 'delta'
    assert calls[0][1] == 'upstream only'


def test_engine_replan_methods_are_thin_runtime_facades():
    engine = (ROOT / 'lib' / 'orchestration_engine.py').read_text()
    runtime = (ROOT / 'lib' / 'orchestration_replan_runtime.py').read_text()

    method = engine.split('    def _run_replan(', 1)[1].split(
        '\n    def ', 1,
    )[0]
    assert 'self._replan_runtime.run(' in method
    assert 'Structural plan defect to fix' not in engine
    assert 'Structural plan defect to fix' in runtime
    assert 'render_role_brief' not in engine
    assert 'from lib.orchestration_engine import' not in runtime
