"""Pure helpers for compact, UI-safe persistence projections.

This module deliberately imports no database, task-manager, or application
bootstrap code.  Database migrations and row projection can therefore reuse
the exact runtime sanitizer without starting PostgreSQL, opening the active
application database, or constructing task singletons merely as an import
side effect.
"""

from __future__ import annotations

import copy
import json
import os

from lib.tools.result_envelope import sparse_result_items


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
    return project_usage_container_for_storage(event)


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
    transient_output_fields = (
        '_partialOutput',
        '_partialOutputTotalChars',
        '_partialOutputTruncated',
    )
    if (round_item.get('status') == 'done'
            and any(field in round_item for field in transient_output_fields)):
        round_item = dict(round_item)
        for field in transient_output_fields:
            round_item.pop(field, None)
    return round_item


# A durable turn frame must stay below the sidecar wire cap (64 MiB). The
# toolRounds lane dominates long tool-heavy turns; once its string payload
# crosses this budget the oldest settled rounds are compacted so full folds
# remain writable instead of degrading into the slim text-only lane.
TOOL_ROUNDS_FRAME_BUDGET_BYTES = int(os.environ.get(
    'TOFU_TOOL_ROUNDS_FRAME_BUDGET_BYTES', str(16 * 1024 * 1024)))
_TOOL_ROUNDS_KEEP_TAIL_INTACT = 24
_ELIDED_PAYLOAD_MIN_CHARS = 4096


def _string_payload_chars(value):
    """Cheap serialized-size proxy: JSON overhead stays proportional."""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return sum(_string_payload_chars(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_string_payload_chars(item) for item in value)
    return 0


def _elided_payload_stub(original_chars):
    return (f'[payload elided from the durable projection so the turn frame '
            f'stays writable — originalChars={original_chars}. The complete '
            f'output was delivered to the model live; consult task logs or '
            f're-run the tool for the full payload.]')


def _elide_string_field(container, field):
    value = container.get(field)
    if not isinstance(value, str) or len(value) < _ELIDED_PAYLOAD_MIN_CHARS:
        return 0
    stub = _elided_payload_stub(len(value))
    container[field] = stub
    return len(value) - len(stub)


def _elide_results_payloads(round_item):
    results = round_item.get('results')
    if not isinstance(results, list):
        return 0
    reclaimed = 0
    for result in results:
        if not isinstance(result, dict):
            continue
        for key, value in list(result.items()):
            if isinstance(value, str) and len(value) >= _ELIDED_PAYLOAD_MIN_CHARS:
                stub = _elided_payload_stub(len(value))
                result[key] = stub
                reclaimed += len(value) - len(stub)
    return reclaimed


def _swarm_stub_snapshot_from_handle(round_item):
    """Salvage a minimal ``_swarmSnapshot`` from a spawn handle.

    The swarm panel's reload path prefers ``round._swarmSnapshot`` and falls
    back to parsing the persisted spawn handle in ``toolContent``. Eliding
    that handle without a snapshot leaves the panel with no roster at all,
    so its header toggles an empty body and the click reads as dead. The
    donated stub keeps id/role/objective with an honest ``unknown`` status.
    ``version`` 0 ranks below every driver-produced snapshot (``stamp_round``
    only refuses a strictly older version), so a real snapshot stamped later
    still wins.
    """
    if not isinstance(round_item, dict) \
            or round_item.get('toolName') != 'spawn_agents':
        return None
    existing = round_item.get('_swarmSnapshot')
    if isinstance(existing, dict) and existing.get('agents'):
        return None
    raw = round_item.get('toolContent')
    if not isinstance(raw, str) or not raw:
        return None
    try:
        handle = json.loads(raw)
    except ValueError:
        return None
    items = sparse_result_items(handle)
    if items:
        handle = next(
            (entry for entry in items
             if isinstance(entry, dict)
             and (entry.get('agent_id')
                  or any(isinstance(entry.get(key), list)
                         for key in ('agents', 'completed', 'results')))),
            items[0] if isinstance(items[0], dict) else handle)
    agents = handle.get('agents') if isinstance(handle, dict) else None
    if not isinstance(agents, list):
        return None
    stubs = [{
        'id': entry.get('id') or '',
        'role': entry.get('role') or 'agent',
        'objective': entry.get('objective') or '',
        'status': 'unknown',
    } for entry in agents if isinstance(entry, dict)]
    if not stubs:
        return None
    return {
        'agents': stubs,
        'settled': False,
        'agentCount': len(stubs),
        'doneCount': 0,
        'version': 0,
        'recoveredFromHandle': True,
    }

def compact_tool_rounds_for_frame_budget(
    rounds,
    *,
    budget_bytes=None,
    keep_tail=_TOOL_ROUNDS_KEEP_TAIL_INTACT,
):
    """Elide the oldest settled rounds' heavy payloads when the toolRounds
    lane crosses the durable frame budget.

    Identity fields replay depends on (``toolCallId``, ``toolName``,
    ``toolArgs``, ``status``) are never touched, so the compacted lane stays a
    valid replay prefix; only free-text payload buffers (``toolContent``,
    ``results[]`` string values) become honest stubs. The newest ``keep_tail``
    rounds and any in-flight round stay intact. Copy-on-change: the input and
    every untouched round keep their identity, and the function is
    allocation-free while the lane already fits.
    """
    if not isinstance(rounds, list) or not rounds:
        return rounds
    budget = (TOOL_ROUNDS_FRAME_BUDGET_BYTES
              if budget_bytes is None else budget_bytes)
    total = sum(_string_payload_chars(item) for item in rounds)
    if total <= budget:
        return rounds
    compacted = list(rounds)
    last_eligible = max(0, len(compacted) - keep_tail)
    for position in range(last_eligible):
        if total <= budget:
            break
        item = compacted[position]
        if not isinstance(item, dict):
            continue
        if item.get('status') in ('running', 'pending'):
            continue
        clone = copy.deepcopy(item)
        stub_snapshot = _swarm_stub_snapshot_from_handle(item)
        reclaimed = _elide_string_field(clone, 'toolContent')
        reclaimed += _elide_results_payloads(clone)
        if reclaimed <= 0:
            continue
        if stub_snapshot is not None:
            clone['_swarmSnapshot'] = stub_snapshot
            total += _string_payload_chars(stub_snapshot)
        clone['_persistCompacted'] = True
        compacted[position] = clone
        total -= reclaimed
    return compacted

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


__all__ = [
    '_USAGE_TRANSIENT_KEYS',
    'sanitize_usage_for_persist',
    'sanitize_api_rounds_for_persist',
    'project_usage_container_for_storage',
    'project_event_usage_for_storage',
    'project_task_result_metadata_for_storage',
    'trim_tool_round_for_persist',
    'project_message_for_window',
]
