"""Backfill search_text/search_tsv for conversations with translatedContent.

Rationale:
  build_search_text() was updated to include messages[*].translatedContent
  (see routes/conversations.py).  Pre-existing rows have search_text that
  was computed WITHOUT translatedContent, so the translated (e.g. Chinese)
  text is absent from the FTS index — searching terms from the translation
  returns 0 results.

This script rebuilds search_text + search_tsv for every conversation whose
messages JSON contains the substring '"translatedContent"'.  It is idempotent
and safe to run multiple times.

Usage:
    python debug/backfill_translated_search.py             # dry-run (prints counts)
    python debug/backfill_translated_search.py --apply     # actually write
"""

import argparse
import json
import logging
import sys
import time

# Ensure project root on path when run directly
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.database import DOMAIN_CHAT, get_thread_db, _BACKEND
from lib.log import get_logger
from routes.conversations import build_search_text

logger = get_logger('backfill_translated_search')
# Always echo to stdout for interactive runs
_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logger.addHandler(_console)
logger.setLevel(logging.INFO)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true',
                        help='Actually write changes (default: dry-run)')
    args = parser.parse_args()

    db = get_thread_db(DOMAIN_CHAT)

    # Find conversations whose messages contain translatedContent
    is_pg = _BACKEND in ('pg', 'postgres', 'postgresql')
    if is_pg:
        rows = db.execute(
            "SELECT id, user_id, messages::text AS m, search_text "
            "FROM conversations "
            "WHERE messages::text LIKE %s",
            ('%"translatedContent"%',)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id, user_id, messages AS m, search_text "
            "FROM conversations "
            "WHERE messages LIKE ?",
            ('%"translatedContent"%',)
        ).fetchall()

    logger.info('[backfill] Found %d conversations with translatedContent', len(rows))

    changed = 0
    unchanged = 0
    errors = 0
    t0 = time.monotonic()

    for row in rows:
        conv_id = row['id']
        try:
            messages = json.loads(row['m'] or '[]')
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning('[backfill] Failed to parse messages for %s: %s', conv_id, e)
            errors += 1
            continue

        new_search_text = build_search_text(messages)
        old_search_text = row['search_text'] or ''

        if new_search_text == old_search_text:
            unchanged += 1
            continue

        delta = len(new_search_text) - len(old_search_text)
        logger.info('[backfill] conv=%s: search_text %d -> %d chars (+%d)',
                    conv_id, len(old_search_text), len(new_search_text), delta)

        if not args.apply:
            changed += 1
            continue

        try:
            if is_pg:
                db.execute(
                    "UPDATE conversations "
                    "SET search_text = %s, "
                    "    search_tsv = to_tsvector('simple', left(%s, 50000)) "
                    "WHERE id = %s AND user_id = %s",
                    (new_search_text, new_search_text, conv_id, row['user_id'])
                )
                db.commit()
            else:
                db.execute(
                    "UPDATE conversations SET search_text = ? "
                    "WHERE id = ? AND user_id = ?",
                    (new_search_text, conv_id, row['user_id'])
                )
                # Refresh SQLite FTS5 index too
                db.execute(
                    "INSERT OR REPLACE INTO conversations_fts (rowid, search_text) "
                    "SELECT rowid, ? FROM conversations WHERE id = ?",
                    (new_search_text, conv_id)
                )
                db.commit()
            changed += 1
        except Exception as e:
            logger.error('[backfill] Failed to update %s: %s', conv_id, e, exc_info=True)
            errors += 1

    elapsed = time.monotonic() - t0
    mode = 'APPLIED' if args.apply else 'DRY-RUN'
    logger.info('[backfill] %s — changed=%d unchanged=%d errors=%d elapsed=%.2fs',
                mode, changed, unchanged, errors, elapsed)
    if not args.apply and changed > 0:
        logger.info('[backfill] Re-run with --apply to actually write changes.')


if __name__ == '__main__':
    main()
