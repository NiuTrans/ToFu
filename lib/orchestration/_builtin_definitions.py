"""Canonical server-authored orchestration definitions.

Owns the reference graphs exposed to Studio and chat-mode adapters. Template
construction is independent of validation, storage and subflow expansion.
"""

from __future__ import annotations

from typing import Any

from lib.orchestration._definition_contract import SCHEMA_ID
from lib.orchestration._defaults import control_node_params, role_node_params
from lib.orchestration._layout import layout_definition
from lib.orchestration._role_personas import role_persona
from lib.orchestration._role_axes import DEFAULT_ROLE_TIER
from lib.orchestration.loop_policy import (
    DEFAULT_EXECUTOR_MAX_ITERATIONS,
    DEFAULT_MAX_ITERATIONS,
)


def _role_params(role: str, **overrides: Any) -> dict:
    """Build authored role params using the registry's canonical tier hint."""
    persona = role_persona(role)
    return role_node_params(
        tier=persona.get('tier') or DEFAULT_ROLE_TIER, **overrides,
    )


def build_blank_definition(*, name: str = 'Untitled Flow') -> dict:
    """Build an empty Studio draft through the server-owned interface."""
    return {'schema': SCHEMA_ID, 'name': name, 'nodes': [], 'edges': []}


def build_endpoint_definition(*, name: str = 'Endpoint Loop',
                              max_iterations: int = DEFAULT_MAX_ITERATIONS,
                              verifier: str = 'critic') -> dict:
    """Build the canonical Planner → Worker/Verifier endpoint loop."""
    definition = {
        'schema': SCHEMA_ID,
        'name': name,
        'nodes': [
            {'id': 'start', 'type': 'control', 'kind': 'start'},
            {'id': 'planner', 'type': 'role', 'role': 'planner',
             'params': _role_params(
                 'planner', objective='Rewrite the request into a structured '
                 'brief + checklist for the worker.',
             )},
            {'id': 'loop', 'type': 'control', 'kind': 'loop',
             'params': control_node_params(
                 'loop', max_iterations=int(max_iterations), verifier=verifier,
             )},
            {'id': 'worker', 'type': 'role', 'role': 'worker',
             'params': _role_params(
                 'worker', isolation='shared-context',
                 objective='Execute the plan. Your first tool call MUST be '
                 'state-changing — act, do not just analyze.',
             )},
            {'id': 'critic', 'type': 'role', 'role': verifier,
             'params': _role_params(
                 verifier, objective='Review the worker output against the '
                 'checklist. End with [VERDICT: STOP] or '
                 '[VERDICT: CONTINUE_WORKER].',
             )},
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


def build_autopilot_definition(*, name: str = 'Autopilot',
                               max_iterations: int =
                               DEFAULT_EXECUTOR_MAX_ITERATIONS,
                               worker: str = 'worker') -> dict:
    """Build the canonical Worker ⇄ Virtual User autopilot loop."""
    definition = {
        'schema': SCHEMA_ID,
        'name': name,
        'nodes': [
            {'id': 'start', 'type': 'control', 'kind': 'start'},
            {'id': 'loop', 'type': 'control', 'kind': 'loop',
             'params': control_node_params(
                 'loop', max_iterations=int(max_iterations),
                 verifier='virtual_user',
             )},
            {'id': 'worker', 'type': 'role', 'role': worker,
             'params': _role_params(
                 worker, isolation='shared-context', emits='assistant',
                 objective='Continue the task. Make concrete progress every '
                 'turn; act, do not just analyze.',
             )},
            {'id': 'vu', 'type': 'role', 'role': 'virtual_user',
             'params': _role_params(
                 'virtual_user', emits='user',
                 objective='Stand in for the human and drive the task to '
                 'completion per your virtual-user role. Emit [VERDICT: STOP] '
                 '(or [VU: TASK_DONE]) only when the objective is genuinely '
                 'met.',
             )},
            {'id': 'stop', 'type': 'control', 'kind': 'stop'},
        ],
        'edges': [
            {'from': 'start', 'to': 'loop'},
            {'from': 'loop', 'to': 'worker'},
            {'from': 'worker', 'to': 'vu'},
            {'from': 'vu', 'to': 'loop'},
            {'from': 'loop', 'to': 'stop'},
        ],
    }
    layout_definition(definition)
    return definition


def build_fanout_definition(*, name: str = 'Fan-out → Synthesize') -> dict:
    """Build the canonical parallel research and synthesis flow."""
    definition = {
        'schema': SCHEMA_ID,
        'name': name,
        'nodes': [
            {'id': 'start', 'type': 'control', 'kind': 'start',
             'params': control_node_params('start')},
            {'id': 'fanout', 'type': 'control', 'kind': 'parallel',
             'params': control_node_params('parallel')},
            *[
                {'id': f'researcher_{index}', 'type': 'role',
                 'role': 'researcher', 'params': _role_params('researcher')}
                for index in range(1, 4)
            ],
            {'id': 'join', 'type': 'control', 'kind': 'barrier',
             'params': control_node_params('barrier')},
            {'id': 'synthesizer', 'type': 'role', 'role': 'synthesizer',
             'params': _role_params(
                 'synthesizer',
                 objective='Merge all findings into one cited report.',
             )},
            {'id': 'stop', 'type': 'control', 'kind': 'stop',
             'params': control_node_params('stop')},
        ],
        'edges': [
            {'from': 'start', 'to': 'fanout'},
            *[
                {'from': 'fanout', 'to': f'researcher_{index}'}
                for index in range(1, 4)
            ],
            *[
                {'from': f'researcher_{index}', 'to': 'join'}
                for index in range(1, 4)
            ],
            {'from': 'join', 'to': 'synthesizer'},
            {'from': 'synthesizer', 'to': 'stop'},
        ],
    }
    layout_definition(definition)
    return definition


def build_adversarial_definition(*, name: str = 'Adversarial Verify') -> dict:
    """Build the canonical produce, challenge and synthesize flow."""
    definition = {
        'schema': SCHEMA_ID,
        'name': name,
        'nodes': [
            {'id': 'start', 'type': 'control', 'kind': 'start',
             'params': control_node_params('start')},
            {'id': 'producer', 'type': 'role', 'role': 'coder',
             'params': _role_params(
                 'coder', objective='Produce the change or finding.',
             )},
            {'id': 'reviewer', 'type': 'role', 'role': 'reviewer',
             'params': _role_params(
                 'reviewer',
                 objective='Try to refute the finding against a rubric.',
             )},
            {'id': 'synthesizer', 'type': 'role', 'role': 'synthesizer',
             'params': _role_params(
                 'synthesizer', objective='Keep only findings the reviewer '
                 'could not knock down.',
             )},
            {'id': 'stop', 'type': 'control', 'kind': 'stop',
             'params': control_node_params('stop')},
        ],
        'edges': [
            {'from': 'start', 'to': 'producer'},
            {'from': 'producer', 'to': 'reviewer'},
            {'from': 'reviewer', 'to': 'synthesizer'},
            {'from': 'synthesizer', 'to': 'stop'},
        ],
    }
    layout_definition(definition)
    return definition


__all__ = [
    'build_blank_definition',
    'build_endpoint_definition',
    'build_autopilot_definition',
    'build_fanout_definition',
    'build_adversarial_definition',
]
