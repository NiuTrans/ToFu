#!/usr/bin/env python3
"""inspect_conversation — one-shot debug dump for a conversation ID.

Responsibility: given a conversation ID (the sidebar copy-ID button copies
``data-conv-id`` values like ``mt18xr3wfs0rbq``), answer "where does this
conversation live and what does it contain" in ONE read-only pass, so an
agent never has to guess table names or storage modes.

Entry point: ``python3 debug/inspect_conversation.py <conv_id> [--full]
[--raw] [--logs N] [--no-logs] [--db PATH] [--user-id N]``

Dependencies: ``lib.storage_sidecar.offline`` for fail-closed active-authority
discovery plus the bounded query-only connection, and
``lib.storage_sidecar.operations`` for the exact ``conversation.get`` and
compaction-archive read paths the running sidecar serves. WAL permits concurrent
readers, so this is safe against the live database. Read-only: never writes.

Exit codes: 0 = found, 2 = not found/invalid usage, 1 = database/IO error.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.runtime_paths import data_root, logs_root
from lib.storage.errors import StorageError
from lib.storage_sidecar.offline import (
    SQLiteAuthorityDiscoveryError,
    open_readonly_sqlite_authority,
    resolve_readonly_sqlite_authority,
)

DEFAULT_DATA_DIR = Path(data_root())
LOG_DIR = Path(logs_root())

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


def _open_ro(db_path: Path) -> sqlite3.Connection:
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
        return datetime.fromtimestamp(seconds).strftime('%Y-%m-%d %H:%M:%S')
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


def _render_transcript(document: dict, *, full: bool, raw: bool) -> list[str]:
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
    from lib.log_redaction import sanitize_value
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


def _scan_logs(conv_id: str, cap: int) -> list[str]:
    from lib.log_redaction import redact_text

    lines = []
    for name in ('app.log', 'access.log'):
        path = LOG_DIR / name
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
            redact_text(line, max_chars=16_384)
            for line in chunk.splitlines() if conv_id in line
        ]
        matches = matches[-cap:]
        lines.append(f'--- {name}: {len(matches)} line(s) '
                     f'(last {LOG_TAIL_BYTES // 1024 // 1024} MiB)')
        lines.extend(matches)
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='One-shot read-only debug dump for a conversation ID.')
    parser.add_argument('conv_id', type=_conversation_id)
    parser.add_argument(
        '--db', type=Path, default=None,
        help=('explicit sqlite authority; default auto-discovers the active '
              'Sidecar/fastpath authority'))
    parser.add_argument('--user-id', type=int, default=None,
                        help='owning user (default: auto-detect from row)')
    parser.add_argument('--full', action='store_true',
                        help='untruncated transcript, all messages')
    parser.add_argument('--raw', action='store_true',
                        help='dump the messages array as JSON')
    parser.add_argument('--logs', type=_log_line_count, default=LOG_LINE_CAP,
                        metavar='N',
                        help=f'log lines per file, 1-1000 (default: {LOG_LINE_CAP})')
    parser.add_argument('--no-logs', action='store_true',
                        help='omit matching log lines')
    args = parser.parse_args(argv)

    try:
        location = resolve_readonly_sqlite_authority(
            DEFAULT_DATA_DIR, explicit_path=args.db)
    except FileNotFoundError as exc:
        missing = args.db if args.db is not None else DEFAULT_DATA_DIR
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
        print(f"  conversation '{conv_id}' is in no known store.")
        if not found_anywhere:
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
        from lib.log_redaction import sanitize_value
        safe_settings = sanitize_value(settings, max_items=100,
                                       max_string_chars=2_000)
        print(f'  settings:   {json.dumps(safe_settings, ensure_ascii=False)}')

    messages = document.get('messages') or []
    print(f'\n== Transcript == ({len(messages)} message(s)'
          f'{"" if args.full or args.raw else f", head {HEAD_MESSAGES} + tail {TAIL_MESSAGES}"})')
    for line in _render_transcript(document, full=args.full, raw=args.raw):
        print(line)

    print(f'\n== Compaction Archives == ({len(archives)} archive(s)'
          f'{"" if args.full else f", latest {ARCHIVE_TAIL}"})')
    for line in _render_compaction_archives(archives, full=args.full):
        print(line)

    if not args.no_logs:
        print('\n== Logs ==')
        for line in _scan_logs(conv_id, max(1, args.logs)):
            print(line)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
