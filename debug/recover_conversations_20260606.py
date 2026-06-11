#!/usr/bin/env python3
"""One-shot recovery: merge the 2026-06-06 PG-corruption backups into the live tofu DB.

Sources (all READ-ONLY here; we only WRITE to the live tofu DB on 15432):
  1. scratch_recovery DB (15432) — restored from data/conversations_backup_20260606_1936.dump
     (2704 conversations recovered from the corrupt 15439 instance).
  2. data/merge_backup_15439.json — 6 convs; 5 are unique (not in dump/live), incl. the
     auto-verifier conv mq272wvf0af0r9.

Target: live tofu DB (15432). Insert with ON CONFLICT (id, user_id) DO NOTHING so the
18 existing (newer) live convs are preserved untouched. Composite PK is (id, user_id).

NEVER connects to port 15439 (corrupt instance — big inserts spin uninterruptibly).

Usage:
  python debug/recover_conversations_20260606.py            # DRY RUN (counts only)
  python debug/recover_conversations_20260606.py --apply    # perform the merge
"""
import json
import sys

import psycopg2
import psycopg2.extras

PORT = 15432
USER = 'your-user'
TARGET_DB = 'tofu'
SCRATCH_DB = 'scratch_recovery'
MERGE_JSON = 'data/merge_backup_15439.json'
MERGE_UNIQUE_IDS = [
    'mpxlbnb09s6vdi', 'mpxlezwjipdqog', 'mq24fwpa1jkbeo',
    'mq24h3a42vgt2y', 'mq272wvf0af0r9',
]

COLS = ['id', 'user_id', 'title', 'messages', 'created_at',
        'updated_at', 'settings', 'msg_count', 'search_text']


def connect(db):
    c = psycopg2.connect(f'host=127.0.0.1 port={PORT} dbname={db} user={USER} connect_timeout=10')
    return c


def _strip_nul_obj(o):
    """Recursively remove actual NUL chars from a decoded JSON object."""
    if isinstance(o, str):
        return o.replace('\x00', '')
    if isinstance(o, list):
        return [_strip_nul_obj(x) for x in o]
    if isinstance(o, dict):
        return {_strip_nul_obj(k): _strip_nul_obj(v) for k, v in o.items()}
    return o


def sanitize_json_text(s):
    """Make a JSON text safe for PG jsonb by removing NUL escapes the bulletproof way:
    parse (decodes \\u0000 -> NUL), strip NUL recursively, re-serialize. Avoids the
    blunt-replace trap that corrupts escaped-backslash sequences."""
    if s is None:
        return None
    if '\\u0000' not in s and '\x00' not in s:
        return s
    return json.dumps(_strip_nul_obj(json.loads(s)), ensure_ascii=False)


def rows_from_scratch(conn):
    # Rows come from a clean pg_restore of valid jsonb — guaranteed NUL-free and
    # well-formed. Insert AS-IS; do NOT sanitize (blunt replaces corrupt valid escapes).
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id,user_id,title,messages::text AS messages,created_at,"
                "updated_at,settings::text AS settings,msg_count,search_text "
                "FROM conversations")
    out = []
    for r in cur.fetchall():
        out.append(dict(r))
    cur.close()
    return out


def rows_from_merge():
    data = {c['id']: c for c in json.load(open(MERGE_JSON, encoding='utf-8'))}
    out = []
    for cid in MERGE_UNIQUE_IDS:
        c = data[cid]
        msgs = c['messages'] if isinstance(c['messages'], str) else json.dumps(c['messages'], ensure_ascii=False)
        settings = c.get('settings')
        if settings is not None and not isinstance(settings, str):
            settings = json.dumps(settings, ensure_ascii=False)
        out.append({
            'id': c['id'], 'user_id': c.get('user_id', 1), 'title': c.get('title'),
            'messages': sanitize_json_text(msgs),
            'created_at': c.get('created_at'), 'updated_at': c.get('updated_at'),
            'settings': sanitize_json_text(settings), 'msg_count': c.get('msg_count'),
            'search_text': c.get('search_text'),
        })
    return out


def main():
    apply = '--apply' in sys.argv

    scratch = connect(SCRATCH_DB)
    scratch_rows = rows_from_scratch(scratch)
    scratch.close()
    merge_rows = rows_from_merge()

    tgt = connect(TARGET_DB)
    tgt.autocommit = True  # per-row isolation: a failed row never rolls back prior successes
    cur = tgt.cursor()
    cur.execute("SELECT count(*) FROM conversations")
    before = cur.fetchone()[0]

    all_rows = scratch_rows + merge_rows
    print(f"Target tofu BEFORE: {before} conversations")
    print(f"Candidates: {len(scratch_rows)} from dump + {len(merge_rows)} from merge-backup "
          f"= {len(all_rows)} (ON CONFLICT (id,user_id) DO NOTHING)")

    if not apply:
        print("\nDRY RUN — no writes. Re-run with --apply to perform the merge.")
        tgt.close()
        return

    sql = (
        "INSERT INTO conversations (id,user_id,title,messages,created_at,updated_at,"
        "settings,msg_count,search_text) VALUES "
        "(%(id)s,%(user_id)s,%(title)s,%(messages)s::jsonb,%(created_at)s,%(updated_at)s,"
        "%(settings)s::jsonb,%(msg_count)s,%(search_text)s) "
        "ON CONFLICT (id,user_id) DO NOTHING"
    )
    inserted = 0
    failed = []
    for row in all_rows:
        try:
            cur.execute(sql, row)
            inserted += cur.rowcount
        except Exception as e:
            failed.append((row['id'], str(e)[:160]))
            continue

    cur.execute("SELECT count(*) FROM conversations")
    after = cur.fetchone()[0]
    print(f"\nTarget tofu AFTER: {after} conversations (+{after - before})")
    print(f"Insert attempts that added rows: {inserted}")
    if failed:
        print(f"\n{len(failed)} rows FAILED:")
        for cid, err in failed[:20]:
            print(f"  {cid}: {err}")
    # verify the auto-verifier + key recovered convs landed
    for cid in ['mq272wvf0af0r9', 'mptuvb5hdjxanp']:
        cur.execute("SELECT id,left(title,40),length(messages::text) FROM conversations WHERE id=%s", (cid,))
        print('verify', cid, '->', cur.fetchone())
    tgt.close()


if __name__ == '__main__':
    main()
