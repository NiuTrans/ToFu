"""Backend-owned orchestration ingress and authoring input limits."""

from __future__ import annotations

from lib.orchestration._definition_contract import MAX_NAME_LEN, MAX_NODES
from lib.orchestration.contract_schema import contract_snapshot_schema
from lib.orchestration._subflow_contract import MAX_SUBFLOW_DEPTH


MAX_COMPOSE_REQUIREMENT_LENGTH = 4000
MAX_COMPOSE_HISTORY_ITEMS = 8
MAX_COMPOSE_HISTORY_CONTENT_LENGTH = 4000
MAX_RUN_INPUT_LENGTH = 8000
MAX_HUMAN_INPUT_LENGTH = 8000


def request_limits_contract() -> dict:
    """Publish authoring-input limits enforced by ingress or validation."""
    return {
        'definitionName': {
            'maxLength': MAX_NAME_LEN,
        },
        'definitionNodes': {
            'maxItems': MAX_NODES,
        },
        'subflowDepth': {
            'maxDepth': MAX_SUBFLOW_DEPTH,
        },
        'composeRequirement': {
            'maxLength': MAX_COMPOSE_REQUIREMENT_LENGTH,
        },
        'composeHistory': {
            # Rolling clients may send more; HTTP ingress keeps the newest
            # window. New clients avoid uploading data that is never consumed.
            'retainedItems': MAX_COMPOSE_HISTORY_ITEMS,
            'messageMaxLength': MAX_COMPOSE_HISTORY_CONTENT_LENGTH,
        },
        'runInput': {
            'maxLength': MAX_RUN_INPUT_LENGTH,
        },
        'humanInput': {
            'maxLength': MAX_HUMAN_INPUT_LENGTH,
        },
    }


def normalize_compose_history(history: object) -> list[dict[str, str]]:
    """Return the bounded user/assistant text window accepted by Composer."""
    if not isinstance(history, list):
        return []
    normalized = []
    for turn in history[-MAX_COMPOSE_HISTORY_ITEMS:]:
        if not isinstance(turn, dict):
            continue
        role = turn.get('role')
        content = turn.get('content')
        if role not in ('user', 'assistant') or not isinstance(content, str):
            continue
        content = content.strip()
        if content:
            normalized.append({
                'role': role,
                'content': content[:MAX_COMPOSE_HISTORY_CONTENT_LENGTH],
            })
    return normalized


def request_limits_contract_schema() -> dict:
    """Describe every ingress limit from the backend-enforced snapshot."""
    return contract_snapshot_schema(request_limits_contract())


__all__ = [
    'MAX_NAME_LEN',
    'MAX_NODES',
    'MAX_SUBFLOW_DEPTH',
    'MAX_COMPOSE_REQUIREMENT_LENGTH',
    'MAX_COMPOSE_HISTORY_ITEMS',
    'MAX_COMPOSE_HISTORY_CONTENT_LENGTH',
    'MAX_RUN_INPUT_LENGTH',
    'MAX_HUMAN_INPUT_LENGTH',
    'normalize_compose_history',
    'request_limits_contract', 'request_limits_contract_schema',
]
