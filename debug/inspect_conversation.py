#!/usr/bin/env python3
"""inspect_conversation — one-shot debug dump for a conversation ID.

Responsibility: given a conversation ID (the sidebar copy-ID button copies
``data-conv-id`` values like ``mt18xr3wfs0rbq``), answer "where does this
conversation live and what does it contain" in ONE read-only pass, so an
agent never has to guess table names or storage modes.

Entry point: ``python3 debug/inspect_conversation.py <conv_id> [--full]
[--raw | --json] [--context] [--tools] [--no-anomalies] [--logs N] [--no-logs]
[--log-match REGEX] [--db PATH] [--user-id N] [--logs-dir PATH]``
or ``<conv_id> --api-url http://127.0.0.1:15000 [--api-token-env NAME]``.
With no source flags the inspector follows the configured backend: SQLite
deployments auto-discover the live Sidecar/fastpath authority; ``tofudb``
deployments auto-discover the running application's loopback API from the
configured ``PORT`` (default 15000) and read through the v4 conversation
endpoints instead of erroring out.

Dependencies: ``lib.storage_authority_sql.offline`` for fail-closed active-authority
discovery plus the bounded query-only connection, and
``lib.storage_authority_sql.operations`` for the exact ``conversation.get`` and
compaction-archive read paths the running sidecar serves. WAL permits concurrent
readers, so this is safe against the live SQLite database. For native storage,
explicit application API reads retain the service authorization boundary. They
never open database files or start a service; their total deadline is 30 seconds,
with a byte-derived request allowance, 8 MiB per response and 32 MiB total.
Read-only: never writes.

Exit codes: 0 = found, 2 = not found/invalid usage, 1 = database/IO error,
3 = partial evidence (budget stop, a live final Turn, or a best-effort view of
a conversation whose writes never paused for one verified revision). With
``--json`` stdout carries exactly one JSON document for exits 0, 2 and 3; exit 1
and usage errors leave stdout empty and report on stderr.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os

import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runtime_path_policy import resolve_runtime_base

# Compatibility seam for direct log-scan tests; main passes its resolved root.
LOG_DIR = REPO_ROOT / 'logs'

#: Stores probed for traces of the ID, ordered authority-first. Each entry is
#: (table, candidate id columns) — columns that don't exist are skipped, so
#: the probe survives schema drift.
PROBES = (
    ('storage_conversations', ('id',)),
    ('storage_conversation_turns', ('conversation_id',)),
    ('storage_conversation_trash', ('conversation_id',)),
    ('storage_conversation_trash_turns', ('conversation_id',)),
    ('storage_compaction_archives', ('conversation_id',)),
    ('storage_turn_search', ('conversation_id',)),
    ('storage_turn_tombstones', ('conversation_id',)),
    ('storage_generation_attempts', ('conversation_id',)),
    ('storage_attempt_events', ('conversation_id',)),
    ('storage_conversation_sync_heads', ('conversation_id',)),
    ('storage_conversation_changes', ('conversation_id',)),
    ('storage_queue_items', ('conv_id',)),
    ('storage_timers', ('conv_id',)),
    ('chat_artifacts', ('conv_id',)),
    ('swarm_sessions', ('conv_id',)),
)

TAIL_MESSAGES = 20
HEAD_MESSAGES = 3
MSG_TEXT_CAP = 600
LOG_TAIL_BYTES = 4 * 1024 * 1024
LOG_LINE_CAP = 50
#: Many log call sites print ``conversation_id[:8]`` instead of the full ID.
LOG_SHORT_ID_CHARS = 8
ARCHIVE_TAIL = 20
ARCHIVE_SUMMARY_CAP = 4_000
#: tool_use ``result.status`` values that mean the call never ran to success.
TOOL_ANOMALY_STATUSES = frozenset(
    {'rejected', 'error', 'errored', 'failed', 'skipped', 'cancelled',
     'canceled', 'timeout', 'aborted', 'partial_failure'})
ANOMALY_CAP = 25
ANOMALY_TEXT_CAP = 320
# Per-round inspection limits, unchanged by --full. Only result carriers are
# traversed; arguments, snippets, arbitrary data and artifact references are not.
ANOMALY_JSON_CHAR_CAP = 65_536
ANOMALY_JSON_TOTAL_CHAR_CAP = 256 * 1024
ANOMALY_NODE_CAP = 256
ANOMALY_DEPTH_CAP = 12
ANOMALY_RESULT_FIELDS = ('result', 'results', 'content', 'output', 'toolContent',
                         'program', 'childResults', 'backgroundCommand')
THINKING_CAP = 400
RESULT_CAP = 240
ARGS_CAP = 240


def _conversation_id(value: str) -> str:
    """Reject copy/paste mistakes before issuing a broad store probe."""
    normalized = value.strip()
    if not normalized:
        raise argparse.ArgumentTypeError('conversation ID must not be empty')
    if len(normalized) > 256:
        raise argparse.ArgumentTypeError(
            'conversation ID must be at most 256 characters')
    if any(character.isspace() or ord(character) < 32
           for character in normalized):
        raise argparse.ArgumentTypeError(
            'conversation ID must not contain whitespace or control characters')
    return normalized


def _log_line_count(value: str) -> int:
    try:
        count = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            'log line count must be an integer from 1 to 1000') from exc
    if not 1 <= count <= 1000:
        raise argparse.ArgumentTypeError(
            'log line count must be an integer from 1 to 1000')
    return count


def _log_match_pattern(value: str) -> re.Pattern:
    try:
        return re.compile(value)
    except re.error as exc:
        raise argparse.ArgumentTypeError(f'invalid regular expression: {exc}') from exc


def resolve_readonly_sqlite_authority(*args, **kwargs):
    from lib.storage_authority_sql.offline import resolve_readonly_sqlite_authority as resolve
    return resolve(*args, **kwargs)


def _open_ro(db_path: Path) -> sqlite3.Connection:
    from lib.storage_authority_sql.offline import open_readonly_sqlite_authority
    return open_readonly_sqlite_authority(db_path)


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchall()
    if not rows:
        return set()
    return {row[1] for row in connection.execute(
        f'PRAGMA table_info({table})')}


def _probe_locations(connection: sqlite3.Connection, conv_id: str) -> list:
    """Count rows tied to ``conv_id`` in every known store."""
    report = []
    for table, candidates in PROBES:
        columns = _table_columns(connection, table)
        if not columns:
            report.append((table, None, 'table absent'))
            continue
        hits = []
        for column in candidates:
            if column not in columns:
                continue
            count = connection.execute(
                f'SELECT count(*) FROM {table} WHERE {column} = ?',
                (conv_id,)).fetchone()[0]
            if count:
                hits.append(f'{column}: {count} row(s)')
        report.append((table, bool(hits), '; '.join(hits) or 'no rows'))
    return report


def _load_sidecar_document(connection: sqlite3.Connection, conv_id: str,
                           user_id: int | None) -> dict | None:
    """Read via the sidecar's own ``conversation.get`` operation.

    Turn-native conversations keep the transcript in
    ``storage_conversation_turns`` and leave ``messages_json`` empty;
    ``derive_messages`` makes the sidecar project the turns into the legacy
    message shape — identical to what the server renders.
    """
    if not _table_columns(connection, 'storage_conversations'):
        return None

    from lib.storage_authority_sql import operations as ops
    from lib.storage_authority_sql.adapters.sqlite import SQLiteSession

    session = SQLiteSession(connection)
    if user_id is None:
        row = session.fetch_one(
            'SELECT user_id FROM storage_conversations WHERE id = ?',
            (conv_id,))
        if row is None:
            return None
        user_id = int(row['user_id'])
    return ops._conversation_get(session, {
        'conv_id': conv_id,
        'user_id': user_id,
        'derive_messages': True,
    })


def _load_compaction_archives(
    connection: sqlite3.Connection,
    conv_id: str,
    user_id: int,
) -> list[dict]:
    """Read archive summaries/receipts through their semantic operations."""
    if not _table_columns(connection, 'storage_compaction_archives'):
        return []

    from lib.storage_authority_sql import operations as ops
    from lib.storage_authority_sql.adapters.sqlite import SQLiteSession

    session = SQLiteSession(connection)
    listed = ops._archive_list(session, {
        'conversation_id': conv_id,
        'user_id': user_id,
        'limit': 1000,
    })
    archives = []
    for metadata in listed.get('archives') or []:
        loaded = ops._archive_get(session, {
            'conversation_id': conv_id,
            'user_id': user_id,
            'archive_id': metadata.get('id'),
            'include_messages': False,
        })
        archive = (loaded or {}).get('archive')
        if isinstance(archive, dict):
            archives.append(archive)
    return archives


def _format_ts(value) -> str:
    if value is None:
        return '-'
    if isinstance(value, (int, float)):
        # Sidecar stores epoch milliseconds.
        seconds = value / 1000.0 if value > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds).strftime('%Y-%m-%d %H:%M:%S')
        except (OSError, OverflowError, ValueError):
            return f'{value} (unrepresentable timestamp)'
    return str(value)


def _message_text(message) -> str:
    if not isinstance(message, dict):
        return str(message)
    content = message.get('content')
    if isinstance(content, str) and content:
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                text = part.get('text') or part.get('content') or ''
                if text:
                    parts.append(str(text))
            elif part:
                parts.append(str(part))
        if parts:
            return '\n'.join(parts)
    # Turn-projection messages (sidecar turns v2): assistant text lives in
    # ``segments`` — dicts with ``text`` (type text/thinking) or ``_round``
    # (a tool round, rendered as a marker only).
    segments = message.get('segments')
    if isinstance(segments, list):
        parts = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            text = segment.get('text')
            if text:
                prefix = '[thinking] ' if segment.get('type') == 'thinking' \
                    else ''
                parts.append(prefix + str(text))
            elif segment.get('_round'):
                query = (segment['_round'] or {}).get('query') if \
                    isinstance(segment['_round'], dict) else None
                parts.append(f'[tool round: {query or "?"}]')
        if parts:
            return '\n'.join(parts)
    for key in ('text', 'thinking', 'translated_content'):
        if message.get(key):
            return str(message[key])
    return ''


def _message_markers(message) -> str:
    if not isinstance(message, dict):
        return ''
    markers = []
    if message.get('thinking'):
        markers.append('thinking')
    tool_keys = ('tool_calls', 'toolRounds', 'tool_rounds', 'toolCalls')
    for key in tool_keys:
        value = message.get(key)
        if value:
            count = len(value) if isinstance(value, (list, dict)) else '?'
            markers.append(f'{key}×{count}')
    if message.get('branches'):
        markers.append(f"branches×{len(message['branches'])}")
    turn_status = str(message.get('_turnStatus') or '')
    settlement = message.get('_turnSettlement')
    settlement = settlement if isinstance(settlement, dict) else {}
    if turn_status:
        markers.append(f'turn={turn_status}')
    cause = str(settlement.get('cause') or '')
    if cause:
        markers.append(f'cause={cause}')
    stream_state = str(settlement.get('streamState') or '')
    if stream_state:
        markers.append(f'stream={stream_state}')
    return f"  [{' '.join(markers)}]" if markers else ''


def _short(value, cap: int) -> str:
    """Collapse whitespace and cap a value for one-line anomaly rendering."""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        text = '' if value is None else str(value)
    text = ' '.join(text.split())
    return text if len(text) <= cap else text[:cap] + '…'


def _tool_input_summary(raw) -> str:
    """Render tool args readably: parse the JSON string, else show as-is."""
    if isinstance(raw, str):
        if len(raw) > ANOMALY_JSON_CHAR_CAP:
            return _short(raw[:ANOMALY_JSON_CHAR_CAP], ARGS_CAP)
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError):
            pass
    return _short(raw, ARGS_CAP)


def _anomaly_json(value: str, budget: dict):
    """Decode only complete, size-bounded JSON objects/arrays in result fields."""
    size = len(value)
    if size > ANOMALY_JSON_CHAR_CAP or size > budget['chars']:
        return None
    text = value.strip()
    if not text or text[0] not in '{[':
        return None
    budget['chars'] -= size
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def _result_error_text(result, budget=None, depth=0) -> str:
    """Bounded error detail extraction, never evidence of failure by itself."""
    if budget is None:
        budget = {'nodes': ANOMALY_NODE_CAP, 'chars': ANOMALY_JSON_TOTAL_CHAR_CAP}
    if depth > ANOMALY_DEPTH_CAP or budget['nodes'] <= 0:
        return ''
    budget['nodes'] -= 1
    if isinstance(result, str):
        parsed = _anomaly_json(result, budget)
        if parsed is not None:
            return _result_error_text(parsed, budget, depth + 1)
        return _short(result[:ANOMALY_JSON_CHAR_CAP], ANOMALY_TEXT_CAP)
    if isinstance(result, list):
        for item in result[:budget['nodes']]:
            text = _result_error_text(item, budget, depth + 1)
            if text:
                return text
        return ''
    if not isinstance(result, dict):
        return ''
    contract = result.get('_contractError')
    if isinstance(contract, dict) and contract:
        text = _result_error_text(contract, budget, depth + 1)
        for key in ('path', 'nextAction'):
            if isinstance(contract.get(key), str):
                text += f' {key}: {contract[key][:ANOMALY_TEXT_CAP]}'
        return _short(text, ANOMALY_TEXT_CAP)
    for key in ('_rejected', 'error', 'errors', 'summary', 'message', 'reason',
                'code', 'result', 'results', 'content', 'output', 'toolContent'):
        value = result.get(key)
        if value:
            text = _result_error_text(value, budget, depth + 1)
            if text:
                return text
    return ''


def _tool_round_anomalies(entry: dict) -> list[dict]:
    """Inspect explicit result envelopes, not just the display round verdict.

    A done display carrier can contain a failed tool_use result or gateway
    child output. Inner status/contract fields are evidence; prose containing
    error words and unknown outcomes are not. Per-call identities prevent
    duplicate display/model surfaces from double-counting the same failure.
    """
    budget = {'nodes': ANOMALY_NODE_CAP, 'chars': ANOMALY_JSON_TOTAL_CHAR_CAP}
    context = {'tool': str(entry.get('toolName') or entry.get('query') or '?'),
               'callId': entry.get('toolCallId'), 'occurrence': 0,
               'args': entry.get('toolArgs') if entry.get('toolArgs') is not None
                       else entry.get('input')}

    def visit(value, owner, depth=0, result_list=False, command_meta=False):
        if depth > ANOMALY_DEPTH_CAP or budget['nodes'] <= 0:
            return []
        budget['nodes'] -= 1
        if isinstance(value, str):
            parsed = _anomaly_json(value, budget)
            return visit(parsed, owner, depth + 1) if parsed is not None else []
        if isinstance(value, list):
            records = []
            for index in range(min(len(value), budget['nodes'])):
                if budget['nodes'] <= 0:
                    break
                child_owner = {**owner, 'occurrence': index} if result_list else owner
                records.extend(visit(value[index], child_owner, depth + 1,
                                     command_meta=command_meta))
            return records
        if not isinstance(value, dict):
            return []
        tool = str(value.get('toolName') or value.get('name') or owner['tool'])
        call_id = (value.get('toolCallId') or value.get('call_id')
                   or (owner['callId'] if tool == owner['tool'] else None))
        args = value.get('toolArgs', value.get('arguments', value.get('input')))
        if args is None and tool == owner['tool'] and call_id == owner['callId']:
            args = owner['args']
        current = {'tool': tool, 'callId': call_id, 'args': args,
                   'occurrence': owner['occurrence']}
        command = _background_command_anomaly(value, entry.get('roundNum'))
        if command is not None:
            command.update(taskId=str(entry.get('taskId') or '')[:8], toolCallId=call_id)
            return [command]
        status = str(value.get('status') or '').strip().lower()
        marker = bool(value.get('_contractError') or value.get('_rejected'))
        children = []
        command_failure = (
            _run_command_result_failure(value)
            if command_meta and tool == 'run_command'
            and (not status or status in {'done', 'completed'} | TOOL_ANOMALY_STATUSES)
            else None)
        if command_failure is not None:
            command_status, command_error = command_failure
            if status not in TOOL_ANOMALY_STATUSES:
                status = command_status
        for key in ANOMALY_RESULT_FIELDS:
            if key in value:
                children.extend(visit(value[key], current, depth + 1,
                                      result_list=key in {'results', 'childResults'},
                                      command_meta=(key == 'results' and tool == 'run_command'
                                                    and status in {'done', 'completed'})))
        if value.get('type') == 'text' and isinstance(value.get('text'), str):
            children.extend(visit(value['text'], current, depth + 1))
        if status not in TOOL_ANOMALY_STATUSES and not marker:
            return children
        same_call = [child for child in children
                     if child['tool'] == tool and child.get('toolCallId') == call_id]
        # Child failures are more specific than a transport/aggregate verdict.
        # An explicit rejection still wins over its nested error detail.
        if children and not marker and status != 'rejected':
            return children
        error = command_error if command_failure is not None else _result_error_text(value, budget)
        if not error and same_call:
            error = same_call[0]['error']
        args_text = _tool_input_summary(args)
        if tool == '?' and not args_text and not error and not marker:
            return children
        record = {
            'kind': 'tool', 'tool': tool,
            'status': status if status in TOOL_ANOMALY_STATUSES else 'rejected',
            'rejection': marker or status == 'rejected',
            'round': entry.get('roundNum') if entry.get('roundNum') is not None
                     else entry.get('llmRound'),
            'args': args_text, 'error': error,
            'taskId': str(entry.get('taskId') or '')[:8], 'toolCallId': call_id,
            '_occurrence': owner['occurrence'],
        }
        return [record] + [child for child in children if child not in same_call]

    records = {}
    for record in visit(entry, context):
        key = (record['tool'], str(record.get('toolCallId') or ''),
               record.pop('_occurrence', 0), record['status'], record.get('commandId'))
        previous = records.get(key)
        if previous is None:
            records[key] = record
        else:
            # The detailed model surface can accompany a status-only display.
            for field in ('args', 'error'):
                if len(record[field]) > len(previous[field]):
                    previous[field] = record[field]
    return list(records.values())


def _tool_round_anomaly(entry: dict) -> dict | None:
    """Single-record convenience seam; document inspection retains all children."""
    records = _tool_round_anomalies(entry)
    return records[0] if records else None


def _run_command_result_failure(result: dict) -> tuple[str, str] | None:
    """Read producer frontend metadata, never exit-like fields in command stdout.

    Unlike background completion receipts, run_command's frontend producer
    stamps numeric exitCode as a decimal string. Only direct results of a done
    command round enter this seam; no recursive body/data field does.
    """
    exit_code = result.get('exitCode')
    if isinstance(exit_code, str) and re.fullmatch(r'-?[0-9]{1,10}', exit_code):
        exit_code = int(exit_code)
    timed_out = result.get('timedOut') is True
    interrupted = result.get('interrupted') is True
    if not (type(exit_code) is int and exit_code != 0) and not (timed_out or interrupted):
        return None
    status = 'timeout' if timed_out else 'aborted' if interrupted else 'error'
    exit_text = f'exitCode={exit_code}' if type(exit_code) is int else 'exitCode=unknown'
    output = result.get('output')
    output = output[:ANOMALY_JSON_CHAR_CAP] if isinstance(output, str) else ''
    return status, _short(f'{exit_text} {output}', ANOMALY_TEXT_CAP)


def _background_command_anomaly(completion, round_num=None) -> dict | None:
    """A completed producer receipt is neutral without typed failure evidence."""
    if (not isinstance(completion, dict) or completion.get('status') != 'completed'
            or not isinstance(completion.get('commandId'), str)
            or not completion['commandId']):
        return None
    exit_code = completion.get('exitCode')
    timed_out = completion.get('timedOut') is True
    interrupted = completion.get('interrupted') is True
    if not (type(exit_code) is int and exit_code != 0) and not (timed_out or interrupted):
        return None
    status = 'timeout' if timed_out else 'aborted' if interrupted else 'error'
    exit_text = f'exitCode={exit_code}' if type(exit_code) is int else 'exitCode=unknown'
    return {
        'kind': 'command', 'tool': 'run_command', 'status': status, 'rejection': False,
        'round': round_num,
        'args': _short(str(completion.get('command') or '')[:ANOMALY_JSON_CHAR_CAP], ARGS_CAP),
        'error': _short(exit_text + ' ' + str(completion.get('resultPreview') or '')
                       [:ANOMALY_JSON_CHAR_CAP], ANOMALY_TEXT_CAP),
        'taskId': '', 'commandId': completion['commandId'][:128], 'exitCode': exit_code,
    }


def _background_command_anomalies(message: dict) -> list[dict]:
    """Read only the named background-completion sidecars, with a shared cap."""
    records = []
    record = _background_command_anomaly(message.get('backgroundCommand'))
    if record is not None:
        records.append(record)
    injections = message.get('_bgCommandInjects')
    if isinstance(injections, list):
        remaining = ANOMALY_NODE_CAP
        for injection in injections[:ANOMALY_NODE_CAP]:
            if remaining <= 0:
                break
            remaining -= 1
            if not isinstance(injection, dict) or not isinstance(injection.get('previews'), list):
                continue
            for preview in injection['previews'][:remaining]:
                remaining -= 1
                if isinstance(preview, dict):
                    record = _background_command_anomaly(
                        preview.get('backgroundCommand'), injection.get('round'))
                    if record is not None:
                        records.append(record)
    for record in records:
        record['turnId'] = str(message.get('_turnId') or '')[:8]
    return records


def _turn_anomaly(message: dict) -> dict | None:
    """Report explicit failed settlement even without an offline attempt table."""
    settlement = message.get('_turnSettlement')
    settlement = settlement if isinstance(settlement, dict) else {}
    if (message.get('_turnStatus') != 'failed'
            and settlement.get('outcome') != 'failed'):
        return None
    detail = _result_error_text(settlement)
    error = settlement.get('error')
    typed = error.get('detail') if isinstance(error, dict) else None
    if isinstance(typed, str) and typed.strip() and typed.strip() not in detail:
        # The envelope message names only the category ("HTTP 400"); its
        # typed detail is the cause, so it leads within the bounded text.
        detail = f'{_short(typed.strip(), ANOMALY_TEXT_CAP)} | {detail}'
    cause = str(settlement.get('cause') or '')[:ANOMALY_TEXT_CAP]
    stream = str(settlement.get('streamState') or '')[:ANOMALY_TEXT_CAP]
    return {
        'kind': 'turn', 'tool': cause or 'generation', 'status': 'failed',
        'rejection': False, 'round': None, 'args': '',
        'error': _short(f'cause={cause or "?"} stream={stream or "?"}: {detail}',
                        ANOMALY_TEXT_CAP),
        'taskId': str(message.get('_taskId') or '')[:8],
        'turnId': str(message.get('_turnId') or '')[:8],
        'attemptId': str(message.get('_attemptId') or '')[:8],
    }


def _load_anomalies(connection: sqlite3.Connection | None, conv_id: str,
                    document: dict | None) -> list[dict]:
    """Collect debugging red flags from the projected transcript + attempts.

    Projected tool rounds include display verdicts and structured child result
    outcomes; failed Turn settlement remains available on native API reads.
    ``storage_generation_attempts.error_json`` adds separate attempt evidence
    when a direct SQLite read is available (offline mode).
    """
    anomalies = []
    for message in (document or {}).get('messages') or []:
        if not isinstance(message, dict):
            continue
        record = _turn_anomaly(message)
        if record is not None:
            anomalies.append(record)
        anomalies.extend(_background_command_anomalies(message))
        rounds = message.get('toolRounds')
        if not isinstance(rounds, list):
            continue
        for entry in rounds:
            if isinstance(entry, dict):
                anomalies.extend(_tool_round_anomalies(entry))
    if connection is not None and _table_columns(
            connection, 'storage_generation_attempts'):
        try:
            rows = connection.execute(
                "SELECT attempt_id, operation, status, error_json "
                "FROM storage_generation_attempts WHERE conversation_id = ? "
                "AND error_json IS NOT NULL",
                (conv_id,))
        except sqlite3.Error:
            rows = []
        for attempt_id, operation, status, error_json in rows:
            if isinstance(error_json, (bytes, bytearray, memoryview)):
                error_json = bytes(error_json).decode('utf-8', 'replace')
            text = str(error_json or '').strip()
            if not text or text in ('{}', 'null', '[]', '""'):
                continue
            anomalies.append({
                'kind': 'attempt',
                'tool': operation,
                'status': status,
                'rejection': False,
                'round': None,
                'args': '',
                'error': _short(text, ANOMALY_TEXT_CAP),
                'taskId': str(attempt_id or '')[:8],
            })
    # The same command completion can be shown by poll and an injection receipt.
    seen_commands = set()
    unique = []
    for record in anomalies:
        if record.get('commandId'):
            key = (record['commandId'], record['status'], record.get('exitCode'))
            if key in seen_commands:
                continue
            seen_commands.add(key)
        unique.append(record)
    anomalies = unique
    # Rejections sort ahead of ordinary errors, then chronologically by round.
    anomalies.sort(key=lambda item: (
        0 if item.get('rejection') else 1,
        item.get('round') if isinstance(item.get('round'), int) else 1 << 30,
    ))
    return anomalies


def _render_tool_rounds(message, *, full: bool) -> list[str]:
    """Render a message's toolRounds with args, per-result status, thinking.

    The default transcript collapses every tool round to a marker count;
    ``--tools`` keeps the debugging-relevant payload (which tool, what args,
    what error) that would otherwise require ``--raw`` spelunking.
    """
    rounds = message.get('toolRounds') if isinstance(message, dict) else None
    if not isinstance(rounds, list) or not rounds:
        return []
    lines = []
    for index, entry in enumerate(rounds, 1):
        if not isinstance(entry, dict):
            continue
        status = str(entry.get('status') or entry.get('_runtimeState') or '')
        results = entry.get('results')
        results = results if isinstance(results, list) else []
        names = [str(r.get('toolName') or r.get('title') or '?')
                 for r in results if isinstance(r, dict)]
        round_num = entry.get('roundNum') or index
        label = ', '.join(names) if names else str(entry.get('query') or '?')
        lines.append(f'  [tool round {round_num}: {label}'
                     f'{" — " + status if status else ""}]')
        thinking = entry.get('thinking')
        if thinking:
            cap = 32_768 if full else THINKING_CAP
            lines.append(f'    thinking: {_short(thinking, cap)}')
        for result in results:
            if not isinstance(result, dict):
                continue
            tool = str(result.get('toolName') or result.get('title') or '?')
            badge = result.get('badge') or result.get('status') or ''
            snippet = (result.get('snippet') or result.get('error')
                       or result.get('content') or '')
            line = f'    • {tool}'
            if badge:
                line += f' [{badge}]'
            if snippet:
                line += f': {_short(snippet, RESULT_CAP)}'
            lines.append(line)
    return lines


def _render_result_surfaces(document, *, tool_call_id=None, full=False, max_results=20):
    """Read the shared result observer; redact every diagnostic preview."""
    from lib.tools.result_surfaces import inspect_result_document
    from lib.logging_pkg.log_record_values import sanitize_value
    report = inspect_result_document(document, tool_call_id=tool_call_id, max_results=max_results,
                                     preview_chars=16384 if full else 4096)
    safe = sanitize_value(report, max_depth=12, max_items=256, max_string_chars=16384)
    lines = [f"result coverage: {report['coverage']}; "
             f"{len(report['records'])}/{report['totalRecords']} record(s)"]
    for record in safe['records']:
        identity = record.get('toolCallId') or record.get('agentId') or '?'
        lines.append(f"\n[turn {record['turnIndex']} · {record['toolName']} · {identity}]")
        for lane in ('model', 'frontend'):
            view = record[lane]
            lines.append(f"  {lane}: {view['availability']} · {view.get('source') or '?'}"
                         f" · {view.get('chars', 0)} chars"
                         + (' · diagnostic preview truncated' if view.get('truncated') else ''))
            if 'text' in view:
                lines.append(str(view['text']))
            if view.get('mediaOmitted'):
                lines.append('  inline media omitted from diagnostic; model payload was not rewritten')
            if lane == 'frontend':
                for field in ('metadata', 'arguments', 'roundSidecars'):
                    if field in view:
                        lines.append(f'  frontend {field}: ' + str(view[field]['text']))
        lines.append('  canonical: ' + json.dumps(record['canonical'], ensure_ascii=False))
        for finding in record['audit']['findings']:
            lines.append('  review: ' + str(finding) + ' — '
                         + str(report['rules'].get(finding, finding)))
        lines.append('  shape: ' + json.dumps(record['audit']['metrics'], ensure_ascii=False))
    if report['truncated']:
        lines.append('Result limit reached; use --tool-call-id to select an occurrence.')
    return lines


def _render_anomalies(anomalies: list[dict], *, full: bool) -> list[str]:
    """One line per anomaly: kind, tool, status, args, and the error text."""
    if not anomalies:
        return ['<no tool/attempt anomalies>']
    selected = anomalies if full else anomalies[:ANOMALY_CAP]
    lines = []
    for item in selected:
        round_part = (f" r{item['round']}" if item.get('round') is not None
                      else '')
        flag = ' ⚑ REJECTED' if item.get('rejection') else ''
        head = (f"[{item['kind']}] {item.get('tool') or '?'} "
                f"status={item.get('status') or '?'}{round_part}"
                + (f" task={item['taskId']}" if item.get('taskId') else '')
                + (f" turn={item['turnId']}" if item.get('turnId') else '')
                + (f" attempt={item['attemptId']}" if item.get('attemptId') else '')
                + (f" command={item['commandId']}" if item.get('commandId') else '')
                + flag)
        lines.append(head)
        if item.get('args'):
            lines.append(f"    args:  {item['args']}")
        if item.get('error'):
            lines.append(f"    error: {item['error']}")
    omitted = len(anomalies) - len(selected)
    if omitted:
        lines.append(f'… <{omitted} more anomaly(ies); use --full>')
    return lines


def _render_request_evidence(api, document: dict) -> list[str]:
    """Find the last four task-owned request archives without dumping bodies.

    Use the selected application's authenticated operations and existing shared
    read budget. An absent archive is an evidence gap, never proof of outage.
    """
    from urllib.parse import quote
    tasks = []
    for message in reversed(document.get('messages') or []):
        for entry in reversed(message.get('toolRounds') or []):
            reference = entry.get('inspectorRef') or {}
            task_id = entry.get('taskId') or reference.get('taskId')
            if isinstance(task_id, str) and task_id and task_id not in tasks:
                tasks.append(task_id)
                if len(tasks) == 4:
                    break
        if len(tasks) == 4:
            break
    lines = []
    for task_id in tasks:
        path = '/api/v4/tasks/' + quote(task_id, safe='')
        try:
            catalog = api.get(path + '/requests', missing_ok=True)
            requests = catalog.get('requests') if isinstance(catalog, dict) else None
            if not isinstance(requests, list) or not requests:
                lines.append(f'  task={task_id}: request catalog unavailable or expired')
                continue
            row = requests[-1]
            round_num = row.get('roundNum')
            if type(round_num) is not int or round_num < 0:
                raise DiagnosticAPIError('Invalid request evidence round number')
            turn = row.get('turn') or ''
            payload = api.get(path + f'/requests/{round_num}?turn='
                              + quote(str(turn), safe=''), missing_ok=True)
            archives = payload.get('rawArchives') if isinstance(payload, dict) else None
            if not isinstance(archives, list) or not archives:
                lines.append(f'  task={task_id} round={round_num}: no retained provider archive')
                continue
            for archive in archives[-4:]:
                if not isinstance(archive, dict) or archive.get('taskId') != task_id:
                    raise DiagnosticAPIError('Request archive task ownership mismatch')
                fields = {key: archive.get(key) for key in (
                    'archiveId', 'taskId', 'turnId', 'attemptId', 'roundNum',
                    'transportAttempt', 'integrity', 'truncationReason',
                    'requestAvailable', 'responseAvailable', 'responseBytes')}
                details = archive.get('details') or {}
                fields['traceId'] = details.get('traceId')
                fields['statusCode'] = details.get('statusCode')
                from lib.logging_pkg.log_record_values import sanitize_value
                lines.append('  ' + json.dumps(sanitize_value(fields), ensure_ascii=False))
        except DiagnosticAPIError as error:
            lines.append(f'  task={task_id}: evidence unavailable ({error})')
    return lines or ['  No task/request references in the selected transcript window.']


def _render_transcript(document: dict, *, full: bool, raw: bool,
                       tools: bool = False, head_tail: bool = False) -> list[str]:
    """Render the read messages; ``head_tail`` keeps the default head/tail view
    of an API read widened past it (``--context``/``--json`` read every turn)."""
    messages = document.get('messages') or []
    lines = []
    if raw:
        lines.append(json.dumps(messages, ensure_ascii=False, indent=2,
                                default=str))
        return lines
    total = len(messages)
    if not full and total > HEAD_MESSAGES + TAIL_MESSAGES:
        window = list(enumerate(messages[:HEAD_MESSAGES], 1))
        omitted = total - HEAD_MESSAGES - TAIL_MESSAGES
        tail = list(enumerate(messages[-TAIL_MESSAGES:],
                              total - TAIL_MESSAGES + 1))
    else:
        window = list(enumerate(messages, 1))
        omitted = 0
        tail = []

    def emit(number: int, message, label: str = '') -> None:
        role = message.get('role', '?') if isinstance(message, dict) else '?'
        text = _message_text(message).replace('\r', '')
        if not full and len(text) > MSG_TEXT_CAP:
            text = text[:MSG_TEXT_CAP] + f'… <+{len(text) - MSG_TEXT_CAP} chars>'
        lines.append(f'--- #{number} [{role}]{_message_markers(message)}{label}')
        lines.append(text if text else '<empty>')
        if tools:
            lines.extend(_render_tool_rounds(message, full=full))

    indices = document.get('diagnostic_message_indices')
    if isinstance(indices, list):
        previous = -1
        live = document.get('diagnostic_live_index')
        snapshot = document.get('diagnostic_revision')
        revisions = document.get('diagnostic_turn_revisions') or [snapshot] * len(indices)
        total = int(document.get('diagnostic_total_count', 0))
        read = set(indices)
        shown = (set(range(min(HEAD_MESSAGES, total)))
                 | set(range(max(0, total - TAIL_MESSAGES), total))) if head_tail else read

        def gap(start: int, end: int) -> None:
            hidden = len(read.intersection(range(start, end)))
            if end - start > hidden:
                lines.append(f'  … {end - start - hidden} unread message(s) …')
            if hidden:
                lines.append(f'… <{hidden} message(s) omitted; use --full>')

        for index, message, revision in zip(indices, messages, revisions):
            if index not in shown:
                continue
            if index > previous + 1:
                gap(previous + 1, index)
            if index == live:
                label = f' [live, as of revision {revision}]'
            else:
                label = f' [as of revision {revision}]' if revision != snapshot else ''
            emit(index + 1, message, label)
            previous = index
        remaining = total - previous - 1
        if live is not None and remaining > 0 and live == previous + remaining:
            if remaining > 1:
                gap(previous + 1, live)
            lines.append(f'--- #{live + 1} [live]')
            lines.append('<still being written; not readable through revision-fenced diagnostics>')
        elif remaining > 0:
            gap(previous + 1, total)
        return lines or ['<no messages>']

    for number, message in window:
        emit(number, message)
    if omitted:
        lines.append(f'… <{omitted} message(s) omitted; use --full>')
        for number, message in tail:
            emit(number, message)
    if not lines:
        lines.append('<no messages>')
    return lines


def _context_injections(document: dict) -> list[dict]:
    """Every ``system_note`` segment of the read transcript, in transcript order.

    A note whose exact text already appeared earlier names that first
    occurrence, so re-injected rule files and context blocks are visible.
    """
    messages = document.get('messages') or []
    indices = document.get('diagnostic_message_indices')
    numbers = ([index + 1 for index in indices] if isinstance(indices, list)
               else range(1, len(messages) + 1))
    first_seen: dict[str, dict] = {}
    records = []
    for number, message in zip(numbers, messages):
        segments = message.get('segments') if isinstance(message, dict) else None
        for segment in segments if isinstance(segments, list) else ():
            if not isinstance(segment, dict) or segment.get('type') != 'system_note':
                continue
            text = segment.get('text') if isinstance(segment.get('text'), str) else ''
            digest = hashlib.sha256(text.encode('utf-8', 'surrogatepass')).hexdigest()
            lines = text.splitlines()
            records.append({
                'messageNumber': number,
                'llmRound': segment.get('llmRound'),
                'reason': segment.get('reason'),
                'noteKind': segment.get('noteKind'),
                'nonBlankLines': sum(1 for line in lines if line.strip()),
                # Rule notes say ``Source: <path>`` or ``Source: <path> — <status>``.
                'sources': [line[len('Source: '):].split(' — ', 1)[0].strip()
                            for line in lines if line.startswith('Source: ')],
                'sha256': digest,
                'repeatOf': first_seen.get(digest),
            })
            first_seen.setdefault(digest, {'messageNumber': number,
                                           'llmRound': segment.get('llmRound')})
    return records


def _render_context_injections(records: list[dict]) -> list[str]:
    def round_label(value) -> str:
        return f'r{value}' if value is not None else 'r?'

    lines = []
    for record in records:
        line = (f"  #{record['messageNumber']} {round_label(record['llmRound'])} "
                f"{record['reason'] or record['noteKind'] or '?'}: "
                f"{record['nonBlankLines']} line(s)")
        if record['sources']:
            line += ' source: ' + ', '.join(record['sources'])
        repeat = record['repeatOf']
        if repeat:
            line += (f" (repeat of #{repeat['messageNumber']} "
                     f"{round_label(repeat['llmRound'])})")
        lines.append(line)
    distinct = len({record['sha256'] for record in records})
    lines.append(f'  {len(records)} note(s), {distinct} distinct text(s), '
                 f'{len(records) - distinct} repeat(s)')
    return lines


def _render_compaction_archives(
    archives: list[dict],
    *,
    full: bool,
) -> list[str]:
    """Render summary state receipts where objective drift is observable."""
    if not archives:
        return ['<no compaction archives>']
    selected = archives if full else archives[-ARCHIVE_TAIL:]
    lines = []
    omitted = len(archives) - len(selected)
    if omitted:
        lines.append(f'… <{omitted} older archive(s) omitted; use --full>')
    from lib.logging_pkg.log_record_values import sanitize_value
    for archive in selected:
        lines.append(
            '--- archive '
            f"{archive.get('id')}  trigger={archive.get('trigger')}  "
            f"round={archive.get('roundNum')}  status={archive.get('resultStatus')}  "
            f"tokens={archive.get('tokensBefore')}→{archive.get('tokensAfter')}  "
            f"created={_format_ts(archive.get('createdAt'))}"
        )
        summary = str(archive.get('summary') or '')
        if not full and len(summary) > ARCHIVE_SUMMARY_CAP:
            summary = (
                summary[:ARCHIVE_SUMMARY_CAP]
                + f'… <+{len(summary) - ARCHIVE_SUMMARY_CAP} chars>')
        lines.append('summary:')
        lines.append(summary or '<empty>')
        receipt = sanitize_value(
            archive.get('receipt') or {}, max_items=200,
            max_string_chars=ARCHIVE_SUMMARY_CAP if not full else 32_768)
        lines.append(
            'receipt: '
            + json.dumps(receipt, ensure_ascii=False, sort_keys=True,
                         default=str))
    return lines


def _epoch_seconds(value) -> float | None:
    """Metadata timestamp as epoch seconds, read like ``_format_ts``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value / 1000.0 if value > 1e11 else float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.strip()).timestamp()
        except ValueError:
            return None
    return None


