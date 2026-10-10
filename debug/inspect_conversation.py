#!/usr/bin/env python3
"""inspect_conversation — one-shot debug dump for a conversation ID.

Responsibility: given a conversation ID (the sidebar copy-ID button copies
``data-conv-id`` values like ``mt18xr3wfs0rbq``), answer "where does this
conversation live and what does it contain" in ONE read-only pass, so an
agent never has to guess table names or storage modes.

Entry point: ``python3 debug/inspect_conversation.py <conv_id> [--full]
[--raw] [--logs N] [--no-logs] [--db PATH] [--user-id N] [--logs-dir PATH]``
or ``<conv_id> --api-url http://127.0.0.1:15000 [--api-token-env NAME]``.
With no source flags the inspector follows the configured backend: SQLite
deployments auto-discover the live Sidecar/fastpath authority; ``tofudb``
deployments auto-discover the running application's loopback API from the
configured ``PORT`` (default 15000) and read through the v4 conversation
endpoints instead of erroring out.

Dependencies: ``lib.storage_sidecar.offline`` for fail-closed active-authority
discovery plus the bounded query-only connection, and
``lib.storage_sidecar.operations`` for the exact ``conversation.get`` and
compaction-archive read paths the running sidecar serves. WAL permits concurrent
readers, so this is safe against the live SQLite database. For native storage,
explicit application API reads retain the service authorization boundary. They
never open database files or start a service; their total deadline is 30 seconds,
with a byte-derived request allowance, 8 MiB per response and 32 MiB total.
Read-only: never writes.

Exit codes: 0 = found, 2 = not found/invalid usage, 1 = database/IO error.
"""

from __future__ import annotations

import argparse
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
ARCHIVE_TAIL = 20
ARCHIVE_SUMMARY_CAP = 4_000
#: tool_use ``result.status`` values that mean the call never ran to success.
TOOL_ANOMALY_STATUSES = frozenset(
    {'rejected', 'error', 'errored', 'failed', 'skipped', 'cancelled',
     'canceled', 'timeout', 'aborted'})
ANOMALY_CAP = 25
ANOMALY_TEXT_CAP = 320
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


def resolve_readonly_sqlite_authority(*args, **kwargs):
    from lib.storage_sidecar.offline import resolve_readonly_sqlite_authority as resolve
    return resolve(*args, **kwargs)


def _open_ro(db_path: Path) -> sqlite3.Connection:
    from lib.storage_sidecar.offline import open_readonly_sqlite_authority
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

    from lib.storage_sidecar import operations as ops
    from lib.storage_sidecar.adapters.sqlite import SQLiteSession

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

    from lib.storage_sidecar import operations as ops
    from lib.storage_sidecar.adapters.sqlite import SQLiteSession

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
        try:
            raw = json.loads(raw)
        except ValueError:
            pass
    return _short(raw, ARGS_CAP)


def _result_error_text(result) -> str:
    """Pull the human-readable error out of a tool_use ``result`` envelope."""
    if not isinstance(result, dict):
        return ''
    content = result.get('content')
    if isinstance(content, str) and content:
        try:
            parsed = json.loads(content)
        except ValueError:
            return _short(content, ANOMALY_TEXT_CAP)
        if isinstance(parsed, dict):
            return _short(
                parsed.get('summary') or parsed.get('error')
                or parsed.get('message') or content,
                ANOMALY_TEXT_CAP)
        return _short(content, ANOMALY_TEXT_CAP)
    for key in ('error', 'summary', 'message', 'reason'):
        if result.get(key):
            return _short(result[key], ANOMALY_TEXT_CAP)
    return ''


