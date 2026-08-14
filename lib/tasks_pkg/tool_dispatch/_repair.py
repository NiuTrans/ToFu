# HOT_PATH
"""Harness self-repair UI surfacing — repair-summary + stale-round patching.

When the dispatcher recovers a malformed tool call (truncated/invalid JSON
or schema-shape coercions from lib/tool_input_repair.py), we attach a small
``_repaired`` descriptor to the tool round so the frontend can show a
"fixed" badge + tooltip explaining what was corrected.
"""

from __future__ import annotations

import json

from lib.log import get_logger
from lib.tasks_pkg.tool_display import _build_tool_round_entry

logger = get_logger(__name__)


# Human-readable labels for the schema-repair pattern names emitted by
# validate_then_repair.
_REPAIR_PATTERN_LABELS = {
    'null_omission': 'dropped null optional',
    'stringified_json': 'parsed stringified JSON',
    'stringified_primitive': 'coerced string to number/bool',
    'bare_string_to_array': 'wrapped string in array',
    'empty_placeholder_unwrap': 'unwrapped object to array',
    'leaked_tool_call_syntax': 'stripped leaked tool-call markup',
    'param_alias': 'renamed wrong-harness arg key',
}


def _build_repair_summary(json_repaired: bool, repair_log, tool_name_aliased: str | None = None,
                          resolved_tool_name: str | None = None) -> dict | None:
    """Build a UI-facing repair descriptor, or None when nothing was fixed.

    Returns ``{'label': str, 'detail': str, 'patterns': [...]}`` describing
    the auto-corrections applied to a tool call's arguments.

    ``tool_name_aliased`` / ``resolved_tool_name``: when the model called a
    tool by a wrong-harness name (e.g. ``read_file``) that we rewrote to the
    canonical Tofu tool (``read_files``), record it so the UI shows the fix.
    """
    parts = []
    patterns = []
    if tool_name_aliased and resolved_tool_name:
        parts.append(f'renamed tool {tool_name_aliased} → {resolved_tool_name}')
        patterns.append('tool_name_alias')
    if json_repaired:
        parts.append('recovered malformed JSON')
    for entry in (repair_log or []):
        try:
            path, pattern = entry
        except (ValueError, TypeError) as e:
            logger.debug('[tool_dispatch] skipping malformed repair_log entry %r: %s', entry, e)
            continue
        patterns.append(pattern)
        label = _REPAIR_PATTERN_LABELS.get(pattern, pattern)
        parts.append(f'{path}: {label}')
    if not parts:
        return None
    return {
        'label': 'auto-fixed',
        'detail': '; '.join(parts),
        'patterns': patterns,
    }


def _apply_repair_to_round(round_entry: dict, fn_name: str, fn_args: dict,
                           repair_summary: dict, project_enabled: bool,
                           conv_id) -> str | None:
    """Patch a stale (early-announced) round entry after a late repair.

    The streaming early-announce path renders the round display BEFORE the
    schema-repair pass runs, so a malformed arg (e.g. ``reads`` as a JSON
    string) produces a garbled display line.  Once repaired, rebuild the
    display from the corrected args and attach the ``_repaired`` descriptor.

    Returns the refreshed display query when it actually CHANGED, else
    ``None``.  The caller uses that signal to push a live display-patch
    frame — the garbled early-announce line is already on the user's screen,
    and the settle frame may be a long command away.
    """
    round_entry['_repaired'] = repair_summary
    try:
        tc_args_str = json.dumps(fn_args, ensure_ascii=False) if fn_args else '{}'
        _, fresh_entry, _ = _build_tool_round_entry(
            fn_name, fn_args, round_entry.get('toolCallId', ''), tc_args_str,
            round_entry.get('roundNum', 1) - 1, project_enabled, conv_id=conv_id,
        )
        # Only refresh the human-facing display string; keep roundNum, status,
        # llmRound, toolCallId, etc. intact on the live entry.
        fresh_query = fresh_entry.get('query')
        changed = bool(fresh_query) and fresh_query != round_entry.get('query')
        if fresh_query:
            round_entry['query'] = fresh_query
        round_entry['toolArgs'] = tc_args_str
        return fresh_query if changed else None
    except Exception as e:
        logger.debug('[ToolDispatch] repair display refresh failed for %s: %s',
                     fn_name, e)
        return None
