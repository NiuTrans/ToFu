"""Best-effort context/tool telemetry used by cost experiments and benchmarks.

The helpers in this module never affect request construction.  They use local
token counters where possible, fall back to a conservative character estimate,
and keep only bounded numeric/fingerprint metadata on the live task.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

_MAX_ROUND_SNAPSHOTS = 512


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'))
    except (TypeError, ValueError) as exc:
        logger.debug('[ContextTelemetry] JSON serialization fallback: %s', exc)
        return str(value)


def _count_text(value: Any, *, model: str = '') -> int:
    text = value if isinstance(value, str) else _json_text(value)
    if not text:
        return 0
    try:
        from lib.token_counter import count_text
        return max(0, int(count_text(text, model=model)))
    except Exception as exc:
        logger.debug('[ContextTelemetry] token count fallback: %s', exc)
        cjk = sum(1 for ch in text if '\u2e80' <= ch <= '\u9fff')
        return max(1, cjk + (len(text) - cjk + 3) // 4)


def _content_tokens(message: dict, *, model: str = '') -> int:
    content = message.get('content')
    return _count_text(content, model=model)


def tool_schema_tokens(tools: Any, *, model: str = '') -> int:
    return _count_text(tools or [], model=model) if tools else 0


def tool_result_tokens(messages: Any, *, model: str = '') -> int:
    if not isinstance(messages, list):
        return 0
    return sum(_content_tokens(message, model=model)
               for message in messages
               if isinstance(message, dict) and message.get('role') == 'tool')


def raw_tool_result_tokens(task: dict, *, model: str = '') -> int:
    """Count the pre-compaction tool content retained on task/round metadata."""
    total = 0
    seen: set[str] = set()
    for round_entry in task.get('toolRounds') or ():
        if not isinstance(round_entry, dict):
            continue
        identity = str(round_entry.get('toolCallId') or id(round_entry))
        if identity in seen:
            continue
        seen.add(identity)
        reported = round_entry.get('rawToolTokens')
        if reported is not None:
            try:
                total += max(0, int(reported or 0))
                continue
            except (TypeError, ValueError) as exc:
                logger.debug(
                    '[ContextTelemetry] invalid rawToolTokens ignored: %s', exc)
        content = (round_entry.get('rawToolContent')
                   if round_entry.get('rawToolContent') is not None
                   else round_entry.get('toolContent'))
        total += _count_text(content or '', model=model)
    return total


def stable_prefix_tokens(messages: Any, *, model: str = '') -> int:
    """Count the leading system/developer instruction floor."""
    if not isinstance(messages, list):
        return 0
    total = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get('role') not in ('system', 'developer'):
            break
        total += _content_tokens(message, model=model)
    return total


def prefix_fingerprint(messages: Any) -> str:
    try:
        from lib.tasks_pkg.wire_fingerprint import static_prefix_hash
        return str(static_prefix_hash(messages or []))
    except Exception as exc:
        logger.debug('[ContextTelemetry] canonical prefix hash fallback: %s',
                     exc)
        return hashlib.sha256(
            _json_text(messages or []).encode('utf-8')).hexdigest()[:24]


def capture_round_context(task: dict, messages: list, tools: Any,
                          *, round_num: int, model: str) -> dict:
    """Capture the request-side token shape immediately before an API call."""
    snapshot = {
        'round': int(round_num) + 1,
        'stablePrefixTokens': stable_prefix_tokens(messages, model=model),
        'toolSchemaTokens': tool_schema_tokens(tools, model=model),
        'rawToolResultTokens': raw_tool_result_tokens(task, model=model),
        'modelToolResultTokens': tool_result_tokens(messages, model=model),
        'prefixFingerprint': prefix_fingerprint(messages),
    }
    rows = task.setdefault('_contextTelemetryRounds', [])
    if isinstance(rows, list):
        rows.append(snapshot)
        if len(rows) > _MAX_ROUND_SNAPSHOTS:
            del rows[:-_MAX_ROUND_SNAPSHOTS]
    return snapshot


def stamp_tool_exposure(task: dict, *, mode: str, available: int,
                        exposed: int, routed_keys: list[str] | None = None,
                        omitted_keys: list[str] | None = None) -> None:
    task['_toolExposureTelemetry'] = {
        'mode': mode,
        'availableTools': max(0, int(available or 0)),
        'exposedTools': max(0, int(exposed or 0)),
        'routedKeys': list(routed_keys or []),
        'omittedKeys': list(omitted_keys or []),
    }


def record_compaction_event(task: dict | None, *, trigger: str,
                            reason: str = '', tokens_before: int = 0,
                            tokens_after: int = 0, archive_id: Any = None,
                            evidence_retained: list[str] | None = None,
                            evidence_lost: list[str] | None = None) -> None:
    if not isinstance(task, dict):
        return
    event = {
        'trigger': str(trigger or ''),
        'reason': str(reason or '')[:500],
        'tokensBefore': max(0, int(tokens_before or 0)),
        'tokensAfter': max(0, int(tokens_after or 0)),
        'evidenceRetained': list(evidence_retained or []),
        'evidenceLost': list(evidence_lost or []),
    }
    if archive_id is not None:
        event['archiveId'] = archive_id
    task.setdefault('_contextCompactionEvents', []).append(event)


def record_mcp_search(task: dict, *, misses: int = 0) -> None:
    task['_mcpSearchCount'] = int(task.get('_mcpSearchCount') or 0) + 1
    task['_mcpSearchMissCount'] = (
        int(task.get('_mcpSearchMissCount') or 0) + max(0, int(misses or 0)))


__all__ = [
    'capture_round_context', 'prefix_fingerprint',
    'raw_tool_result_tokens', 'record_compaction_event', 'record_mcp_search',
    'stable_prefix_tokens', 'stamp_tool_exposure', 'tool_result_tokens',
    'tool_schema_tokens',
]
