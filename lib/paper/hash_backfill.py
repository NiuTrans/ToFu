"""lib/paper/hash_backfill.py — one-shot idempotent re-key of the paper
identity fork (epic pt_c9a103fe, owner-locked 2026-07-25: "最长期最优雅").

The fork (live-DB evidence: 15/21 library rows): ingest stored
``_paper_hash(raw parsed_text)`` while report-start hashed the client text
AFTER ``.strip()`` — so a paper whose parsed text carried trailing whitespace
(≈70% of the library) got TWO identities, reports landing under the strip
hash while the library / podcast gate (``has_report``) keyed on the ingest
hash and was falsely false. A third variant existed too (text mutated after
hash assignment).

The code-side fix (same commit): ``_paper_hash`` strip-canonicalizes input
and downstream routes prefer the client-presented ingest-minted hash
(``resolve_paper_hash``), so no NEW fork can form. This module heals the
EXISTING rows: for every ``paper_library`` row it recomputes the canonical
hash from the stored ``parsed_text`` and, when it differs from the stored
hash:

  1. UPDATEs the library row (CAS on the old hash);
  2. re-keys dependent rows in ``paper_reports`` / ``paper_translations`` /
     ``paper_podcasts`` from the old hash to the canonical one (collision →
     keep the newer row by ``created_at``, drop the older);
  3. renames the on-disk asset dirs (figure manifests under
     ``PAPER_IMG_DIR/<hash>``, podcast audio under ``PAPER_DIR/podcast/<hash>``)
     — non-destructively (an existing target wins, the source is left alone).

Idempotent by construction (a second run finds ``stored == canonical``
everywhere) AND flag-gated in ``schema_meta`` so the full library scan runs
at most once per deployment. Called from BOTH backend ``init_db`` paths
BEFORE the version fast-path (a converged DB must still heal). Boot passes
``force=False``; tests drive the healing directly with ``force=True``.
"""

from __future__ import annotations

import os

from lib.log import audit_log, get_logger
from lib.paper.hashing import PAPER_DIR, PAPER_IMG_DIR, _paper_hash

logger = get_logger(__name__)

_FLAG_KEY = 'paper_hash_canonical_v1'

# (table, rest-of-PK columns) — dependents whose PK starts with paper_hash.
_DEPENDENTS = (
    ('paper_reports', ('lang',)),
    ('paper_translations', ('lang',)),
    ('paper_podcasts', ('mode', 'lang', 'voice')),
)


def _read_flag(db) -> bool:
    try:
        row = db.execute(
            'SELECT value FROM schema_meta WHERE key = ?', (_FLAG_KEY,),
        ).fetchone()
        return bool(row and row['value'])
    except Exception as e:
        logger.debug('[Paper:HashBackfill] flag read failed (first install?): %s', e)
        return False


def _write_flag(db, count: int) -> None:
    from lib.database._core_schema import SCHEMA_META, upsert
    upsert(db, SCHEMA_META, {'key': _FLAG_KEY, 'value': str(count)}, commit=False)


def _rekey_dependents(db, old: str, new: str) -> int:
    """Move dependent rows from the old hash to the canonical one.

    Collision rule: the row with the newer ``created_at`` wins, the older is
    deleted (the same paper's content-addressed cache — never a data-loss
    decision, just which cached artifact to keep).
    """
    moved = 0
    for table, rest_cols in _DEPENDENTS:
        try:
            rest_csv = ', '.join(rest_cols)
            rows = db.execute(
                f'SELECT paper_hash, created_at, {rest_csv} '
                f'FROM {table} WHERE paper_hash = ?',
                (old,),
            ).fetchall()
        except Exception as e:
            logger.debug('[Paper:HashBackfill] scan %s failed: %s', table, e)
            continue
        for r in rows:
            conds = ' AND '.join(f'{c} = ?' for c in rest_cols)
            params = tuple(r[c] for c in rest_cols)
            clash = db.execute(
                f'SELECT created_at FROM {table} '
                f'WHERE paper_hash = ? AND {conds}',
                (new,) + params,
            ).fetchone()
            try:
                if clash and (clash['created_at'] or 0) >= (r['created_at'] or 0):
                    # Canonical row already exists and is newer — drop the fork.
                    db.execute(
                        f'DELETE FROM {table} WHERE paper_hash = ? AND {conds}',
                        (old,) + params,
                    )
                else:
                    if clash:
                        # Fork row is newer — evict the canonical stale row.
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
    """Rename on-disk hash-keyed asset dirs (figure manifests, podcast audio).

    Non-destructive: an existing target wins and the source dir is left in
    place (the next access re-derives under the canonical hash anyway).
    """
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
    """Re-key forked paper identities to the canonical strip-normalized hash.

    Args:
        db: project DB connection wrapper; defaults to the thread-local chat
            connection.
        force: bypass the schema_meta done-flag (tests drive the heal
            directly; boot is flag-gated so the scan runs at most once).

    Returns:
        Stats dict (rekeyed / dependents_moved / dirs_renamed / skipped).
        Never raises — boot must not die on a data migration.
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
            rows = db.execute(
                'SELECT id, user_id, paper_hash, parsed_text '
                'FROM paper_library WHERE parsed_text != ?', ('',),
            ).fetchall()
        except Exception as e:
            # Fresh install — tables not created yet (the full DDL pass runs
            # after this point in init_db). Skip WITHOUT setting the flag so
            # the next boot retries against the real tables.
            logger.debug('[Paper:HashBackfill] library scan failed '
                         '(first install?): %s', e)
            stats['skipped'] = 'no paper_library table'
            return stats

        for r in rows:
            stored = (r['paper_hash'] or '').strip()
            canonical = _paper_hash(r['parsed_text'])
            if not canonical or stored == canonical:
                continue
            # CAS on the old hash — a concurrent re-key by another worker
            # simply affects 0 rows and we move on.
            cur = db.execute(
                'UPDATE paper_library SET paper_hash = ? '
                'WHERE id = ? AND user_id = ? AND paper_hash = ?',
                (canonical, r['id'], r['user_id'], stored),
            )
            if getattr(cur, 'rowcount', 0):
                stats['rekeyed'] += 1
                stats['dependents_moved'] += _rekey_dependents(db, stored, canonical)
                stats['dirs_renamed'] += _rename_asset_dirs(stored, canonical)
                logger.info('[Paper:HashBackfill] re-keyed paper %s → %s '
                            '(library id=%s)', stored[:8], canonical[:8], r['id'])
            elif stored:
                # Someone else already re-keyed the library row — still move
                # any dependents left under the old hash.
                stats['dependents_moved'] += _rekey_dependents(db, stored, canonical)

        _write_flag(db, stats['rekeyed'])
        db.commit()
        if stats['rekeyed'] or stats['dependents_moved']:
            audit_log('paper_hash_canonical_backfill', **stats)
        logger.info('[Paper:HashBackfill] done: %s', stats)
    except Exception as e:
        logger.error('[Paper:HashBackfill] failed (non-fatal): %s', e, exc_info=True)
        try:
            db.rollback()
        except Exception as rb:
            logger.debug('[Paper:HashBackfill] rollback failed: %s', rb)
        stats['skipped'] = f'error: {e}'
    return stats


__all__ = ['backfill_paper_hash_canonical']
