"""Synthetic orchestration definitions shared by engine contract tests.

Application code exposes only product-supported built-ins. Tests that need a
planner/worker/verifier loop construct it here so engine semantics remain
covered without turning a test graph into a production compatibility API.
"""

from __future__ import annotations

from lib.orchestration._definition_contract import SCHEMA_ID
from lib.orchestration._defaults import control_node_params, role_node_params
from lib.orchestration._layout import layout_definition


def build_verifier_loop_definition(
    *,
    name: str = 'Verifier Loop Test Flow',
    max_iterations: int = 10,
    verifier: str = 'critic',
) -> dict:
    """Build a laid-out planner -> worker/verifier loop for tests."""
    definition = {
        'schema': SCHEMA_ID,
        'name': name,
        'nodes': [
            {'id': 'start', 'type': 'control', 'kind': 'start'},
            {
                'id': 'planner',
                'type': 'role',
                'role': 'planner',
                'params': role_node_params(
                    objective='Prepare a concrete execution checklist.',
                ),
            },
            {
                'id': 'loop',
                'type': 'control',
                'kind': 'loop',
                'params': control_node_params(
                    'loop',
                    max_iterations=int(max_iterations),
                    verifier=verifier,
                ),
            },
            {
                'id': 'worker',
                'type': 'role',
                'role': 'worker',
                'params': role_node_params(
                    isolation='shared-context',
                    objective='Execute the checklist and make concrete progress.',
                ),
            },
            {
                'id': 'critic',
                'type': 'role',
                'role': verifier,
                'params': role_node_params(
                    objective='Verify the result and emit a typed verdict.',
                ),
            },
            {'id': 'stop', 'type': 'control', 'kind': 'stop'},
        ],
        'edges': [
            {'from': 'start', 'to': 'planner'},
            {'from': 'planner', 'to': 'loop'},
            {'from': 'loop', 'to': 'worker'},
            {'from': 'worker', 'to': 'critic'},
            {'from': 'critic', 'to': 'loop'},
            {'from': 'loop', 'to': 'stop'},
        ],
    }
    layout_definition(definition)
    return definition


__all__ = ['build_verifier_loop_definition']
