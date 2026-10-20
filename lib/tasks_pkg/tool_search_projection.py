"""Pure Tool Search result-to-display projection shared by execution and replay.

The retained result JSON owns match/count/cursor data. These adapters produce
presentation only; they never query the catalog or grant execution authority.
"""
from __future__ import annotations

import json
from typing import Any


def _schema_type_label(schema: Any) -> str:
    if not isinstance(schema, dict):
        return 'value'
    raw_type = schema.get('type')
    if isinstance(raw_type, list):
        types = [str(value) for value in raw_type if value != 'null']
        return ' | '.join(types) or 'value'
    if raw_type:
        return str(raw_type)
    if 'enum' in schema:
        return 'enum'
    if 'oneOf' in schema or 'anyOf' in schema:
        return 'variant'
    return 'value'


def tool_search_display_results(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Project result JSON into the minimal catalog/availability card rows."""
    rows: list[dict[str, Any]] = []
    for item in result.get('items') or []:
        if not isinstance(item, dict) or not item.get('name'):
            continue
        schema = item.get('arguments_schema')
        schema = schema if isinstance(schema, dict) else {}
        properties = schema.get('properties')
        properties = properties if isinstance(properties, dict) else {}
        required = {str(value) for value in (schema.get('required') or [])}
        arguments = []
        for arg_name, arg_schema in properties.items():
            arguments.append({
                'name': str(arg_name),
                'type': _schema_type_label(arg_schema),
                'required': str(arg_name) in required,
            })
        rows.append({
            'type': 'tool_catalog_match',
            'toolName': str(item['name']),
            'title': str(item['name']),
            'namespace': str(item.get('namespace') or 'general'),
            'snippet': str(item.get('summary') or item.get('description') or ''),
            'arguments': arguments,
            'score': item.get('score'),
        })
    for availability in result.get('capability_availability') or []:
        rows.append({
            'type': 'tool_catalog_availability',
            'namespace': availability['namespace'],
            'reasonCode': availability['reason_code'],
            'message': availability['message'],
            'recoveryHint': availability['recovery_hint'],
        })
    return rows


def tool_search_metadata(result: dict[str, Any]) -> dict[str, Any]:
    """Return the same count/pagination fields for rounds and live events."""
    return {
        'toolSearchTotal': int(result.get('total') or 0),
        'toolSearchNextCursor': result.get('next_cursor'),
        'toolSearchFailOpen': bool(result.get('fail_open')),
    }


def cached_tool_search_projection(content: Any) -> tuple[list[dict], dict] | None:
    """Decode a successful retained response, or require normal re-execution.

    Old cache entries need no new tuple slots: the original JSON already owns
    the full display data. Invalid/error entries must not fabricate empty hits.
    """
    if not isinstance(content, str):
        return None
    try:
        result = json.loads(content)
        if (not isinstance(result, dict) or result.get('status') != 'ok'
                or not isinstance(result.get('items'), list)):
            return None
        total = result.get('total')
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            return None
        if any(not isinstance(item, dict) or not item.get('name')
               for item in result['items']):
            return None
        return tool_search_display_results(result), tool_search_metadata(result)
    except (ValueError, TypeError, KeyError, RecursionError):
        return None