def _conversation_window(metadata: dict) -> tuple[float, float] | None:
    created = _epoch_seconds(metadata.get('created_at'))
    updated = _epoch_seconds(metadata.get('updated_at'))
    if created is None:
        return None
    return created, max(created, updated if updated is not None else created)


def _rotated_logs(directory: Path, name: str,
                  window: tuple[float, float] | None) -> list[Path]:
    """Rotations of ``name`` written while the conversation was active.

    A rotation holds the lines written after its predecessor's last write up
    to its own last write. Selecting only those overlapping ``window`` bounds
    the scan to the size-rotated logs of the conversation's lifetime.
    """
    if window is None:
        return []
    created, updated = window
    rotations = []
    for path in directory.glob(f'{name}.*'):
        if not path.is_file() or path.name.endswith('.lock'):
            continue
        try:
            rotations.append((path.stat().st_mtime, path.name, path))
        except OSError:
            continue
    rotations.sort()
    selected, started = [], float('-inf')
    for written, _name, path in rotations:
        if written >= created and started <= updated:
            selected.append(path)
        started = written
    return selected


def _conversation_log_pattern(conv_id: str) -> str:
    """Regex for one conversation's log lines: its full ID or its short-ID token.

    The short prefix must stand alone; an alphanumeric run that continues it is
    another conversation sharing the prefix unless it is this full ID.
    """
    token = (rf'(?<![0-9A-Za-z]){re.escape(conv_id[:LOG_SHORT_ID_CHARS])}'
             r'(?![0-9A-Za-z])')
    if len(conv_id) <= LOG_SHORT_ID_CHARS:
        return token
    return f'{re.escape(conv_id)}|{token}'


