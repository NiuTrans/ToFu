#!/usr/bin/env python3
"""Check or activate a quiesced, verified PostgreSQL→SQLite candidate.

The default is read-only validation.  Activation requires both ``--apply``
and the literal ``--owner-approved`` flag, rechecks that PostgreSQL fresh
sessions remain read-only, and refuses while any other database client session
is connected.  PostgreSQL data is never deleted; the prior ``data/tofu.db`` is
atomically archived for rollback.
"""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import json
import os
from pathlib import Path
import sys

try:
    from scripts._database_leaf import load_database_leaf
except ModuleNotFoundError as exc:  # direct ``python scripts/...`` execution
    if exc.name != 'scripts':
        raise
    from _database_leaf import load_database_leaf


_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    # Needed only for the leaf module's normal ``lib.log`` dependency. This
    # does not import lib.database or run backend discovery.
    sys.path.insert(0, str(_ROOT))
# Import the leaf module by file path. ``import lib.database.sqlite_cutover``
# would execute lib/database/__init__.py first, which performs backend discovery
# and may attach to/start PostgreSQL. A check-only/help maintenance command must
# have zero database lifecycle side effects before it explicitly probes PG.
_CUTOVER_PATH = _ROOT / 'lib' / 'database' / 'sqlite_cutover.py'
_SPEC = importlib.util.spec_from_file_location(
    'tofu_sqlite_cutover_leaf', _CUTOVER_PATH)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - install damage
    raise RuntimeError(f'cannot load cutover module: {_CUTOVER_PATH}')
_CUTOVER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CUTOVER)
_PG_TOOLING = load_database_leaf('pg_tooling')
SQLiteCutoverError = _CUTOVER.SQLiteCutoverError
activate_candidate = _CUTOVER.activate_candidate
validate_candidate = _CUTOVER.validate_candidate


def _env() -> dict[str, str]:
    values: dict[str, str] = {}
    path = _ROOT / '.env'
    if path.exists():
        for raw in path.read_text(encoding='utf-8', errors='replace').splitlines():
            line = raw.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key = key.strip()
            if key.startswith('TOFU_PG_'):
                values[key] = value.strip().strip('"').strip("'")
    for key in tuple(values) + (
            'TOFU_PG_HOST', 'TOFU_PG_PORT', 'TOFU_PG_DBNAME',
            'TOFU_PG_USER', 'TOFU_PG_PASSWORD'):
        if key in os.environ:
            values[key] = os.environ[key]
    return values


def _pg_quiescence() -> dict:
    env = _env()
    conn = _PG_TOOLING.open_postgres_tool_connection(
        host=env.get('TOFU_PG_HOST', '127.0.0.1'),
        port=int(env.get('TOFU_PG_PORT', '15432')),
        dbname=env.get('TOFU_PG_DBNAME', 'tofu'),
        user=env.get('TOFU_PG_USER', getpass.getuser()),
        password=env.get('TOFU_PG_PASSWORD', ''),
        connect_timeout=10,
        application_name='tofu-sqlite-cutover-check')
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute('SHOW default_transaction_read_only')
            read_only = str(cur.fetchone()[0]).strip().lower() == 'on'
            cur.execute("""
                SELECT pid, application_name, state
                  FROM pg_stat_activity
                 WHERE datname=current_database()
                   AND pid <> pg_backend_pid()
                   AND backend_type='client backend'
                 ORDER BY pid
            """)
            peers = [tuple(row) for row in cur.fetchall()]
        return {
            'default_transaction_read_only': read_only,
            'other_client_sessions': len(peers),
            'peer_sample': peers[:10],
        }
    finally:
        conn.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--report', required=True)
    parser.add_argument('--canonical', default=str(_ROOT / 'data' / 'tofu.db'))
    parser.add_argument('--apply', action='store_true',
                        help='perform atomic promotion; omitted means check only')
    parser.add_argument('--owner-approved', action='store_true',
                        help='literal acknowledgement required with --apply')
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    data_dir = (_ROOT / 'data').resolve()
    evidence = validate_candidate(args.candidate, args.report, data_dir)
    result = {'candidate': evidence, 'mode': 'check'}
    if args.apply:
        if not args.owner_approved:
            raise SQLiteCutoverError('--apply requires --owner-approved')
        pg = _pg_quiescence()
        result['postgresql'] = pg
        if not pg['default_transaction_read_only']:
            raise SQLiteCutoverError(
                'PostgreSQL fresh sessions are not read-only; refusing cutover')
        if pg['other_client_sessions']:
            raise SQLiteCutoverError(
                f'PostgreSQL still has {pg["other_client_sessions"]} other '
                'client session(s); stop the application/migrator first')
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
            'Set TOFU_DB_BACKEND=sqlite and start the application; keep '
            'PostgreSQL/data/pgdata intact for rollback until owner sign-off.')
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SQLiteCutoverError as exc:
        print(f'cutover refused: {exc}', file=sys.stderr)
        raise SystemExit(2)