def _tool_round_anomaly(entry: dict) -> dict | None:
    """Build an anomaly record from a projected toolRounds entry, or None.

    The projected toolRounds are the authoritative terminal state: a rejected
    call carries ``_rejected``/``_contractError`` + ``toolArgs`` + ``toolName``;
    an errored round carries ``status='error'`` + the result content.
    """
    status = str(entry.get('status') or '').strip().lower()
    contract = entry.get('_contractError')
    rejected = entry.get('_rejected')
    if status not in TOOL_ANOMALY_STATUSES and not contract and not rejected:
        return None
    if isinstance(contract, dict):
        error = (contract.get('message') or '')
        if contract.get('path'):
            error = f"[{contract.get('code')}] {error} Path: {contract['path']}"\
                .strip()
        if contract.get('nextAction'):
            error = (error + f" {contract['nextAction']}").strip()
    elif isinstance(rejected, dict):
        error = str(rejected.get('reason') or rejected.get('code') or '')
    else:
        error = ''
    if not error:
        error = _result_error_text(entry.get('result'))
    if not error:
        results = entry.get('results')
        if isinstance(results, list):
            for result in results:
                error = _result_error_text(result)
                if error:
                    break
    if not error and entry.get('toolContent'):
        error = _short(entry.get('toolContent'), ANOMALY_TEXT_CAP)
    tool = entry.get('toolName') or entry.get('query') or '?'
    args = _tool_input_summary(
        entry.get('toolArgs') if entry.get('toolArgs') is not None
        else entry.get('input'))
    # Content-free orphan rounds (no name, args, or error) carry no
    # debugging signal; skip them.
    if tool == '?' and not args and not error and not (contract or rejected):
        return None
    is_rejection = bool(contract or rejected) or status == 'rejected'
    return {
        'kind': 'tool',
        'tool': tool,
        'status': status or ('rejected' if (contract or rejected) else '?'),
        'rejection': is_rejection,
        'round': entry.get('roundNum') if entry.get('roundNum') is not None
                 else entry.get('llmRound'),
        'args': args,
        'error': error,
        'taskId': str(entry.get('taskId') or '')[:8],
    }


def _load_anomalies(connection: sqlite3.Connection | None, conv_id: str,
                    document: dict | None) -> list[dict]:
    """Collect debugging red flags from the projected transcript + attempts.

    Primary source is the projected ``toolRounds`` (authoritative terminal
    outcome). ``storage_generation_attempts.error_json`` adds attempt-level
    failures when a direct SQLite read is available (offline mode). A
    malformed entry never aborts the dump.
    """
    anomalies = []
    for message in (document or {}).get('messages') or []:
        if not isinstance(message, dict):
            continue
        for entry in message.get('toolRounds') or []:
            if not isinstance(entry, dict):
                continue
            record = _tool_round_anomaly(entry)
            if record is not None:
                anomalies.append(record)
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
    # Rejections (contract violations — usually the actual bug) sort ahead of
    # ordinary errored rounds, then chronological by round.
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
    from lib.log_record_values import sanitize_value
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
                from lib.log_record_values import sanitize_value
                lines.append('  ' + json.dumps(sanitize_value(fields), ensure_ascii=False))
        except DiagnosticAPIError as error:
            lines.append(f'  task={task_id}: evidence unavailable ({error})')
    return lines or ['  No task/request references in the selected transcript window.']


def _render_transcript(document: dict, *, full: bool, raw: bool,
                       tools: bool = False) -> list[str]:
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

    def emit(number: int, message) -> None:
        role = message.get('role', '?') if isinstance(message, dict) else '?'
        text = _message_text(message).replace('\r', '')
        if not full and len(text) > MSG_TEXT_CAP:
            text = text[:MSG_TEXT_CAP] + f'… <+{len(text) - MSG_TEXT_CAP} chars>'
        lines.append(f'--- #{number} [{role}]{_message_markers(message)}')
        lines.append(text if text else '<empty>')
        if tools:
            lines.extend(_render_tool_rounds(message, full=full))

    indices = document.get('diagnostic_message_indices')
    if isinstance(indices, list):
        previous = -1
        for index, message in zip(indices, messages):
            if index > previous + 1:
                lines.append(f'  … {index - previous - 1} unread message(s) …')
            emit(index + 1, message)
            previous = index
        remaining = int(document.get('diagnostic_total_count', 0)) - previous - 1
        if remaining > 0:
            lines.append(f'  … {remaining} unread message(s) …')
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
    from lib.log_record_values import sanitize_value
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


