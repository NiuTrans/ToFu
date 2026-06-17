"""lib/orchestration_composer.py — LLM-driven orchestration authoring.

Turns a natural-language requirement (plus an optional *current* graph)
into a validated ``tofu.orchestration/v1`` definition. This is the brain
behind the Studio's "discuss with the model and watch the graph change"
experience: the frontend sends the conversation + current graph, the
backend reasons and returns a NEW graph + a chat reply.

Design rules (CLAUDE.md):
  * All reasoning is server-side. The frontend never builds graphs.
  * We never trust the model's JSON blindly — it is parsed, the schema
    constants below are the source of truth, and the result is run
    through :func:`lib.orchestration.validate_definition` +
    :func:`lib.orchestration.layout_definition` before returning.
  * Mirrors the optimizer proposer pattern: build prompt → smart_chat →
    strip fences → json.loads → validate.
"""

from __future__ import annotations

import json
import re

from lib.log import get_logger
from lib.orchestration import (
    CONTROL_KINDS,
    KNOWN_ROLES,
    SCHEMA_ID,
    layout_definition,
    validate_definition,
)

logger = get_logger(__name__)


# Human-readable capability catalogue injected into the system prompt so
# the model knows exactly which roles / control nodes / params exist.
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
    'loop': 'Repeat the wrapped step until a stop condition. params: '
            'max_iterations (int), stop_condition (verdict:STOP|no_new_findings|max_only), verifier (role).',
    'parallel': 'Fan out downstream agents concurrently. params: max_concurrent (int), per_item (bool).',
    'barrier': 'Wait for all parallel branches, then continue.',
    'branch': 'Route the flow down a path chosen by a classifier. params: classifier (role), branches (int).',
}


def _catalogue() -> str:
    roles = '\n'.join(
        f'  - {r}: {_ROLE_HELP.get(r, "")}' for r in sorted(KNOWN_ROLES)
    )
    controls = '\n'.join(
        f'  - {k}: {_CONTROL_HELP.get(k, "")}'
        f'{" (at most one)" if CONTROL_KINDS[k]["single"] else ""}'
        for k in CONTROL_KINDS
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
  * AUTOPILOT pattern: a loop wrapping worker → virtual_user, where the
    virtual_user (emits "user") auto-replies to keep the worker going and
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


def _strip_fences(text: str) -> str:
    s = (text or '').strip()
    if s.startswith('```'):
        s = s.split('\n', 1)[-1] if '\n' in s else s[3:]
        if s.endswith('```'):
            s = s[:-3]
    return s.strip()


def _extract_json(text: str) -> dict | None:
    """Best-effort parse: try whole string, else first {...} block."""
    s = _strip_fences(text)
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        pass
    m = re.search(r'\{.*\}', s, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[Composer] inner-brace parse failed: %s', e)
    return None


def _build_messages(requirement: str, current: dict | None,
                    history: list[dict] | None) -> list[dict]:
    roles, controls = _catalogue()
    system = _SYSTEM.format(roles=roles, controls=controls, schema=SCHEMA_ID)
    messages: list[dict] = [{'role': 'system', 'content': system}]

    # Replay prior turns (user/assistant text only) for conversational edits.
    for turn in (history or [])[-8:]:
        role = turn.get('role')
        content = turn.get('content')
        if role in ('user', 'assistant') and isinstance(content, str) and content.strip():
            messages.append({'role': role, 'content': content[:4000]})

    if current and (current.get('nodes') or current.get('edges')):
        cur = json.dumps({
            'name': current.get('name'),
            'nodes': current.get('nodes'),
            'edges': current.get('edges'),
        }, ensure_ascii=False, default=str)[:8000]
        user = (f'Current graph:\n{cur}\n\n'
                f'Modify it per this request:\n{requirement}')
    else:
        user = f'Create a new orchestration graph for this request:\n{requirement}'
    messages.append({'role': 'user', 'content': user})
    return messages


def compose(requirement: str, *, current: dict | None = None,
            history: list[dict] | None = None,
            llm_override=None) -> dict:
    """Generate (or edit) an orchestration definition from NL.

    Args:
        requirement: The user's latest natural-language instruction.
        current: Optional current definition to modify.
        history: Optional prior [{role, content}] chat turns.
        llm_override: optional callable(messages)->(content, usage) for tests.

    Returns:
        ``{'ok': bool, 'reply': str, 'definition': dict|None,
           'validation': {...}, 'error': str|None}``. ``ok`` is True iff a
        structurally-valid definition was produced.
    """
    requirement = (requirement or '').strip()
    if not requirement:
        return {'ok': False, 'reply': '', 'definition': None,
                'validation': None, 'error': 'empty requirement'}

    messages = _build_messages(requirement, current, history)

    try:
        if llm_override is not None:
            content, usage = llm_override(messages)
        else:
            from lib.llm_dispatch import smart_chat
            content, usage = smart_chat(
                messages=messages,
                max_tokens=3000,
                temperature=0,
                capability='text',
                log_prefix='[Composer]',
            )
    except Exception as e:
        logger.error('[Composer] LLM call failed: %s', e, exc_info=True)
        return {'ok': False, 'reply': '', 'definition': None,
                'validation': None, 'error': f'LLM call failed: {e}'}

    logger.info('[Composer] LLM returned %d chars, usage=%s',
                len(content or ''), str(usage)[:160])

    data = _extract_json(content or '')
    if not isinstance(data, dict):
        logger.warning('[Composer] no JSON object in LLM output; preview=%.200s',
                       content)
        return {'ok': False, 'reply': '', 'definition': None,
                'validation': None, 'error': 'model did not return JSON'}

    reply = str(data.get('reply') or '').strip()[:1000]
    defn = data.get('definition')
    if not isinstance(defn, dict):
        return {'ok': False, 'reply': reply, 'definition': None,
                'validation': None, 'error': 'model JSON missing "definition"'}

    # Force our schema id + ensure name; never trust the model for these.
    defn['schema'] = SCHEMA_ID
    if not (isinstance(defn.get('name'), str) and defn['name'].strip()):
        defn['name'] = (current or {}).get('name') or 'Composed Flow'

    verdict = validate_definition(defn)
    if verdict['ok']:
        layout_definition(defn)   # backend owns positioning

    logger.info('[Composer] composed name=%r nodes=%d ok=%s',
                defn.get('name'), len(defn.get('nodes') or []), verdict['ok'])
    return {
        'ok': verdict['ok'],
        'reply': reply or ('Updated the graph.' if current else 'Built a new graph.'),
        'definition': defn,
        'validation': verdict,
        'error': None if verdict['ok'] else 'composed graph failed validation',
    }


__all__ = ['compose']