def _scan_logs(conv_id: str, cap: int, log_dir: Path | None = None,
               window: tuple[float, float] | None = None,
               log_match: re.Pattern | None = None,
               ) -> list[tuple[str, list[str] | None]]:
    """Per scanned log file, its report header and last ``cap`` matching lines.

    ``log_match`` filters the conversation's lines before the cap. An
    unreadable file yields its error as the header and ``None`` lines.
    """
    from collections import deque

    from lib.logging_pkg.log_record_values import text_value

    directory = LOG_DIR if log_dir is None else log_dir
    prefix = conv_id[:LOG_SHORT_ID_CHARS]
    pattern = _conversation_log_pattern(conv_id)
    text_pattern = re.compile(pattern)
    byte_pattern = re.compile(pattern.encode('utf-8'))
    needle = prefix.encode('utf-8')
    sections = []
    for name in ('app.log', 'access.log'):
        for rotated in _rotated_logs(directory, name, window):
            kept = deque(maxlen=cap)
            try:
                with rotated.open('rb') as handle:
                    for raw in handle:
                        if needle in raw and byte_pattern.search(raw) and (
                                log_match is None or log_match.search(
                                    raw.decode('utf-8', errors='replace').rstrip('\r\n'))):
                            kept.append(raw)
            except OSError as exc:
                sections.append((f'{rotated.name}: unreadable: {exc}', None))
                continue
            sections.append((
                f'--- {rotated.name}: {len(kept)} line(s) '
                '(rotation overlapping the conversation lifetime)',
                [text_value(raw.decode('utf-8', errors='replace').rstrip('\r\n'),
                            max_chars=16_384)
                 for raw in kept]))
        path = directory / name
        if not path.is_file():
            continue
        try:
            with path.open('rb') as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - LOG_TAIL_BYTES))
                chunk = handle.read().decode('utf-8', errors='replace')
        except OSError as exc:
            sections.append((f'{name}: unreadable: {exc}', None))
            continue
        matches = [
            text_value(line, max_chars=16_384)
            for line in chunk.splitlines()
            if prefix in line and text_pattern.search(line)
            and (log_match is None or log_match.search(line))
        ]
        matches = matches[-cap:]
        sections.append((f'--- {name}: {len(matches)} line(s) '
                         f'(last {LOG_TAIL_BYTES // 1024 // 1024} MiB)', matches))
    return sections