def _scan_logs(conv_id: str, cap: int, log_dir: Path | None = None) -> list[str]:
    from lib.log_record_values import text_value

    lines = []
    for name in ('app.log', 'access.log'):
        path = (LOG_DIR if log_dir is None else log_dir) / name
        if not path.is_file():
            continue
        try:
            with path.open('rb') as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - LOG_TAIL_BYTES))
                chunk = handle.read().decode('utf-8', errors='replace')
        except OSError as exc:
            lines.append(f'{name}: unreadable: {exc}')
            continue
        matches = [
            text_value(line, max_chars=16_384)
            for line in chunk.splitlines() if conv_id in line
        ]
        matches = matches[-cap:]
        lines.append(f'--- {name}: {len(matches)} line(s) '
                     f'(last {LOG_TAIL_BYTES // 1024 // 1024} MiB)')
        lines.extend(matches)
    return lines


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

    def get(self, path: str, *, missing_ok: bool = False) -> dict | None:
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
            unavailable = ('Diagnostic endpoint unavailable or incompatible (HTTP 404); '
                           'update the running application and storage service before retrying. '
                           'This does not establish that the conversation is missing.')
            if response.status != 200 and not missing_candidate:
                raise DiagnosticAPIError(f'API refused read (HTTP {response.status}); verify endpoint and conversation-scoped authorization')
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise DiagnosticAPIError('API returned unsupported compressed content')
            media_type = response.getheader('Content-Type', '').split(';', 1)[0].strip().lower()
            expected_type = 'application/problem+json' if missing_candidate else 'application/json'
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


