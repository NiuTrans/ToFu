#!/usr/bin/env python3
"""Check or activate a verified PostgreSQL-to-SQLite reverse export.

Validation is offline and read-only. Activation additionally requires an
explicit owner acknowledgement, a TLS-verified external PostgreSQL DSN secret
file, fresh-session read-only policy, and zero other client sessions. The
command never deletes PostgreSQL data or the previous SQLite authority.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, urlsplit


_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.storage_sidecar import offline_maintenance as _PG_TOOLING  # noqa: E402
from lib.storage_sidecar.cutover import (  # noqa: E402
    SQLiteCutoverError,
    activate_candidate,
    validate_candidate,
)
_MAX_SECRET_BYTES = 16 * 1024


def _dsn_uses_verified_tls(dsn: str) -> bool:
    if dsn.startswith(('postgres://', 'postgresql://')):
        values = parse_qs(urlsplit(dsn).query).get('sslmode', ())
        return bool(values and values[-1].lower() == 'verify-full')
    match = re.search(
        r'(?:^|\s)sslmode\s*=\s*["\']?([^\s"\']+)', dsn,
        flags=re.IGNORECASE,
    )
    return bool(match and match.group(1).lower() == 'verify-full')


def _read_dsn_secret(path: str | os.PathLike[str]) -> str:
    secret_path = Path(path).expanduser()
    if not secret_path.is_absolute():
        raise SQLiteCutoverError(
            'PostgreSQL DSN secret file must be an absolute path')
    try:
        resolved = secret_path.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise SQLiteCutoverError(
            'PostgreSQL DSN secret file is unreadable') from exc
    if not resolved.is_file() or not 0 < metadata.st_size <= _MAX_SECRET_BYTES:
        raise SQLiteCutoverError(
            'PostgreSQL DSN secret file must contain 1..16384 bytes')
    try:
        value = resolved.read_text(encoding='utf-8').strip()
    except (OSError, UnicodeError) as exc:
        raise SQLiteCutoverError(
            'PostgreSQL DSN secret file is not readable UTF-8') from exc
    if not value or '\x00' in value:
        raise SQLiteCutoverError('PostgreSQL DSN secret file is invalid')
    if not _dsn_uses_verified_tls(value):
        raise SQLiteCutoverError(
            'external PostgreSQL DSN requires sslmode=verify-full')
    return value


def _pg_quiescence(dsn: str) -> dict:
    connection = _PG_TOOLING.open_postgres_tool_connection(
        dsn,
        connect_timeout=10,
        application_name='tofu-sqlite-cutover-check',
    )
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute('SHOW default_transaction_read_only')
            read_only = str(cursor.fetchone()[0]).strip().lower() == 'on'
            cursor.execute("""
                SELECT pid, application_name, state
                  FROM pg_stat_activity
                 WHERE datname=current_database()
                   AND pid <> pg_backend_pid()
                   AND backend_type='client backend'
                 ORDER BY pid
            """)
            peers = [tuple(row) for row in cursor.fetchall()]
        return {
            'default_transaction_read_only': read_only,
            'other_client_sessions': len(peers),
            'peer_sample': peers[:10],
        }
    finally:
        connection.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--report', required=True)
    parser.add_argument('--canonical', default=str(_ROOT / 'data' / 'tofu.db'))
    parser.add_argument(
        '--postgres-dsn-file',
        default=os.environ.get('TOFU_POSTGRES_DSN_FILE', ''),
        help='absolute TLS-verified external PostgreSQL DSN secret file; '
             'required only with --apply',
    )
    parser.add_argument(
        '--apply', action='store_true',
        help='perform atomic promotion; omitted means check only',
    )
    parser.add_argument(
        '--owner-approved', action='store_true',
        help='literal acknowledgement required with --apply',
    )
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    data_dir = (_ROOT / 'data').resolve()
    evidence = validate_candidate(args.candidate, args.report, data_dir)
    result = {'candidate': evidence, 'mode': 'check'}
    if args.apply:
        if not args.owner_approved:
            raise SQLiteCutoverError('--apply requires --owner-approved')
        if not args.postgres_dsn_file:
            raise SQLiteCutoverError(
                '--apply requires --postgres-dsn-file or '
                'TOFU_POSTGRES_DSN_FILE')
        postgres = _pg_quiescence(
            _read_dsn_secret(args.postgres_dsn_file))
        result['postgresql'] = postgres
        if not postgres['default_transaction_read_only']:
            raise SQLiteCutoverError(
                'PostgreSQL fresh sessions are not read-only; refusing cutover')
        if postgres['other_client_sessions']:
            raise SQLiteCutoverError(
                'PostgreSQL still has '
                f'{postgres["other_client_sessions"]} other client session(s); '
                'stop the application and migrator first')
        result['authority'] = activate_candidate(
            candidate=args.candidate,
            report_path=args.report,
            canonical_path=args.canonical,
            data_dir=data_dir,
            owner_approved=True,
            source_still_read_only=True,
        )
        result['mode'] = 'applied'
        result['next_step'] = (
            'Set TOFU_DEPLOYMENT_MODE=personal and TOFU_PROCESS_ROLE=all; '
            'remove TOFU_POSTGRES_DSN_FILE, TOFU_REDIS_URL_FILE, '
            'TOFU_REPLICA_ID, TOFU_DISTRIBUTED_PREVIEW_MODE, '
            'every TOFU_DB_* / CHATUI_DB_* variable, TOFU_REQUIRE_PG, and '
            'TOFU_REPLICA_RING before starting the application. Keep the '
            'external PostgreSQL authority and backups intact until owner '
            'sign-off.')
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SQLiteCutoverError as exc:
        print(f'cutover refused: {exc}', file=sys.stderr)
        raise SystemExit(2)
