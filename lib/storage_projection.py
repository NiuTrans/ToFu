"""Pure helpers for compact, UI-safe persistence projections.

This module deliberately imports no database, task-manager, or application
bootstrap code.  Database migrations and row projection can therefore reuse
the exact runtime sanitizer without starting PostgreSQL, opening the active
application database, or constructing task singletons merely as an import
side effect.
"""

from __future__ import annotations


# Backend-only stream diagnostics.  Keep the legacy spellings documented, but
# treat the entire ``_wire_`` namespace as transient so newly-added probes do
# not silently become durable multi-megabyte payloads.
_USAGE_TRANSIENT_KEYS = ('_wire_fp', '_wire_static', '_wire_routing')
_WINDOW_HEAVY_FIELDS = (
    'segments', 'toolRounds', '_continueToolRounds', 'toolSummary',
)
_API_ROUND_FIELDS = ('apiRounds', '_continueApiRounds')


def _is_usage_transient_key(key) -> bool:
    return (key in _USAGE_TRANSIENT_KEYS
            or (isinstance(key, str) and key.startswith('_wire_')))


def sanitize_usage_for_persist(usage):
    """Copy *usage* only when transient wire diagnostics must be removed."""
    if not isinstance(usage, dict):
        return usage
    if not any(_is_usage_transient_key(k) for k in usage):
        return usage
    return {
        k: v for k, v in usage.items() if not _is_usage_transient_key(k)
    }


def sanitize_api_rounds_for_persist(api_rounds):
    """Remove wire diagnostics while retaining public round/cost/token data."""
    if not isinstance(api_rounds, list):
        return api_rounds
    out = []
    changed = False
    for round_item in api_rounds:
        if (isinstance(round_item, dict)
                and isinstance(round_item.get('usage'), dict)):
            clean_usage = sanitize_usage_for_persist(round_item['usage'])
            if clean_usage is not round_item['usage']:
                round_item = {**round_item, 'usage': clean_usage}
                changed = True
        out.append(round_item)
    return out if changed else api_rounds


def project_usage_container_for_storage(container):
    """Project every persisted usage-bearing field on one mapping.

    This is shared by task-result metadata, durable task events, and offline
    maintenance so all three paths remove the same private diagnostics without
    importing the database or task runtime.
    """
    if not isinstance(container, dict):
        return container
    projected = container
    usage = container.get('usage')
    clean_usage = sanitize_usage_for_persist(usage)
    if clean_usage is not usage:
        projected = dict(container)
        projected['usage'] = clean_usage
    for field in _API_ROUND_FIELDS:
        rounds = container.get(field)
        clean_rounds = sanitize_api_rounds_for_persist(rounds)
        if clean_rounds is rounds:
            continue
        if projected is container:
            projected = dict(container)
        projected[field] = clean_rounds
    return projected


def project_event_usage_for_storage(event):
    """Strip private wire diagnostics from all durable event usage shapes."""
    if not isinstance(event, dict):
        return event
    projected = project_usage_container_for_storage(event)
    for key in ('committedMessage', 'parentMessage'):
        value = event.get(key)
        clean_value = project_usage_container_for_storage(value)
        if clean_value is value:
            continue
        if projected is event:
            projected = dict(event)
        projected[key] = clean_value
    return projected


def project_task_result_metadata_for_storage(metadata):
    """Project task-result recovery metadata without dropping public fields."""
    return project_usage_container_for_storage(metadata)


def trim_tool_round_for_persist(round_item):
    """Drop a completed command round's redundant live output buffer.

    The completed output remains authoritative in ``results[0].output`` or
    ``toolContent``. A running round keeps ``_partialOutput`` so reconnect can
    still replay live progress. The input is never mutated.
    """
    if not isinstance(round_item, dict):
        return round_item
    if round_item.get('status') == 'done' and round_item.get('_partialOutput'):
        round_item = dict(round_item)
        round_item.pop('_partialOutput', None)
    return round_item


def project_message_for_window(message):
    """Return the compact, UI-complete first-paint projection of a message.

    The lossless message remains authoritative elsewhere. This projection
    removes tool-timeline bulk and every known message-level route by which
    backend ``_wire_*`` diagnostics can reach storage or HTTP. Public usage,
    dispatch, cost, and round fields are retained. Copy-on-change semantics
    keep ordinary messages allocation-free and never mutate caller state.
    """
    if not isinstance(message, dict):
        return message

    strips_heavy = any(key in message for key in _WINDOW_HEAVY_FIELDS)
    projected = ({key: value for key, value in message.items()
                  if key not in _WINDOW_HEAVY_FIELDS}
                 if strips_heavy else message)
    changed = strips_heavy

    def ensure_copy():
        nonlocal projected, changed
        if projected is message:
            projected = dict(message)
        changed = True

    usage = message.get('usage')
    if isinstance(usage, dict):
        clean_usage = sanitize_usage_for_persist(usage)
        if clean_usage is not usage:
            ensure_copy()
            projected['usage'] = clean_usage

    for field in _API_ROUND_FIELDS:
        rounds = message.get(field)
        if not isinstance(rounds, list):
            continue
        clean_rounds = sanitize_api_rounds_for_persist(rounds)
        if clean_rounds is not rounds:
            ensure_copy()
            projected[field] = clean_rounds

    live_usage = message.get('_liveLastRoundUsage')
    if isinstance(live_usage, dict) and isinstance(live_usage.get('usage'), dict):
        clean_live = sanitize_usage_for_persist(live_usage['usage'])
        if clean_live is not live_usage['usage']:
            ensure_copy()
            projected['_liveLastRoundUsage'] = {
                **live_usage, 'usage': clean_live,
            }

    if not changed:
        return message
    projected['_trimmed'] = True
    tool_rounds = message.get('toolRounds')
    if isinstance(tool_rounds, list):
        projected['_trimmedToolRoundCount'] = len(tool_rounds)
    return projected


# Backward-compatible private names: the task-manager facade has historically
# exported these spellings and tests/migration scripts import them directly.
_sanitize_usage_for_persist = sanitize_usage_for_persist
_sanitize_api_rounds_for_persist = sanitize_api_rounds_for_persist
_trim_round_for_persist = trim_tool_round_for_persist


__all__ = [
    '_USAGE_TRANSIENT_KEYS',
    'sanitize_usage_for_persist',
    'sanitize_api_rounds_for_persist',
    'project_usage_container_for_storage',
    'project_event_usage_for_storage',
    'project_task_result_metadata_for_storage',
    'trim_tool_round_for_persist',
    'project_message_for_window',
    '_sanitize_usage_for_persist',
    '_sanitize_api_rounds_for_persist',
    '_trim_round_for_persist',
]