def _validate_sqlite_discovery_configuration(
        environment: dict[str, str]) -> str | None:
    """Return an error when the deployment forbids opening local SQLite files.

    A migrated installation may retain a perfectly readable stale SQLite
    file. Its existence is not authority evidence when project configuration
    selects a different backend — and a configuration that cannot even be
    validated must fail closed too, never probe. Match application
    dotenv/environment order.
    """
    from runtime_guards import load_deployment_configuration
    try:
        backend = load_deployment_configuration(environment).storage_backend
    except (OSError, RuntimeError, ValueError) as exc:
        return f'cannot validate storage configuration: {exc}'
    if backend != 'sqlite':
        return (
            f'configuration selects {backend}, not SQLite; '
            'refusing to inspect a leftover SQLite file as the live authority. '
            'Pass --api-url or --db to override the auto-discovered application.')
    return None


def _resolve_storage_backend(environment: dict[str, str]) -> str | None:
    from runtime_guards import load_deployment_configuration
    try:
        return load_deployment_configuration(environment).storage_backend
    except (OSError, RuntimeError, ValueError):
        return None


def _discover_application_api_url(environment: dict[str, str]) -> str:
    """Derive the loopback origin of the configured application.

    The application binds ``PORT`` from the project .env (default 15000); the
    diagnostic stays on the loopback-only authority boundary that
    ``_DiagnosticAPI`` enforces.
    """
    raw = environment.get('PORT', '').strip()
    if raw:
        try:
            port = int(raw)
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            raise DiagnosticAPIError(
                f'configured PORT is not a valid TCP port: {raw!r}')
    else:
        port = 15000
    return f'http://127.0.0.1:{port}'