def _load_api_turn(api: _DiagnosticAPI, path: str, *, conv_id: str,
                   revision: int, total: int, index: int) -> tuple[dict, int]:
    """Reconstruct one bounded client-side turn, reusing the authority codecs."""
    import base64
    import binascii
    from lib.storage_sidecar.projection_codec import decode_projection_from_storage
    from lib.storage_sidecar.operations_pkg._conversations import _turn_to_legacy_message
    from lib.turn_projection_patch import apply_projection_patch
    from lib.storage_sidecar.turn_projection_head import PROJECTION_HEAD_MAX_PATCHES

    from lib.conversations.diagnostic import terminalize_restored_projection
    cursor = f'&revision={revision}&index={index}'
    page = api.get(path + '?kind=turn' + cursor)
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
    size = source['totalBytes']
    chunks = (size + 65535) // 65536
    # The document reader protects a separate final-witness allowance across
    # every turn and archive read. Refuse before spending a partial turn.
    wire_bytes = ((size + 2) // 3) * 4 + chunks * 1024
    if (wire_bytes > api.remaining_bytes
            or api.requests + chunks > api.MAX_REQUESTS - api.reserved_requests):
        raise DiagnosticBudgetError(f'Turn {index} exceeds the remaining diagnostic byte/request budget')
    source_token = source.get('token')
    if not isinstance(source_token, str) or not re.fullmatch(r'[0-9a-f]{64}', source_token):
        raise DiagnosticAPIError('API returned missing or invalid diagnostic source token')
    cursor += '&source_token=' + source_token
    # No server-side full-turn hydration. The client materializes at most the
    # unchanged total read budget and releases the encoded base after decoding.
    raw = bytearray()
    offset = 0
    while offset < size:
        limit = min(65536, size - offset)
        chunk = api.get(path + '?kind=chunk' + cursor + f'&offset={offset}&limit={limit}')
        try:
            data = base64.b64decode(chunk.get('data', ''), validate=True)
        except (ValueError, TypeError, binascii.Error) as exc:
            raise DiagnosticAPIError('API returned invalid diagnostic chunk encoding') from exc
        end = offset + len(data)
        if (chunk.get('revision') != revision or chunk.get('index') != index
                or chunk.get('totalBytes') != size or chunk.get('offset') != offset
                or chunk.get('sourceToken') != source_token
                or len(data) != limit
                or chunk.get('nextOffset') != (end if end < size else None)):
            raise DiagnosticAPIError('API returned a non-contiguous diagnostic chunk')
        raw.extend(data)
        offset = end
    try:
        encoded = json.loads(raw)
        del raw
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


def _load_api_document(api: _DiagnosticAPI, conv_id: str, *, full: bool,
                       start_index: int | None = None, max_turns: int | None = None,
                       ) -> tuple[dict | None, list[dict]]:

    from urllib.parse import quote
    path = '/api/v4/conversations/' + quote(conv_id, safe='')
    diagnostic = path + '/diagnostic'
    before_header = api.bytes_read
    page = api.get(diagnostic + '?kind=header', missing_ok=True)
    if page is None:
        return None, []
    metadata, revision, total = _diagnostic_header(page, conv_id)
    if start_index is not None or max_turns is not None:
        start = start_index or 0
        if start > total:
            raise DiagnosticAPIError('Start index exceeds the conversation turn count')
        selected = range(start, min(total, start + (max_turns or total)))
    elif full:
        selected = range(total)
    else:
        selected = sorted(set(range(min(HEAD_MESSAGES, total)))
                          | set(range(max(0, total - TAIL_MESSAGES), total)))
    document = {'source': 'authenticated-application-api', 'metadata': {
        **metadata, 'user_id': 'authenticated API principal', 'msg_count': total,
    }, 'messages': [], 'diagnostic_message_indices': [],
        'diagnostic_total_count': total, 'diagnostic_revision': revision,
        'diagnostic_archive_catalog_count': 'unknown'}
    # A same-revision header has the same bounded representation. Retain its
    # observed wire size plus envelope slack and one request, even if archives
    # consume their entire allowance (including an over-limit sentinel byte).
    api.reserved_bytes = max(4096, api.bytes_read - before_header + 1024)
    api.reserved_requests = 1
    seen_ids: set[str] = set()
    last_ordinal = -1
    archives = []
    try:
        for index in selected:
            try:
                message, ordinal = _load_api_turn(api, diagnostic, conv_id=conv_id,
                                                 revision=revision, total=total, index=index)
            except DiagnosticBudgetError as exc:
                document['diagnostic_incomplete'] = str(exc)
                document['diagnostic_resume_index'] = index
                break
            identity = message['_turnId']
            if identity in seen_ids or ordinal <= last_ordinal:
                raise DiagnosticAPIError('API returned duplicate or out-of-order diagnostic turns')
            seen_ids.add(identity)
            last_ordinal = ordinal
            document['messages'].append(message)
            document['diagnostic_message_indices'].append(index)
        try:
            archives, count = _load_api_archives(api, path, full=full)
            document['diagnostic_archive_catalog_count'] = count
        except DiagnosticBudgetError as exc:
            document['diagnostic_incomplete'] = str(exc)
    finally:
        api.reserved_bytes = 0
        api.reserved_requests = 0
    # A metadata-only fence never re-downloads the last oversized turn. If
    # even the fence is unaffordable, no transcript is reported as consistent.
    witness = api.get(diagnostic + '?kind=header')
    _, last_revision, last_total = _diagnostic_header(witness, conv_id)
    if last_revision != revision or last_total != total:
        raise DiagnosticAPIError('Conversation changed during diagnostic reads; retry')
    document['diagnostic_unread_count'] = total - len(document['messages'])
    return document, archives


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
                api, conv_id, full=args.full or args.raw,
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
        from lib.storage_sidecar.offline import SQLiteAuthorityDiscoveryError
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
        from lib.log_record_values import sanitize_value
        safe_settings = sanitize_value(settings, max_items=100,
                                       max_string_chars=2_000)
        print(f'  settings:   {json.dumps(safe_settings, ensure_ascii=False)}')

    messages = document.get('messages') or []
    print(f'\n== Transcript == ({metadata.get("msg_count", len(messages))} message(s)'
          f'{"" if args.full or args.raw else f", head {HEAD_MESSAGES} + tail {TAIL_MESSAGES}"})')
    for line in _render_transcript(document, full=args.full, raw=args.raw,
                                   tools=args.tools):
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

    if 'diagnostic_unread_count' in document:
        print(f'  coverage: {len(messages)} read, {document["diagnostic_unread_count"]} unread; '
              f'revision {document["diagnostic_revision"]}; anomalies cover read turns only')
    if document.get('diagnostic_incomplete'):
        print(f'  INCOMPLETE: {document["diagnostic_incomplete"]}')
        resume = document.get('diagnostic_resume_index')
        if resume is not None:
            print(f'  resume: --start-index {resume} --max-turns 1 '
                  '(fresh revision; not a persisted snapshot)')

    if not args.no_anomalies:
        print(f'\n== Anomalies == ({len(anomalies)} found'
              f'{"" if args.full else f", showing first {ANOMALY_CAP}"})')
        for line in _render_anomalies(anomalies, full=args.full):
            print(line)

    print(f'\n== Compaction Archives == ({len(archives)} archive(s)'
          f'{"" if args.full else f", last {ARCHIVE_TAIL} returned"})')
    for line in _render_compaction_archives(archives, full=args.full):
        print(line)

    if not args.no_logs:
        # An explicit --api-url cannot prove ownership of local log files, so
        # that mode requires --logs-dir (enforced above); the auto-discovered
        # application shares this checkout's runtime base.
        log_root = args.logs_dir if args.logs_dir is not None else base / 'logs'
        print('\n== Logs ==')
        for line in _scan_logs(conv_id, max(1, args.logs), log_root):
            print(line)
    return 3 if document.get('diagnostic_incomplete') else 0


if __name__ == '__main__':
    raise SystemExit(main())
