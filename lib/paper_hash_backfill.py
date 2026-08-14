"""Dependency-light one-shot repair for historical paper hash forks.

Database schema initialization imports this module directly. It must remain
free of ``lib.paper`` imports so a DB-only command never initializes PDF,
ONNX, LLM, or swarm runtimes. ``lib.paper.hash_backfill`` aliases this module
for API compatibility.
"""

from __future__ import annotations

import os

from lib.log import audit_log, get_logger
from lib.paper_identity import PAPER_DIR, PAPER_IMG_DIR, _paper_hash

logger = get_logger(__name__)

_FLAG_KEY = 'paper_hash_canonical_v1'
_DEPENDENTS = (
    ('paper_reports', ('lang',)),
    ('paper_translations', ('lang',)),
    ('paper_podcasts', ('mode', 'lang', 'voice')),
)


def _read_flag(db) -> bool:
    # A brand-new database does not have ``schema_meta`` yet: this backfill is
    # deliberately invoked before DDL so it can also heal already-current
    # deployments.  Keep the expected first-install miss observable at DEBUG
    # without emitting a false ERROR from the connection wrapper.
    from lib.database import suppress_sql_error_log
    try:
        with suppress_sql_error_log():
            row = db.execute(
                'SELECT value FROM schema_meta WHERE key = ?', (_FLAG_KEY,),
            ).fetchone()
        return bool(row and row['value'])
    except Exception as e:
        logger.debug('[Paper:HashBackfill] flag read failed (first install?): %s', e)
        return False


def _write_flag(db, count: int) -> None:
    from lib.database import assert_write_transaction
    from lib.database._core_schema import SCHEMA_META, upsert
    assert_write_transaction(db, label='paper hash backfill flag')
    upsert(db, SCHEMA_META, {'key': _FLAG_KEY, 'value': str(count)}, commit=False)


def _rekey_dependents(db, old: str, new: str) -> int:
    from lib.database import assert_write_transaction
    assert_write_transaction(db, label='paper hash dependent rekey')
    moved = 0
    for table, rest_cols in _DEPENDENTS:
        try:
            rest_csv = ', '.join(rest_cols)
            rows = db.execute(
                f'SELECT paper_hash, created_at, {rest_csv} '
                f'FROM {table} WHERE paper_hash = ?', (old,),
            ).fetchall()
        except Exception as e:
            logger.debug('[Paper:HashBackfill] scan %s failed: %s', table, e)
            continue
        for r in rows:
            conds = ' AND '.join(f'{c} = ?' for c in rest_cols)
            params = tuple(r[c] for c in rest_cols)
            clash = db.execute(
                f'SELECT created_at FROM {table} '
                f'WHERE paper_hash = ? AND {conds}', (new,) + params,
            ).fetchone()
            try:
                if clash and (clash['created_at'] or 0) >= (r['created_at'] or 0):
                    db.execute(
                        f'DELETE FROM {table} WHERE paper_hash = ? AND {conds}',
                        (old,) + params,
                    )
                else:
                    if clash:
                        db.execute(
                            f'DELETE FROM {table} WHERE paper_hash = ? AND {conds}',
                            (new,) + params,
                        )
                    db.execute(
                        f'UPDATE {table} SET paper_hash = ? '
                        f'WHERE paper_hash = ? AND {conds}',
                        (new, old) + params,
                    )
                moved += 1
            except Exception as e:
                logger.warning('[Paper:HashBackfill] re-key %s %s→%s failed: %s',
                               table, old[:8], new[:8], e)
    return moved


def _rename_asset_dirs(old: str, new: str) -> int:
    renamed = 0
    for base in (PAPER_IMG_DIR, os.path.join(PAPER_DIR, 'podcast')):
        src = os.path.join(base, old)
        dst = os.path.join(base, new)
        try:
            if os.path.isdir(src) and not os.path.exists(dst):
                os.rename(src, dst)
                renamed += 1
        except OSError as e:
            logger.warning('[Paper:HashBackfill] dir rename %s→%s failed: %s',
                           src, dst, e)
    return renamed


def backfill_paper_hash_canonical(db=None, force: bool = False) -> dict:
    """Re-key stored paper identities and dependents to canonical hashes.

    Idempotent and flag-gated. Errors are contained because a data heal must
    never prevent core schema creation.
    """
    stats = {'rekeyed': 0, 'dependents_moved': 0, 'dirs_renamed': 0,
             'skipped': ''}
    try:
        if db is None:
            from lib.database import get_thread_db
            db = get_thread_db()
        if not force and _read_flag(db):
            stats['skipped'] = 'already done'
            return stats
        try:
            from lib.database import suppress_sql_error_log
            with suppress_sql_error_log():
                rows = db.execute(
                    'SELECT id, user_id, paper_hash, parsed_text '
                    'FROM paper_library WHERE parsed_text != ?', ('',),
                ).fetchall()
        except Exception as e:
            logger.debug('[Paper:HashBackfill] library unavailable: %s', e)
            stats['skipped'] = 'no paper_library table'
            return stats
        candidates = []
        for r in rows:
            stored = (r['paper_hash'] or '').strip()
            canonical = _paper_hash(r['parsed_text'])
            if not canonical or stored == canonical:
                continue
            candidates.append((r, stored, canonical))

        # Filesystem renames can block on a shared mount and must never run
        # while this process owns SQLite's single writer slot.  Doing them
        # before the database transaction is crash-convergent: if the process
        # stops here, the next run sees the old DB key, treats an already moved
        # directory as a no-op, then completes the same DB re-key.
        for _r, stored, canonical in candidates:
            stats['dirs_renamed'] += _rename_asset_dirs(stored, canonical)

        from lib.database import write_transaction
        with write_transaction(db, label='paper hash canonical backfill'):
            for r, stored, canonical in candidates:
                cur = db.execute(
                    'UPDATE paper_library SET paper_hash = ? '
                    'WHERE id = ? AND user_id = ? AND paper_hash = ?',
                    (canonical, r['id'], r['user_id'], stored),
                )
                if getattr(cur, 'rowcount', 0):
                    stats['rekeyed'] += 1
                    stats['dependents_moved'] += _rekey_dependents(
                        db, stored, canonical)
                    logger.info(
                        '[Paper:HashBackfill] re-keyed %s → %s (id=%s)',
                        stored[:8], canonical[:8], r['id'])
                elif stored:
                    stats['dependents_moved'] += _rekey_dependents(
                        db, stored, canonical)
            _write_flag(db, stats['rekeyed'])
        if stats['rekeyed'] or stats['dependents_moved']:
            audit_log('paper_hash_canonical_backfill', **stats)
        logger.info('[Paper:HashBackfill] done: %s', stats)
    except Exception as e:
        logger.error('[Paper:HashBackfill] failed (non-fatal): %s', e,
                     exc_info=True)
        stats['skipped'] = f'error: {e}'
    return stats


__all__ = ['backfill_paper_hash_canonical']