class DiagnosticAPIError(RuntimeError):
    """Bounded read failed without exposing a credential or response body."""


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """Flatten ``exc`` plus its causes/contexts into a de-duplicated list."""
    chain = [exc]
    seen = {id(exc)}
    current = exc
    while True:
        following = current.__cause__ or current.__context__
        if following is None or id(following) in seen or len(chain) >= 8:
            return chain
        chain.append(following)
        seen.add(id(following))
        current = following


class DiagnosticSnapshotConflict(DiagnosticAPIError):
    """A revision/source fence refused one Turn or chunk read (HTTP 409 database_conflict)."""


class DiagnosticBudgetError(DiagnosticAPIError):
    """Clean resource refusal; never a corrupt-data or consistency failure."""



class DiagnosticResponseTooLarge(DiagnosticBudgetError):
    """One response exceeded its bound, independently of the total allowance."""


def _api_total_mib(value: str) -> int:
    """An explicit diagnostic allowance, never an unbounded allocation."""
    try:
        size = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('must be an integer in 1..1024 MiB') from exc
    if not 1 <= size <= 1024:
        raise argparse.ArgumentTypeError('must be an integer in 1..1024 MiB')
    return size

class _DiagnosticAPI:
    """Explicit loopback application authority; no proxies, redirects or writes."""

    MAX_RESPONSE_BYTES = 8 * 1024 * 1024
    MAX_TOTAL_BYTES = 32 * 1024 * 1024
    # Chunk reads trade response size for request count. Keep this tied to the
    # unchanged aggregate byte ceiling, plus metadata/turn/archive overhead.
    MAX_REQUESTS = MAX_TOTAL_BYTES // (64 * 1024) + 128
    DEADLINE_SECONDS = 30.0

    def __init__(self, url: str, token: str | None):
        import ipaddress
        from urllib.parse import urlsplit
        import time
        try:
            parsed = urlsplit(url)
            host = parsed.hostname or ''
            address = ipaddress.ip_address(host)
            port = parsed.port if parsed.port is not None else (443 if parsed.scheme == 'https' else 80)
            valid = (parsed.scheme in {'http', 'https'} and address.is_loopback
                     and parsed.username is None and parsed.password is None
                     and not parsed.query and not parsed.fragment
                     and parsed.path in {'', '/'} and 1 <= port <= 65535)
        except ValueError:
            valid = False
        if not valid:
            raise DiagnosticAPIError('--api-url must be an HTTP(S) loopback IP origin without credentials or a path')
        if token is not None and (not token or len(token) > 8192
                or any(ord(c) < 33 or ord(c) > 126 for c in token)):
            raise DiagnosticAPIError('API token must contain 1..8192 printable ASCII characters without spaces')
        self.host, self.port, self.scheme = host, port, parsed.scheme
        self.origin = f'{parsed.scheme}://{parsed.netloc}'
        self.token = token
        self.deadline = time.monotonic() + self.DEADLINE_SECONDS
        self.requests = 0
        self.bytes_read = 0
        self.reserved_bytes = 0
        self.reserved_requests = 0

    @property
    def remaining_bytes(self) -> int:
        return max(0, self.MAX_TOTAL_BYTES - self.bytes_read - self.reserved_bytes)

    def get(self, path: str, *, missing_ok: bool = False,
            snapshot_read: bool = False) -> dict | None:
        import http.client
        import socket
        import threading
        import time
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or self.requests >= self.MAX_REQUESTS - self.reserved_requests:
            raise DiagnosticBudgetError('API diagnostic exceeded its total time/request budget')
        # Keep one sentinel byte within this read's allowance, never in the
        # protected final-header reserve.
        budget = min(self.MAX_RESPONSE_BYTES, self.remaining_bytes - 1)
        if budget < 1:
            raise DiagnosticBudgetError('API diagnostic exceeded its total byte budget')
        self.requests += 1
        connection_type = (http.client.HTTPSConnection if self.scheme == 'https'
                           else http.client.HTTPConnection)
        connection = connection_type(self.host, self.port, timeout=remaining)
        timer = None
        try:
            # Numeric loopback only: no unbounded DNS or proxy discovery.
            connection.connect()
            sock = connection.sock
            def expire():
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise DiagnosticBudgetError('API diagnostic exceeded its total time budget')
            timer = threading.Timer(remaining, expire)
            timer.daemon = True
            timer.start()
            headers = {'Accept': 'application/json', 'Accept-Encoding': 'identity'}
            if self.token is not None:
                headers['Authorization'] = 'Bearer ' + self.token
            connection.request('GET', path, headers=headers)
            response = connection.getresponse()
            missing_candidate = response.status == 404 and missing_ok

            conflict_candidate = response.status == 409 and snapshot_read
            unavailable = ('Diagnostic endpoint unavailable or incompatible (HTTP 404); '
                           'update the running application and storage service before retrying. '
                           'This does not establish that the conversation is missing.')
            if response.status != 200 and not (missing_candidate or conflict_candidate):
                if 500 <= response.status <= 599:
                    raise DiagnosticAPIError(
                        f'API read failed with HTTP {response.status}: a server-side application '
                        'or storage error; see the application error log (logs/error.log)')
                raise DiagnosticAPIError(f'API refused read (HTTP {response.status}); verify endpoint and conversation-scoped authorization')
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise DiagnosticAPIError('API returned unsupported compressed content')
            media_type = response.getheader('Content-Type', '').split(';', 1)[0].strip().lower()
            expected_type = ('application/problem+json' if missing_candidate or conflict_candidate
                             else 'application/json')
            if media_type != expected_type:
                raise DiagnosticAPIError(unavailable if missing_candidate
                                         else 'API returned a non-JSON response')
            declared = response.getheader('Content-Length')
            if declared is not None and not declared.isdecimal():
                raise DiagnosticAPIError('API returned an invalid response length')
            if declared is not None and int(declared) > budget:
                if int(declared) > self.remaining_bytes - 1:
                    raise DiagnosticBudgetError('API diagnostic exceeded its total byte budget; '
                                                'use --api-max-total-mib for a larger allowance')
                raise DiagnosticResponseTooLarge('API response exceeds its byte budget')
            body = bytearray()
            while True:
                chunk = response.read1(min(65536, budget - len(body) + 1))
                if not chunk:
                    break
                body.extend(chunk)
                self.bytes_read += len(chunk)
                if len(body) > budget:
                    if self.remaining_bytes <= 0:
                        raise DiagnosticBudgetError('API diagnostic exceeded its total byte budget; '
                                                    'use --api-max-total-mib for a larger allowance')
                    raise DiagnosticResponseTooLarge('API response exceeds its byte budget')
            if time.monotonic() >= self.deadline:
                raise DiagnosticBudgetError('API diagnostic exceeded its total time budget')
            if declared is not None and len(body) != int(declared):
                raise DiagnosticAPIError('API response ended before its declared length')
            envelope = json.loads(body)

            if conflict_candidate:
                # v4 _db_safe exposes the storage code as RFC7807 detail.
                # Arbitrary conflicts remain terminal; never display raw bodies.
                if (isinstance(envelope, dict) and envelope.get('status') == 409
                        and envelope.get('code') == 'conflict'
                        and envelope.get('detail') == 'database_conflict'):
                    raise DiagnosticSnapshotConflict(
                        'Conversation changed during diagnostic reads (HTTP 409 database_conflict)')
                raise DiagnosticAPIError('API refused read (HTTP 409); unrecognized diagnostic conflict')
            if missing_candidate:
                if (isinstance(envelope, dict) and envelope.get('status') == 404
                        and envelope.get('code') == 'diagnostic_conversation_not_found'):
                    return None
                raise DiagnosticAPIError(unavailable)
            if not isinstance(envelope, dict) or not isinstance(envelope.get('data'), dict):
                raise DiagnosticAPIError('API returned an invalid v4 success envelope')
            return envelope['data']
        except (OSError, ValueError, RecursionError, http.client.HTTPException) as exc:
            detail = ': '.join(
                f'{link.__class__.__name__}: {link}'
                for link in _exception_chain(exc))
            raise DiagnosticAPIError(
                'API transport/JSON read failed; verify the running application and authorization: '
                + detail) from exc
        finally:
            if timer is not None:
                timer.cancel()
                timer.join()
            connection.close()


def _load_api_archives(api: _DiagnosticAPI, path: str, *, full: bool) -> tuple[list[dict], int]:
    from urllib.parse import quote
    listed = api.get(path + '/compactions')
    entries = listed.get('compactions')
    if not isinstance(entries, list) or len(entries) > 1000:
        raise DiagnosticAPIError('API returned invalid archive bounds')
    archives = []
    for entry in entries if full else entries[-ARCHIVE_TAIL:]:
        identity = entry.get('id') if isinstance(entry, dict) else None
        if not isinstance(identity, str) or not identity or len(identity) > 256 or '/' in identity:
            raise DiagnosticAPIError('API returned invalid archive identity')
        loaded = api.get(path + '/compactions/' + quote(identity, safe='') + '?includeMessages=false')
        archive = loaded.get('archive')
        if not isinstance(archive, dict) or archive.get('id') != identity:
            raise DiagnosticAPIError('API archive identity differs from its catalog')
        archives.append(archive)
    return archives, len(entries)


def _diagnostic_header(page: dict, conv_id: str) -> tuple[dict, int, int]:
    document = page.get('document')
    metadata = document.get('metadata') if isinstance(document, dict) else None
    revision, total = page.get('revision'), page.get('totalCount')
    if (not isinstance(metadata, dict) or metadata.get('id') != conv_id
            or type(revision) is not int or revision < 0
            or type(total) is not int or total < 0
            or metadata.get('rev') != revision
            or document.get('messages') not in (None, [])
            or type(page.get('turnNative')) is not bool):
        raise DiagnosticAPIError('API returned invalid diagnostic header')
    if not page['turnNative']:
        raise DiagnosticAPIError('Legacy messages require a migrated bounded diagnostic source')
    return metadata, revision, total


def _validate_turn_descriptor(page: dict, *, conv_id: str, revision: int, total: int,
                              index: int) -> tuple[int, str]:
    """Return ``(source bytes, source token)`` of one fenced Turn descriptor."""
    from lib.storage_authority_sql.turn_projection_head import PROJECTION_HEAD_MAX_PATCHES
    turn, source, patches = page.get('turn'), page.get('source'), page.get('patches')
    if (page.get('revision') != revision or page.get('index') != index
            or page.get('totalCount') != total or not isinstance(turn, dict)
            or turn.get('conversationId') != conv_id or turn.get('laneId') != 'main'
            or not isinstance(turn.get('turnId'), str) or not turn['turnId']
            or type(turn.get('ordinal')) is not int or turn['ordinal'] < 0
            or type(turn.get('projectionRevision')) is not int or turn['projectionRevision'] < 0
            or not isinstance(source, dict) or source.get('format') not in {'projection', 'turn'}
            or type(source.get('totalBytes')) is not int or source['totalBytes'] < 2
            or not isinstance(patches, list) or len(patches) > PROJECTION_HEAD_MAX_PATCHES):
        raise DiagnosticAPIError('API returned invalid diagnostic turn identity or bounds')
    token = source.get('token')
    if not isinstance(token, str) or not re.fullmatch(r'[0-9a-f]{64}', token):
        raise DiagnosticAPIError('API returned missing or invalid diagnostic source token')
    return source['totalBytes'], token


