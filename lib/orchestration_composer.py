"""LLM-backed application boundary for natural-language graph composition.

Prompt/catalogue policy lives in :mod:`lib.orchestration.composer_prompt`;
this module owns the external model call and the validated result boundary.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.orchestration._layout import layout_definition
from lib.orchestration._validate import validate_definition
from lib.orchestration._definition_contract import SCHEMA_ID
from lib.orchestration.composer_prompt import (
    build_composer_messages,
)

logger = get_logger(__name__)

def _extract_json(text: str) -> dict | None:
    """Best-effort parse through the shared model-JSON boundary."""
    from lib.llm_json import extract_json
    result = extract_json(text)
    return result if isinstance(result, dict) else None


def _failure(error: str, *, reply: str = '') -> dict:
    """Return the stable Composer error envelope."""
    return {
        'ok': False,
        'reply': reply,
        'definition': None,
        'validation': None,
        'error': error,
    }


def compose(requirement: str, *, current: dict | None = None,
            history: list[dict] | None = None,
            llm_override=None) -> dict:
    """Generate or edit a graph through the stable authoring interface."""
    requirement = (requirement or '').strip()
    if not requirement:
        return _failure('empty requirement')

    messages = build_composer_messages(requirement, current, history)
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
                timeout=90,
            )
    except Exception as exc:
        logger.error('[Composer] LLM call failed: %s', exc, exc_info=True)
        return _failure(f'LLM call failed: {exc}')

    logger.info('[Composer] LLM returned %d chars, usage=%s',
                len(content or ''), str(usage)[:160])
    data = _extract_json(content or '')
    if not isinstance(data, dict):
        logger.warning('[Composer] no JSON object in LLM output; preview=%.200s',
                       content)
        return _failure('model did not return JSON')

    reply = str(data.get('reply') or '').strip()[:1000]
    definition = data.get('definition')
    if not isinstance(definition, dict):
        return _failure('model JSON missing "definition"', reply=reply)

    # The backend owns schema identity, fallback naming and canvas positions.
    definition['schema'] = SCHEMA_ID
    if not (isinstance(definition.get('name'), str)
            and definition['name'].strip()):
        definition['name'] = (current or {}).get('name') or 'Composed Flow'

    verdict = validate_definition(definition)
    if verdict['ok']:
        layout_definition(definition)

    logger.info('[Composer] composed name=%r nodes=%d ok=%s',
                definition.get('name'), len(definition.get('nodes') or []),
                verdict['ok'])
    return {
        'ok': verdict['ok'],
        'reply': reply or (
            'Updated the graph.' if current else 'Built a new graph.'),
        'definition': definition,
        'validation': verdict,
        'error': None if verdict['ok'] else 'composed graph failed validation',
    }


__all__ = ['compose']
