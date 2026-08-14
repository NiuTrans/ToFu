"""Prompt and catalogue policy for orchestration graph composition.

This module owns the model-readable projection of canonical authoring
contracts.  It performs no model calls and no graph mutation, so the same
prompt policy can be tested or reused without crossing the Composer's I/O
boundary.
"""

from __future__ import annotations

import json

from lib.orchestration import CONTROL_KINDS, CONTROL_PARAM_SCHEMA
from lib.orchestration._definition_contract import SCHEMA_ID
from lib.orchestration._role_axes import KNOWN_ROLES
from lib.orchestration._role_specs import role_param_schema
from lib.orchestration.request_limit_contract import normalize_compose_history


_ROLE_HELP = {
    'planner': 'Rewrites the request into a structured brief + checklist.',
    'worker': 'Executes the plan with full tools. Use shared-context to make it persist across loop iterations.',
    'critic': 'Reviews the worker output against the checklist; emits a verdict (STOP / CONTINUE).',
    'reviewer': 'Fresh independent second-opinion read; outputs a punch list.',
    'researcher': 'Gathers + verifies info from web sources.',
    'coder': 'Reads / writes / edits code across files.',
    'analyst': 'Quantitative analysis of on-disk data.',
    'writer': 'Long-form prose from raw inputs.',
    'browser': 'Interacts with live browser tabs.',
    'synthesizer': 'Merges many agent outputs into one converged result.',
    'router': 'Classifies each item and routes it down a branch.',
    'general': 'Versatile fallback when no specialist fits.',
    'virtual_user': 'Stands in for the human (autopilot): auto-replies to keep a task going until done. Speaks as the USER side; emits [VU: TASK_DONE] when finished.',
}

_CONTROL_HELP = {
    'start': 'Entry point. Exactly one. The user request flows in here.',
    'stop': 'Terminal. Exactly one. The converged result returns to chat.',
    'loop': 'Repeat the wrapped step until a stop condition.',
    'parallel': 'Fan out downstream agents concurrently. Outgoing edges define the fan-out width.',
    'barrier': 'Wait for all parallel branches, then continue.',
    'branch': 'Route the flow down one path. Outgoing edges define the available routes.',
    'artifact': 'Declare an expected intermediate deliverable for the run log.',
    'human': 'Ask for approval/input or notify the user during a run.',
}


def _field_signature(spec: dict) -> str:
    """Render one backend FieldSpec as compact, model-readable guidance."""
    key = spec['key']
    kind = spec['kind']
    if kind == 'select':
        values = '|'.join(str(option['value'])
                          for option in spec.get('options', [])
                          if not option.get('disabled'))
        qualifier = 'suggested ' if spec.get('allowUnknown') else ''
        return f'{key} ({qualifier}{values})'
    if kind == 'int':
        bounds = ''
        if spec.get('min') is not None:
            bounds += f'>={spec["min"]}'
        upper = spec.get('max')
        if upper is None:
            upper = spec.get('runtimeMax')
        if upper is not None:
            bounds += f'<={upper}'
        return f'{key} (int{(" " + bounds) if bounds else ""})'
    return f'{key} ({kind})'


def _schema_signature(schema: list[dict]) -> str:
    return ', '.join(_field_signature(spec) for spec in schema) or 'none'


def composer_catalogue() -> tuple[str, str]:
    """Project canonical role/control contracts into prompt catalogue text."""
    roles = '\n'.join(
        f'  - {role}: {_ROLE_HELP.get(role, "")} Task params: '
        f'{_schema_signature(role_param_schema(role))}.'
        for role in sorted(KNOWN_ROLES)
    )
    controls = '\n'.join(
        f'  - {kind}: {_CONTROL_HELP.get(kind, "")} Params: '
        f'{_schema_signature(CONTROL_PARAM_SCHEMA[kind])}.'
        f'{" (at most one)" if CONTROL_KINDS[kind]["single"] else ""}'
        for kind in CONTROL_KINDS
    )
    return roles, controls


_SYSTEM = '''You are the Tofu Orchestration Composer. You design agent
orchestration graphs — endpoint-style loops, fan-out/synthesize flows,
adversarial verification, etc. — from a user's natural-language request.

You return STRICT JSON only: no prose, no markdown fences, no commentary
outside the JSON object.

A graph is a set of NODES wired by directed EDGES.

Node types:
  * "role"    — an agent. fields: id, type:"role", role:<role>, name?,
                params:{{objective?, tier?(light|standard|heavy),
                isolation?(fresh-context|shared-context),
                emits?(user|assistant)}}
  * "subflow" — a "big role" composed of small roles: ONE node that runs a
                whole nested graph. fields: id, type:"subflow", role:<label>,
                name?, params:{{definition:<a full nested graph>, emits?}}.
                Use it to encapsulate a reusable multi-step unit and design
                its internal context organisation independently.
  * "control" — structure. fields: id, type:"control", kind:<kind>,
                name?, params:{{...kind-specific}}

The MESSAGE axis (params.emits) is ORTHOGONAL to role: it sets which side
of the chat a turn lands on. Omit it to use the per-role default
(critic/reviewer/virtual_user → "user"; everything else → "assistant").
Set it explicitly only to override that default.

Available agent roles:
{roles}

Available control kinds:
{controls}

CRITICAL design rules:
  * Exactly one "start" and one "stop" node. The flow goes start → … → stop.
  * A "loop" expressing iterate-until-done should wrap a worker (+ a
    verifier critic) with the verifier edge looping back to the loop node.
  * A stateful worker that must remember across loop iterations MUST set
    params.isolation = "shared-context". One-shot parallel agents use
    "fresh-context".
  * Use a "synthesizer" after a "barrier" to merge fan-out results.
  * Parallel width and branch routes come ONLY from outgoing edges. Never
    write shadow counts/concurrency params for them.
  * AUTOPILOT pattern: a loop wrapping worker → virtual_user, where the
    virtual_user (emits "user") auto-replies to keep a task going and
    ends the loop with [VU: TASK_DONE]. The loop's verifier is
    "virtual_user".
  * Node ids must be unique short strings (e.g. "planner1", "loop1").
  * Do NOT invent roles/kinds outside the lists above.

Return JSON with EXACTLY this shape:
{{
  "reply": "one or two sentences describing what you built/changed",
  "definition": {{
    "schema": "{schema}",
    "name": "<short flow name>",
    "nodes": [ ... ],
    "edges": [ {{"from": "<id>", "to": "<id>"}} ]
  }}
}}
'''


def build_composer_messages(
    requirement: str,
    current: dict | None,
    history: list[dict] | None,
) -> list[dict]:
    """Build the bounded conversation supplied to the composition model."""
    roles, controls = composer_catalogue()
    system = _SYSTEM.format(roles=roles, controls=controls, schema=SCHEMA_ID)
    messages: list[dict] = [{'role': 'system', 'content': system}]
    messages.extend(normalize_compose_history(history))

    if current and (current.get('nodes') or current.get('edges')):
        serialized = json.dumps({
            'name': current.get('name'),
            'nodes': current.get('nodes'),
            'edges': current.get('edges'),
        }, ensure_ascii=False, default=str)[:8000]
        user = (f'Current graph:\n{serialized}\n\n'
                f'Modify it per this request:\n{requirement}')
    else:
        user = f'Create a new orchestration graph for this request:\n{requirement}'
    messages.append({'role': 'user', 'content': user})
    return messages


__all__ = ['build_composer_messages', 'composer_catalogue']