def _chunk_data(page: dict, *, revision: int, index: int, size: int, offset: int,
                limit: int, token: str) -> bytes:
    """Validate one contiguous source range served at ``revision`` under ``token``."""
    import base64
    import binascii
    try:
        data = base64.b64decode(page.get('data', ''), validate=True)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise DiagnosticAPIError('API returned invalid diagnostic chunk encoding') from exc
    end = offset + len(data)
    if (page.get('revision') != revision or page.get('index') != index
            or page.get('totalBytes') != size or page.get('offset') != offset
            or page.get('sourceToken') != token or len(data) != limit
            or page.get('nextOffset') != (end if end < size else None)):
        raise DiagnosticAPIError('API returned a non-contiguous diagnostic chunk')
    return data


def _decode_api_turn(page: dict, raw: bytearray) -> tuple[dict, int]:
    """Decode joined source bytes and replay the patch head with the authority codecs."""
    from lib.storage_authority_sql.projection_codec import decode_projection_from_storage
    from lib.storage_authority_sql.operations_pkg._conversations import _turn_to_legacy_message
    from lib.turn_projection_patch import apply_projection_patch
    from lib.conversations.diagnostic import terminalize_restored_projection
    turn, source, patches = page['turn'], page['source'], page['patches']
    try:
        encoded = json.loads(raw)
        # Release the encoded base before decoding; the caller's buffer empties too.
        raw.clear()
        if source['format'] == 'turn':
            if (not isinstance(encoded, dict)
                    or any(encoded.get(field) != turn.get(field) for field in (
                        'turnId', 'conversationId', 'laneId', 'projectionRevision'))):
                raise ValueError('inline turn source identity changed')
            encoded = encoded.get('projection')
        projection = decode_projection_from_storage(encoded)
        del encoded
        if not isinstance(projection, dict):
            raise ValueError('projection must be an object')
        expected = turn['projectionRevision'] - len(patches) + 1
        for item in patches:
            patch = item.get('projectionPatch') if isinstance(item, dict) else None
            if (not isinstance(patch, dict) or item.get('projectionRevision') != expected
                    or patch.get('baseRevision') != expected - 1
                    or patch.get('targetRevision') != expected):
                raise ValueError('projection patch chain is not contiguous')
            projection = apply_projection_patch(projection, patch)
            expected += 1
        transform = page.get('projectionTransform')
        if transform == 'terminalize_restored':
            terminalize_restored_projection(projection)
        elif transform is not None:
            raise ValueError('unknown public projection transform')
        return _turn_to_legacy_message({**turn, 'projection': projection}), turn['ordinal']
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise DiagnosticAPIError('API returned malformed diagnostic projection data') from exc


#: Verification passes over the selected range. The first pass reads every
#: selected Turn; later passes re-verify retained reconstructions by source
#: token at the newest revision and re-download only Turns that changed.
SNAPSHOT_PASSES = 6
#: Bounded pauses before a re-verification pass or a same-header Turn retry, so
#: a write burst can settle or a structural write can retire a text tail.
SNAPSHOT_RETRY_DELAYS_S = (0.25, 0.75)
#: Reconstructions started per Turn per run; bounds re-downloads of a running Turn.
SNAPSHOT_TURN_READS = 3
#: Consecutive revision moves one request follows before its Turn waits for the next pass.
SNAPSHOT_MOVE_RETRIES = 3


class _RevisionMoved(Exception):
    """Internal: a fenced read conflicted because the conversation header advanced."""


class _UnchangedHeaderConflict(Exception):
    """Internal: a fenced read conflicted although the conversation header did not change."""


class _SourceChanged(Exception):
    """Internal: a Turn's source token stopped matching while it was being read."""


class _Deferred(Exception):
    """Internal: revision moves outpaced one request; the Turn waits for the next pass."""


class _VerifiedTurn:
    """One complete reconstruction, consistent under a single source token.

    ``revision``/``window`` record the newest fenced read that confirmed
    ``token``; ``stale`` records that a later fenced read proved it changed.
    """

    __slots__ = ('message', 'ordinal', 'token', 'size', 'revision', 'window', 'stale')

    def __init__(self, message: dict, ordinal: int, token: str, size: int,
                 revision: int, window: int):
        self.message, self.ordinal, self.token, self.size = message, ordinal, token, size
        self.revision, self.window, self.stale = revision, window, False


def _retry_delay(position: int) -> float:
    return SNAPSHOT_RETRY_DELAYS_S[min(position, len(SNAPSHOT_RETRY_DELAYS_S) - 1)]


def _pause_before_snapshot_retry(api: _DiagnosticAPI, delay: float) -> None:
    import time
    remaining = api.deadline - time.monotonic()
    if delay > 0 and remaining > 0:
        time.sleep(min(delay, remaining / 2))


