#!/usr/bin/env python3
"""Rename the production `chatui` PostgreSQL database to `tofu`.

WHY THIS EXISTS
---------------
The app's default PG database name was renamed `chatui` -> `tofu`
(see lib/database/_core.py and the tofu rebrand). The live data still lived
in a database literally named `chatui`, while the running server — finding no
TOFU_PG_DBNAME override — auto-bootstrapped a fresh, near-empty `tofu`
database and wrote a handful of new conversations into it.

This script makes `tofu` the single authoritative database by:
  1. Backing up `chatui` with pg_dump (safety net).
  2. Renaming the misrouted `tofu` -> `tofu_misroute_bak` (kept, NOT dropped).
  3. ALTER DATABASE chatui RENAME TO tofu  (instant catalog rename, no copy).
  4. Re-importing the 5 unique misroute conversations (captured earlier to
     data/tofu_misroute_convs.json) into the now-canonical `tofu`.

PRECONDITION
------------
The Tofu application server MUST be stopped before running this. A RENAME
requires zero connections to BOTH `chatui` and `tofu`. The PostgreSQL server
process itself stays up — this script talks to it directly. The script
ABORTS (does nothing) if it detects live connections to either database.

USAGE
-----
    # after you stop server.py:
    python scripts/migrate_chatui_to_tofu.py          # dry-run: checks only
    python scripts/migrate_chatui_to_tofu.py --apply  # perform the migration

After it finishes, restart server.py normally (no env var needed — the
default `tofu` now holds all your data).
"""

import argparse
import getpass
import json
import os
import subprocess
import sys
import time

try:
    from scripts._database_leaf import load_database_leaf
except ModuleNotFoundError as exc:  # direct ``python scripts/...`` execution
    if exc.name != 'scripts':
        raise
    from _database_leaf import load_database_leaf


_PG_TOOLING = load_database_leaf('pg_tooling')

HOST = os.environ.get('TOFU_PG_HOST', '127.0.0.1')
PORT = os.environ.get('TOFU_PG_PORT', '15439')
USER = (os.environ.get('TOFU_PG_USER')
        or os.environ.get('PGUSER')
        or getpass.getuser())

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MISROUTE_JSON = os.path.join(BASE_DIR, 'data', 'tofu_misroute_convs.json')
BACKUP_SQL = os.path.join(BASE_DIR, 'data', 'chatui_pre_rename_backup.sql')

OLD_DB = 'chatui'
NEW_DB = 'tofu'
MISROUTE_BAK = 'tofu_misroute_bak'


def connect(dbname):
    return _PG_TOOLING.open_postgres_tool_connection(
        host=HOST, port=PORT, dbname=dbname, user=USER)


def active_connections(admin_cur, dbname):
    """Return count of connections to dbname other than our own."""
    admin_cur.execute(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = %s AND pid <> pg_backend_pid()",
        (dbname,),
    )
    return admin_cur.fetchone()[0]


def db_exists(admin_cur, dbname):
    admin_cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
    return admin_cur.fetchone() is not None


def preflight(admin_cur):
    problems = []
    if not db_exists(admin_cur, OLD_DB):
        problems.append(f"source database '{OLD_DB}' does not exist")
    if not db_exists(admin_cur, NEW_DB):
        problems.append(f"current '{NEW_DB}' database does not exist (unexpected)")
    if db_exists(admin_cur, MISROUTE_BAK):
        problems.append(
            f"'{MISROUTE_BAK}' already exists — a previous run may be incomplete; "
            f"inspect and drop it manually before retrying"
        )
    for db in (OLD_DB, NEW_DB):
        if db_exists(admin_cur, db):
            n = active_connections(admin_cur, db)
            if n:
                problems.append(
                    f"database '{db}' has {n} active connection(s) — "
                    f"stop the app server first"
                )
    if not os.path.exists(MISROUTE_JSON):
        problems.append(f"misroute backup not found: {MISROUTE_JSON}")
    return problems


def run_pg_dump():
    print(f"[1/4] pg_dump {OLD_DB} -> {BACKUP_SQL} ...")
    cmd = [
        'pg_dump', '-h', HOST, '-p', str(PORT), '-U', USER,
        '-d', OLD_DB, '-f', BACKUP_SQL,
    ]
    t0 = time.time()
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print("pg_dump FAILED:", res.stderr, file=sys.stderr)
        sys.exit(1)
    size = os.path.getsize(BACKUP_SQL)
    print(f"      backup written ({size/1e6:.1f} MB) in {time.time()-t0:.1f}s")


def rename_databases(admin_conn):
    admin_conn.autocommit = True
    cur = admin_conn.cursor()
    print(f"[2/4] ALTER DATABASE {NEW_DB} RENAME TO {MISROUTE_BAK}")
    cur.execute(f'ALTER DATABASE "{NEW_DB}" RENAME TO "{MISROUTE_BAK}"')
    print(f"[3/4] ALTER DATABASE {OLD_DB} RENAME TO {NEW_DB}")
    cur.execute(f'ALTER DATABASE "{OLD_DB}" RENAME TO "{NEW_DB}"')
    print("      rename complete")


def reimport_misroute_convs():
    print(f"[4/4] re-importing misroute conversations into {NEW_DB} ...")
    with open(MISROUTE_JSON) as f:
        rows = json.load(f)
    conn = connect(NEW_DB)
    conn.autocommit = False
    cur = conn.cursor()
    inserted = 0
    skipped = 0
    for r in rows:
        # ON CONFLICT DO NOTHING on the composite PK (id, user_id) —
        # never clobber an existing conv with the same identity.
        cur.execute(
            """INSERT INTO conversations
                 (id, user_id, title, messages, created_at, updated_at,
                  settings, msg_count, search_text, search_tsv)
               VALUES (%s, %s, %s, %s::jsonb, %s, %s,
                       %s::jsonb, %s, %s, %s::tsvector)
               ON CONFLICT (id, user_id) DO NOTHING""",
            (
                r['id'], r['user_id'], r['title'], r['messages'],
                r['created_at'], r['updated_at'], r['settings'],
                r['msg_count'], r['search_text'], r['search_tsv'],
            ),
        )
        if cur.rowcount:
            inserted += 1
        else:
            skipped += 1
    conn.commit()
    cur.execute("SELECT count(*) FROM conversations")
    total = cur.fetchone()[0]
    conn.close()
    print(f"      inserted {inserted}, skipped {skipped} (already present)")
    print(f"      {NEW_DB}.conversations now has {total} rows")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true',
                    help='actually perform the migration (default: dry-run)')
    args = ap.parse_args()

    admin = connect('postgres')
    admin.autocommit = True
    acur = admin.cursor()

    problems = preflight(acur)
    if problems:
        print("PREFLIGHT FAILED:")
        for p in problems:
            print("  -", p)
        sys.exit(2)
    print("Preflight OK: both DBs connection-free, backup present, no stale bak DB.")

    if not args.apply:
        print("\nDRY-RUN only. Re-run with --apply to perform the migration.")
        return

    run_pg_dump()
    rename_databases(admin)
    reimport_misroute_convs()
    print("\nDONE. Restart server.py — it will use the canonical 'tofu' database.")
    print(f"Old misrouted DB preserved as '{MISROUTE_BAK}' (drop it once verified).")


if __name__ == '__main__':
    main()
