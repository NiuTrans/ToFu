"""Canonical proposed-plan and execution-handoff documents.

Responsibility: parse the model's ``<proposed_plan>`` deliverable, mint its
stable content identity, normalize the durable turn sidecars, and compose the
exact model handoff used after acceptance.  UI presentation, mode policy and
storage mutations live elsewhere; every producer and consumer shares these
pure shapes.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import re
from typing import Any


# One plan is copied into a bounded number of permanent authority and expiring
# replay documents. Unicode UTF-8 uses at most four bytes per scalar, so both
# the contract character ceiling and its worst-case byte budget are explicit.
MAX_PROPOSED_PLAN_CHARS = 64_000
MAX_PROPOSED_PLAN_UTF8_BYTES = 4 * MAX_PROPOSED_PLAN_CHARS
PLAN_EXECUTION_CONTEXT_MODES = frozenset({'current', 'fresh'})
_CONTENT_NOT_SUPPLIED = object()

_PROPOSED_PLAN_RE = re.compile(
    r'<proposed_plan>\s*\n?(.*?)\n?\s*</proposed_plan>',
    re.DOTALL | re.IGNORECASE,
)


def _normalized_plan_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_PROPOSED_PLAN_CHARS:
        return None
    try:
        encoded_size = len(text.encode('utf-8'))
    except UnicodeEncodeError:
        return None
    if encoded_size > MAX_PROPOSED_PLAN_UTF8_BYTES:
        return None
    return text


def extract_proposed_plan(text: Any) -> str | None:
    """Return the final complete plan block, bounded for durable projection."""
    if not isinstance(text, str) or '<proposed_plan>' not in text.lower():
        return None
    matches = _PROPOSED_PLAN_RE.findall(text)
    if not matches:
        return None
    return _normalized_plan_text(matches[-1])


def proposed_plan_id(text: str) -> str:
    """Content-address one accepted plan without depending on row identity."""
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]
    return f'plan_{digest}'


def proposed_plan_document(value: Any = None, *, content: Any = _CONTENT_NOT_SUPPLIED
                           ) -> dict[str, Any] | None:
    """Normalize an explicit or content-derived proposed-plan sidecar.

    When ``content`` is supplied, a complete tagged block is mandatory and is
    the only text authority. This prevents a stale or forged sidecar from
    disagreeing with the transcript the model and human can see. Passing only
    ``value`` remains useful for normalizing an already-isolated contract
    document such as an execution handoff fixture.
    """
    if content is not _CONTENT_NOT_SUPPLIED:
        text = extract_proposed_plan(content)
        if text is None:
            return None
        if isinstance(value, Mapping):
            explicit_text = _normalized_plan_text(value.get('text'))
            if explicit_text != text:
                return None
            explicit_plan_id = value.get('planId')
            if (explicit_plan_id is not None
                    and explicit_plan_id != proposed_plan_id(text)):
                return None
            if value.get('blockId') not in (None, 'proposed-plan'):
                return None
            if value.get('revision') not in (None, 1):
                return None
            if value.get('format') not in (None, 'markdown'):
                return None
    else:
        text = None
    if text is None and isinstance(value, Mapping):
        text = _normalized_plan_text(value.get('text'))
    if text is None:
        return None
    return {
        'blockId': 'proposed-plan',
        'planId': proposed_plan_id(text),
        'revision': 1,
        'format': 'markdown',
        'text': text,
    }


def plan_execution_document(value: Any) -> dict[str, Any] | None:
    """Normalize the immutable handoff stored on an execution input turn."""
    if not isinstance(value, Mapping):
        return None
    plan_text = value.get('planText')
    source_turn_id = value.get('sourceTurnId')
    source_revision = value.get('sourceProjectionRevision')
    context_mode = str(value.get('contextMode') or '')
    plan_text = _normalized_plan_text(plan_text)
    if (plan_text is None
            or not isinstance(source_turn_id, str) or not source_turn_id.strip()
            or isinstance(source_revision, bool)
            or not isinstance(source_revision, int) or source_revision < 0
            or context_mode not in PLAN_EXECUTION_CONTEXT_MODES):
        return None
    plan_id = proposed_plan_id(plan_text)
    if value.get('planId') not in (None, plan_id):
        return None
    if value.get('blockId') not in (None, 'plan-execution'):
        return None
    return {
        'blockId': 'plan-execution',
        'planId': plan_id,
        'sourceTurnId': source_turn_id.strip(),
        'sourceProjectionRevision': source_revision,
        'contextMode': context_mode,
        'planText': plan_text,
    }


def plan_execution_model_prompt(value: Mapping[str, Any]) -> str:
    """Compose an unambiguous, exact execution instruction for the model."""
    document = plan_execution_document(value)
    if document is None:
        raise ValueError('invalid plan execution handoff')
    payload = json.dumps({
        'planId': document['planId'],
        'sourceTurnId': document['sourceTurnId'],
        'sourceProjectionRevision': document['sourceProjectionRevision'],
        'markdown': document['planText'],
    }, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    # Keep the envelope delimiter structural even when untrusted repository
    # text found its way into a user-approved plan. JSON decoders recover the
    # exact characters, while no raw closing tag can appear inside the body.
    payload = (payload.replace('&', '\\u0026')
               .replace('<', '\\u003c')
               .replace('>', '\\u003e'))
    return (
        'Execute the accepted plan below now. The JSON payload is the exact '
        'accepted plan, not a request to draft another plan. Work through it '
        'with the available tools, verify the result, and report completion. '
        'If a genuinely new user decision is required, use ask_human instead '
        'of guessing.\n\n<accepted_plan_json>\n'
        f'{payload}\n'
        '</accepted_plan_json>'
    )


__all__ = [
    'MAX_PROPOSED_PLAN_CHARS',
    'MAX_PROPOSED_PLAN_UTF8_BYTES',
    'PLAN_EXECUTION_CONTEXT_MODES',
    'extract_proposed_plan',
    'plan_execution_document',
    'plan_execution_model_prompt',
    'proposed_plan_document',
    'proposed_plan_id',
]