class _SnapshotReader:
    """Fence each read on its own Turn, then verify every Turn at one revision.

    The storage fences every Turn/chunk read on the *current* conversation
    revision and on that Turn's source token, which binds the Turn's source
    generation and not the conversation revision. A conflict therefore proves
    only that something changed; the reader classifies it with a header probe
    and follows a moved revision instead of restarting: chunks continue under
    the unchanged token, and a complete reconstruction is re-verified later by a
    one-byte fenced chunk at the newest revision. A *window* is a run of fenced
    reads at one revision with no unexplained conflict. The document is a
    consistent snapshot only when one window verified every shown Turn and a
    final header confirmed that no write landed meanwhile; otherwise each shown
    Turn is still internally consistent and the document is labelled
    best-effort with its revision bounds. No retry discards verified bytes and
    no torn view is labelled consistent.
    """

    def __init__(self, api: _DiagnosticAPI, conv_id: str, *, full: bool,
                 start_index: int | None, max_turns: int | None):
        from collections import Counter
        from urllib.parse import quote
        self.api, self.conv_id, self.full = api, conv_id, full
        self.start_index, self.max_turns = start_index, max_turns
        self.path = '/api/v4/conversations/' + quote(conv_id, safe='')
        self.diagnostic = self.path + '/diagnostic'
        self.metadata: dict = {}
        self.revision: int | None = None
        self.total: int | None = None
        self.window = 0
        self.header_bytes = 0
        self.turns: dict[int, _VerifiedTurn] = {}
        self.reads: Counter = Counter()
        self.anomalies: Counter = Counter()
        self.live: tuple[int, int, int] | None = None
        self.live_seen: tuple[int, int, int] | None = None
        self.incomplete: str | None = None
        self.resume_index: int | None = None
        self.budget_stopped = False
        self.archives: list[dict] = []
        self.archive_count: int | str = 'unknown'
        self.archive_window: int | None = None

    def read(self) -> tuple[dict | None, list[dict]]:
        if not self._header(missing_ok=True):
            return None, []
        for number in range(SNAPSHOT_PASSES):
            if number:
                if not self._may_retry():
                    break
                _pause_before_snapshot_retry(self.api, _retry_delay(number - 1))
            if self._verify_pass() and self._confirm():
                return self._document(consistent=True)
        return self._document(consistent=False)

    def _header(self, *, missing_ok: bool = False) -> bool:
        before = self.api.bytes_read
        page = self.api.get(self.diagnostic + '?kind=header', missing_ok=missing_ok)
        if page is None:
            return False
        metadata, revision, total = _diagnostic_header(page, self.conv_id)
        self.header_bytes = max(self.header_bytes, self.api.bytes_read - before)
        if (revision, total) != (self.revision, self.total):
            self.window += 1
        self.metadata, self.revision, self.total = metadata, revision, total
        return True

    def _fenced(self, query: str) -> dict:
        """One revision/source-fenced read; a conflict is classified by a header probe."""
        try:
            return self.api.get(self.diagnostic + query, snapshot_read=True)
        except DiagnosticSnapshotConflict:
            before = (self.revision, self.total)
            self._header()
            if (self.revision, self.total) != before:
                raise _RevisionMoved from None
            raise _UnchangedHeaderConflict from None

    def _selection(self):
        total = self.total
        if self.start_index is not None or self.max_turns is not None:
            start = self.start_index or 0
            if start > total:
                raise DiagnosticAPIError('Start index exceeds the conversation turn count')
            return range(start, min(total, start + (self.max_turns or total)))
        if self.full:
            return range(total)
        return sorted(set(range(min(HEAD_MESSAGES, total)))
                      | set(range(max(0, total - TAIL_MESSAGES), total)))

    def _descriptor(self, index: int) -> tuple[dict, int, str]:
        for _ in range(SNAPSHOT_MOVE_RETRIES + 1):
            if index >= self.total:
                raise _Deferred
            try:
                page = self._fenced(f'?kind=turn&revision={self.revision}&index={index}')
            except _RevisionMoved:
                continue
            size, token = _validate_turn_descriptor(
                page, conv_id=self.conv_id, revision=self.revision, total=self.total, index=index)
            return page, size, token
        raise _Deferred

    def _chunk(self, index: int, token: str, *, size: int, offset: int, limit: int) -> bytes:
        """One source range at the newest revision; the token fences the source."""
        for _ in range(SNAPSHOT_MOVE_RETRIES + 1):
            if index >= self.total:
                raise _Deferred
            revision = self.revision
            try:
                page = self._fenced(f'?kind=chunk&revision={revision}&index={index}'
                                    f'&source_token={token}&offset={offset}&limit={limit}')
            except _RevisionMoved:
                continue
            return _chunk_data(page, revision=revision, index=index, size=size,
                               offset=offset, limit=limit, token=token)
        raise _Deferred

    def _reconstruct(self, index: int) -> _VerifiedTurn:
        """Download one Turn under one token, following revision moves between chunks."""
        page, size, token = self._descriptor(index)
        api = self.api
        chunks = (size + 65535) // 65536
        # Refuse before spending a partial Turn; the final header keeps its reserve.
        wire_bytes = ((size + 2) // 3) * 4 + chunks * 1024
        if (wire_bytes > api.remaining_bytes
                or api.requests + chunks > api.MAX_REQUESTS - api.reserved_requests):
            raise DiagnosticBudgetError(f'Turn {index} exceeds the remaining diagnostic byte/request budget')
        confirmed = self.revision
        raw = bytearray()
        while len(raw) < size:
            try:
                raw.extend(self._chunk(index, token, size=size, offset=len(raw),
                                       limit=min(65536, size - len(raw))))
            except _UnchangedHeaderConflict:
                if confirmed == self.revision:
                    # Changed without a revision move: distrust this whole window.
                    self.window += 1
                raise _SourceChanged from None
            confirmed = self.revision
        message, ordinal = _decode_api_turn(page, raw)
        return _VerifiedTurn(message, ordinal, token, size, self.revision, self.window)

    def _witness(self, index: int, read: _VerifiedTurn) -> bool:
        """Re-verify a retained reconstruction with a one-byte chunk at the newest revision."""
        try:
            self._chunk(index, read.token, size=read.size, offset=0, limit=1)
        except _UnchangedHeaderConflict:
            if read.revision == self.revision:
                self.window += 1
            read.stale = True
            return False
        read.revision, read.window = self.revision, self.window
        return True

    def _attempt(self, index: int) -> None:
        try:
            read = self._reconstruct(index)
        except _SourceChanged:
            self.reads[index] += 1
            return
        except _UnchangedHeaderConflict:
            self.reads[index] += 1
            self.window += 1
            self._observe_unchanged_header_conflict(index)
            return
        self.reads[index] += 1
        self.turns[index] = read
        if index == self.total - 1:
            self.live_seen = None

    def _observe_unchanged_header_conflict(self, index: int) -> None:
        """Only a streaming final Turn refuses its descriptor at an unchanged header.

        Two consecutive observations at one header classify it live for that
        header (native live text folds into a tail without a revision). A
        non-final Turn that keeps doing it is a storage anomaly, not churn.
        """
        if index != self.total - 1:
            self.anomalies[index] += 1
            if self.anomalies[index] >= SNAPSHOT_TURN_READS:
                raise DiagnosticAPIError(
                    f'Turn {index} conflicted at unchanged revision {self.revision} on '
                    f'{SNAPSHOT_TURN_READS} reads; only a streaming final Turn may do that, so '
                    'this is not write churn. See the application error log (logs/error.log) '
                    f'and re-read it with --start-index {index} --max-turns 1.')
        else:
            key = (self.revision, self.total, index)
            if self.live_seen == key:
                self.live = key
                return
            self.live_seen = key
        _pause_before_snapshot_retry(self.api, _retry_delay(self.reads[index] - 1))

    def _verified(self, index: int) -> bool:
        read = self.turns.get(index)
        return read is not None and not read.stale and read.window == self.window

    def _is_live(self, index: int) -> bool:
        """The final Turn is still being written and stays outside the snapshot.

        Either its streaming tail refused it twice at this exact header, or it
        changed or conflicted through every allowed read and has no current copy.
        """
        if index != self.total - 1:
            return False
        if self.live == (self.revision, self.total, index):
            return True
        read = self.turns.get(index)
        return self.reads[index] >= SNAPSHOT_TURN_READS and (read is None or read.stale)

    def _settle(self, index: int) -> None:
        """Verify one selected Turn in the current window within its read allowance."""
        try:
            while (index < self.total and not self._verified(index)
                   and not self._is_live(index)):
                read = self.turns.get(index)
                if read is not None and not read.stale:
                    self._witness(index, read)
                elif self.reads[index] < SNAPSHOT_TURN_READS:
                    self._attempt(index)
                else:
                    return
        except _Deferred:
            return

    def _pending(self):
        return [index for index in self._selection()
                if self.resume_index is None or index < self.resume_index]

    def _verify_pass(self) -> bool:
        """Settle every selected Turn; read archives once one window covers them all."""
        selection = self._selection()
        for index in [index for index in self.turns if index not in selection]:
            del self.turns[index]
        # A same-revision header has the same bounded representation. Retain its
        # observed size plus envelope slack and one request for the final witness.
        self.api.reserved_bytes = max(4096, self.header_bytes + 1024)
        self.api.reserved_requests = 1
        try:
            for index in self._pending():
                try:
                    self._settle(index)
                except DiagnosticBudgetError as exc:
                    self.incomplete, self.budget_stopped = str(exc), True
                    if index not in self.turns:
                        self.resume_index = index
                    break
            covered = all(self._verified(index) or self._is_live(index)
                          for index in self._pending())
            if covered:
                self._read_archives()
            return covered
        finally:
            self.api.reserved_bytes = 0
            self.api.reserved_requests = 0

    def _read_archives(self) -> None:
        try:
            self.archives, self.archive_count = _load_api_archives(
                self.api, self.path, full=self.full)
        except DiagnosticBudgetError as exc:
            self.incomplete, self.budget_stopped = str(exc), True
            return
        self.archive_window = self.window

    def _confirm(self) -> bool:
        """Metadata-only final witness: no write landed while the window was read."""
        observed = (self.revision, self.total, self.window)
        try:
            self._header()
        except DiagnosticBudgetError as exc:
            if not self.turns:
                raise
            self.incomplete, self.budget_stopped = str(exc), True
            return False
        return (self.revision, self.total, self.window) == observed

    def _may_retry(self) -> bool:
        if self.budget_stopped:
            return False
        for index in self._pending():
            read = self.turns.get(index)
            if (index < self.total and self.reads[index] >= SNAPSHOT_TURN_READS
                    and (read is None or read.stale) and not self._is_live(index)):
                return False  # never verifiable again; a consistent snapshot is unreachable
        return True

    def _document(self, *, consistent: bool) -> tuple[dict, list[dict]]:
        selection = self._selection()
        shown = [(index, self.turns[index]) for index in selection if index in self.turns]
        if not consistent and not shown and len(selection) and not self.budget_stopped:
            raise DiagnosticAPIError(
                'Conversation changed during diagnostic reads; no selected turn could be read '
                f'before the revision moved again ({SNAPSHOT_PASSES} verification passes). Reads '
                'are fenced on the current revision and each turn\'s source token, so a '
                'conversation written continuously faster than single requests stays '
                'unreadable; retry when writes settle.')
        verified = consistent and all(self._verified(index) for index, _ in shown)
        identities, last_ordinal = set(), -1
        for _, read in shown:
            identity = read.message['_turnId']
            if identity in identities or read.ordinal <= last_ordinal:
                raise DiagnosticAPIError(
                    'API returned duplicate or out-of-order diagnostic turns' if verified else
                    'Turns verified at different revisions are duplicated or out of order: turns '
                    'were inserted or deleted while the conversation was read; retry when writes settle')
            identities.add(identity)
            last_ordinal = read.ordinal
        final = self.total - 1
        live = final if (final in selection and not self._verified(final)
                         and self._is_live(final)) else None
        archives_current = verified and self.archive_window == self.window
        archives_shown = archives_current or not verified
        revisions = [read.revision for _, read in shown]
        document = {'source': 'authenticated-application-api', 'metadata': {
            **self.metadata, 'user_id': 'authenticated API principal', 'msg_count': self.total,
        }, 'messages': [read.message for _, read in shown],
            'diagnostic_message_indices': [index for index, _ in shown],
            'diagnostic_turn_revisions': revisions,
            'diagnostic_total_count': self.total, 'diagnostic_revision': self.revision,
            'diagnostic_snapshot': 'consistent' if verified else 'best-effort',
            'diagnostic_archive_catalog_count': self.archive_count if archives_shown else 'unknown',
            'diagnostic_unread_count': self.total - len(shown)}
        if not verified:
            document['diagnostic_revision_bounds'] = [min(revisions + [self.revision]),
                                                      max(revisions + [self.revision])]
        if live is not None:
            document['diagnostic_live_index'] = live
            document['diagnostic_live_reason'] = (
                'streaming' if self.live == (self.revision, self.total, live) else 'unsettled')
        if self.incomplete:
            document['diagnostic_incomplete'] = self.incomplete
        if self.resume_index is not None:
            document['diagnostic_resume_index'] = self.resume_index
        return document, (self.archives if archives_shown else [])


def _load_api_document(api: _DiagnosticAPI, conv_id: str, *, full: bool,
                       start_index: int | None = None, max_turns: int | None = None,
                       ) -> tuple[dict | None, list[dict]]:
    """Read the selected Turns as one verified revision, or a labelled best-effort view."""
    return _SnapshotReader(api, conv_id, full=full, start_index=start_index,
                           max_turns=max_turns).read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='One-shot read-only debug dump for a conversation ID.')
    parser.add_argument('conv_id', type=_conversation_id)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--api-url', help='explicit running application origin, e.g. http://127.0.0.1:15000 (native or SQL); default: auto-discovered from the configured backend and PORT')
    parser.add_argument('--api-token-env', help='environment variable containing a conversation-scoped bearer token; never pass a token on the command line')

    parser.add_argument(
        '--api-max-total-mib', type=_api_total_mib, default=None,
        help='explicit aggregate API read allowance in MiB (1..1024; default 32); '
             'per-response, time, request, and consistent-snapshot limits still apply')
    source.add_argument(
        '--db', type=Path, default=None,
        help=('explicit sqlite authority; default auto-discovers the active '
              'Sidecar/fastpath authority'))
    parser.add_argument('--user-id', type=int, default=None,
                        help='owning user (default: auto-detect from row)')
    parser.add_argument('--full', action='store_true',
                        help='untruncated transcript, all messages')
    parser.add_argument('--raw', action='store_true',
                        help='dump the messages array as JSON')
    parser.add_argument('--json', action='store_true',
                        help=('print exactly one JSON document on stdout instead of the report; '
                              'reads every turn like --raw; exit 1 leaves stdout empty'))
    parser.add_argument('--request-evidence', action='store_true',
                        help='API source: show latest task/attempt/trace/archive references; never dump bodies')

    parser.add_argument('--start-index', type=int, default=None,
                        help='API diagnostic: first zero-based main-lane turn index')
    parser.add_argument('--max-turns', type=int, default=None,
                        help='API diagnostic: read at most this many turns from start-index')
    parser.add_argument('--logs', type=_log_line_count, default=LOG_LINE_CAP,
                        metavar='N',
                        help=f'log lines per file, 1-1000 (default: {LOG_LINE_CAP})')
    parser.add_argument('--no-logs', action='store_true',
                        help='omit matching log lines')
    parser.add_argument('--log-match', type=_log_match_pattern, default=None,
                        metavar='REGEX',
                        help=('keep only conversation log lines matching REGEX '
                              '(Python re.search), applied before the per-file --logs cap'))
    parser.add_argument('--tools', action='store_true',
                        help=('render each assistant tool round: tool names, '
                              'args, per-result status/error, and thinking'))
    parser.add_argument('--tool-results', action='store_true',
                        help='compare retained model content, frontend inputs, canonical references and format findings')
    parser.add_argument('--tool-result-limit', type=int, default=20, help='maximum compared results (1..256; default 20)')
    parser.add_argument('--tool-call-id', help='filter --tool-results by provider call id; repeated ids retain Turn identity')
    parser.add_argument('--no-anomalies', action='store_true',
                        help=('omit the Anomalies section (tool rejections, '
                              'errored calls, failed attempts)'))
    parser.add_argument('--context', action='store_true',
                        help=('list every system_note (context injection) segment with its '
                              'round, reason, size, source files and repeats; reads every turn'))
    parser.add_argument(
        '--logs-dir', type=Path, default=None,
        help=('explicit local logs directory to scan for app.log/access.log; '
              'the only way to include logs in --api-url mode, where the '
              'endpoint cannot prove ownership of local log files'))
    args = parser.parse_args(argv)
    if not 1 <= args.tool_result_limit <= 256:
        parser.error('--tool-result-limit must be between 1 and 256')
    if args.tool_call_id and not args.tool_results:
        parser.error('--tool-call-id requires --tool-results')
    if args.api_token_env and not args.api_url:
        parser.error('--api-token-env requires --api-url')
    if args.logs_dir is not None:
        if args.no_logs:
            parser.error('--logs-dir and --no-logs are contradictory')
        if not args.logs_dir.is_dir():
            parser.error('--logs-dir must name an existing directory')
    if args.api_url and args.user_id is not None:
        parser.error('--user-id is only for offline SQLite; API identity comes from authentication')
    if args.json and args.raw:
        parser.error('--json already carries the full messages array; drop --raw')
    if args.log_match is not None and args.no_logs:
        parser.error('--log-match and --no-logs are contradictory')
    if not args.json:
        return _inspect(parser, args, {})

    collected: dict = {}
    with contextlib.redirect_stdout(_DiscardedReport()):
        code = _inspect(parser, args, collected)
    if code == 1:
        return code
    document = collected.get('document')
    payload = {'conversationId': args.conv_id, 'found': document is not None,
               'exitCode': code}
    if document is not None:
        payload['document'] = {key: value for key, value in document.items()
                               if key != 'messages'}
        payload['messages'] = document.get('messages') or []
        payload.update((key, collected[key]) for key in (
            'anomalies', 'compactionArchives', 'logs', 'contextInjections')
            if key in collected)
    json.dump(payload, sys.stdout, ensure_ascii=False, default=str)
    sys.stdout.write('\n')
    return code


class _DiscardedReport(io.TextIOBase):
    """``--json`` reserves stdout for its one document; report prints land here."""

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        return len(text)


def _inspect(parser: argparse.ArgumentParser, args: argparse.Namespace,
             collected: dict) -> int:
    """Print the report; ``collected`` receives the data ``--json`` emits."""
    if not args.api_url:
        from tofu_dotenv import read_dotenv_values
        try:
            environment = {**read_dotenv_values(REPO_ROOT / '.env'), **os.environ}
            base = Path(resolve_runtime_base(environment, repository_root=REPO_ROOT, allow_probe=False))
        except (OSError, RuntimeError, ValueError) as exc:
            print(f'error: cannot resolve diagnostic paths: {exc}', file=sys.stderr)
            return 1
        data_dir = base / 'data'
    auto_api = False
    if args.db is None and args.api_url is None:
        if _resolve_storage_backend(environment) == 'tofudb':
            # The native backend has no inspectable SQLite file: the running
            # application is the only read authority for this deployment.
            try:
                args.api_url = _discover_application_api_url(environment)
            except DiagnosticAPIError as exc:
                print(f'error: {exc}', file=sys.stderr)
                return 1
            auto_api = True
        else:
            configuration_error = _validate_sqlite_discovery_configuration(environment)
            if configuration_error:
                print(f'error: {configuration_error}', file=sys.stderr)
                return 1
    if ((args.start_index is not None and args.start_index < 0)
            or (args.max_turns is not None and args.max_turns < 1)):
        parser.error('--start-index must be non-negative and --max-turns positive')
    if (args.start_index is not None or args.max_turns is not None) and not args.api_url:
        parser.error('turn range flags require the application API diagnostic source')
    conv_id = args.conv_id
    anomalies: list[dict] = []
    if args.api_url:
        try:
            token = os.environ.get(args.api_token_env) if args.api_token_env else None
            if args.api_token_env and token is None:
                raise DiagnosticAPIError('Requested API token environment variable is unset')
            api = _DiagnosticAPI(args.api_url, token)

            if args.api_max_total_mib is not None:
                api.MAX_TOTAL_BYTES = args.api_max_total_mib * 1024 * 1024
                api.MAX_REQUESTS = api.MAX_TOTAL_BYTES // (64 * 1024) + 128
            document, archives = _load_api_document(
                api, conv_id, full=args.full or args.raw or args.json or args.context,
                start_index=args.start_index, max_turns=args.max_turns)
        except DiagnosticAPIError as exc:
            print(f'error: {exc}', file=sys.stderr)
            return 1
        if document is not None:
            anomalies = _load_anomalies(None, conv_id, document)
        print(f'== Authority == ({api.origin})')
        if auto_api:
            print('  discovery: auto-discovered application API from the configured backend (tofudb) and PORT')
        else:
            print('  discovery: explicit authenticated application API; backend ownership stays with the service')
        print('  location probes: unavailable over this API; no database files opened')
        print('  archive catalog: service-returned window; not a full storage inventory')
        if document is not None:
            print(f"  archives loaded: {len(archives)} of {document['diagnostic_archive_catalog_count']} returned by service")
        found_anywhere = document is not None
        if args.logs_dir is None and not auto_api:
            if not args.no_logs:
                print('  logs: omitted because this endpoint does not prove ownership of local log files')
            args.no_logs = True
    else:
        from lib.storage_authority_sql.offline import SQLiteAuthorityDiscoveryError
        try:
            location = resolve_readonly_sqlite_authority(
                data_dir, explicit_path=args.db, environ=environment)
        except FileNotFoundError as exc:
            missing = args.db if args.db is not None else data_dir
            print(f'error: database not found: {missing}', file=sys.stderr)
            print(f'detail: {exc}', file=sys.stderr)
            return 1
        except SQLiteAuthorityDiscoveryError as exc:
            print(f'error: cannot identify the active SQLite authority: {exc}',
                  file=sys.stderr)
            print('hint: do not assume data/tofu.db while a fastpath shadow exists; '
                  'start the Sidecar or pass an explicitly verified --db path.',
                  file=sys.stderr)
            return 1
        db_path = location.path
        from lib.storage.errors import StorageError
        connection = None
        archives: list[dict] = []
        try:
            connection = _open_ro(db_path)
            conv_id = args.conv_id

            print(f'== Authority == ({db_path})')
            print(f'  discovery:  {location.source}')
            print(f'  fastpath:   {location.fastpath_active}')
            print('== Location probes ==')
            probes = _probe_locations(connection, conv_id)
            found_anywhere = False
            for table, present, detail in probes:
                marker = 'HIT ' if present else '    '
                print(f'  {marker} {table}: {detail}')
                found_anywhere = found_anywhere or bool(present)

            document = _load_sidecar_document(connection, conv_id, args.user_id)
            if document is not None:
                owner = args.user_id
                if owner is None:
                    owner = int((document.get('metadata') or {}).get('user_id'))
                archives = _load_compaction_archives(
                    connection, conv_id, int(owner))
                anomalies = _load_anomalies(connection, conv_id, document)
        except (OSError, sqlite3.Error, StorageError) as exc:
            print(f'error: cannot inspect database {db_path}: {exc}', file=sys.stderr)
            print('hint: verify --db, file permissions, and SQLite integrity; '
                  'run `python serverctl.py doctor` for host diagnostics.',
                  file=sys.stderr)
            return 1
        finally:
            if connection is not None:
                connection.close()

    collected['document'] = document
    if document is None:
        print('\n== Not found ==')
        print(f"  conversation '{conv_id}' was not found in the selected authority and owner scope.")
        if not found_anywhere and not args.api_url:
            print('  No table references this ID at all — check for a typo,')
            print('  a different deployment, or pass --db to another file.')
        return 2

    metadata = document.get('metadata', {})
    print(f"\n== Metadata == (source: {document.get('source')})")
    print(f"  id:         {metadata.get('id')}")
    print(f"  user_id:    {metadata.get('user_id')}")
    print(f"  title:      {metadata.get('title')}")
    print(f"  created_at: {_format_ts(metadata.get('created_at'))}")
    print(f"  updated_at: {_format_ts(metadata.get('updated_at'))}")
    print(f"  msg_count:  {metadata.get('msg_count')}")
    print(f"  rev:        {metadata.get('rev')}")
    settings = metadata.get('settings') or {}
    if settings:
        from lib.logging_pkg.log_record_values import sanitize_value
        safe_settings = sanitize_value(settings, max_items=100,
                                       max_string_chars=2_000)
        print(f'  settings:   {json.dumps(safe_settings, ensure_ascii=False)}')

    messages = document.get('messages') or []
    print(f'\n== Transcript == ({metadata.get("msg_count", len(messages))} message(s)'
          f'{"" if args.full or args.raw else f", head {HEAD_MESSAGES} + tail {TAIL_MESSAGES}"})')
    widened = ((args.json or args.context) and not args.full
               and args.start_index is None and args.max_turns is None)
    for line in _render_transcript(document, full=args.full, raw=args.raw,
                                   tools=args.tools, head_tail=widened):
        print(line)

    if args.request_evidence:
        print('\n== Provider request evidence ==')
        if args.api_url:
            for line in _render_request_evidence(api, document):
                print(line)
        else:
            print('  Unavailable for offline SQLite; no alternative authority was queried.')

    if args.tool_results:
        print('\n== Tool result surfaces ==')
        for line in _render_result_surfaces(document, tool_call_id=args.tool_call_id, full=args.full, max_results=args.tool_result_limit):
            print(line)

    best_effort = document.get('diagnostic_snapshot') == 'best-effort'
    if 'diagnostic_unread_count' in document:
        if best_effort:
            low, high = document['diagnostic_revision_bounds']
            span = f'revisions {low}..{high}, not one snapshot'
        else:
            span = f'revision {document["diagnostic_revision"]}'
        print(f'  coverage: {len(messages)} read, {document["diagnostic_unread_count"]} unread; '
              f'{span}; anomalies cover read turns only')
    if best_effort:
        print('  BEST-EFFORT: writes never paused long enough to verify every shown turn at one '
              'revision. Each turn is internally consistent; one marked [as of revision N] was '
              f'last verified before revision {document["diagnostic_revision"]}, whose metadata '
              'is shown. Re-run when writes settle, or narrow --start-index / --max-turns to '
              'shorten verification.')
    if document.get('diagnostic_incomplete'):
        print(f'  INCOMPLETE: {document["diagnostic_incomplete"]}')
        resume = document.get('diagnostic_resume_index')
        if resume is not None:
            print(f'  resume: --start-index {resume} --max-turns 1 '
                  '(fresh revision; not a persisted snapshot)')
    live = document.get('diagnostic_live_index')
    if live is not None:
        if document.get('diagnostic_live_reason') == 'streaming':
            reason = ('its streaming text kept the revision-fenced read in conflict at revision '
                      f'{document["diagnostic_revision"]}')
        else:
            reason = 'it changed or conflicted through every bounded read attempt'
        shown = ('shown as last fully read, marked [live, as of revision N]'
                 if live in (document.get('diagnostic_message_indices') or ()) else 'not shown')
        print(f'  LIVE: turn #{live + 1} is still being written; {reason}; {shown}. '
              f'Re-read it after it settles: --start-index {live} --max-turns 1')

    if args.context:
        injections = _context_injections(document)
        collected['contextInjections'] = injections
        print('\n== Context injections ==')
        for line in _render_context_injections(injections):
            print(line)

    if not args.no_anomalies:
        collected['anomalies'] = anomalies
        print(f'\n== Anomalies == ({len(anomalies)} found'
              f'{"" if args.full else f", showing first {ANOMALY_CAP}"})')
        for line in _render_anomalies(anomalies, full=args.full):
            print(line)

    collected['compactionArchives'] = archives
    print(f'\n== Compaction Archives == ({len(archives)} archive(s)'
          f'{"" if args.full else f", last {ARCHIVE_TAIL} returned"})')
    for line in _render_compaction_archives(archives, full=args.full):
        print(line)

    if not args.no_logs:
        # An explicit --api-url cannot prove ownership of local log files, so
        # that mode requires --logs-dir (enforced above); the auto-discovered
        # application shares this checkout's runtime base.
        log_root = args.logs_dir if args.logs_dir is not None else base / 'logs'
        print('\n== Logs ==' + (f' (--log-match {args.log_match.pattern})'
                                if args.log_match is not None else ''))
        collected['logs'] = []
        for header, matched in _scan_logs(conv_id, max(1, args.logs), log_root,
                                          window=_conversation_window(metadata),
                                          log_match=args.log_match):
            print(header)
            if matched is None and args.json:
                # The report is discarded; an unreadable log must not vanish.
                print(f'warning: {header}', file=sys.stderr)
            for line in matched or ():
                print(line)
            collected['logs'].extend(matched or ())
    incomplete = document.get('diagnostic_incomplete') or live is not None or best_effort
    return 3 if incomplete else 0


if __name__ == '__main__':
    raise SystemExit(main())
