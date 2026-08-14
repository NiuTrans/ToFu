"""Atomic semantic mutations for ``paper_reports`` structured metadata."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable

from lib.database._core import write_transaction
from lib.database.scoped_sequences import lock_scoped_sequence
from lib.log import get_logger


logger = get_logger(__name__)


def mutate_paper_report_meta(
    db,
    paper_hash: str,
    lang: str,
    mutator: Callable[[dict], dict | None],
) -> dict | None:
    """Serialize one read/modify/write of a report's JSON metadata.

    The scoped lock is portable across SQLite processes and PostgreSQL hosts.
    ``mutator`` runs inside the short transaction and must only mutate/return
    the supplied dict; it must not perform network or filesystem work.
    ``None`` means the report row does not exist.
    """
    if not paper_hash or not lang:
        raise ValueError('paper_hash and lang must not be empty')
    if not callable(mutator):
        raise TypeError('mutator must be callable')

    scope_key = hashlib.sha256(
        f'{paper_hash}\0{lang}'.encode('utf-8')).hexdigest()
    with write_transaction(db, label='mutate paper report metadata'):
        lock_scoped_sequence(db, 'paper_report_meta', scope_key)
        row = db.execute(
            'SELECT meta FROM paper_reports WHERE paper_hash=? AND lang=?',
            (paper_hash, lang),
        ).fetchone()
        if row is None:
            return None
        raw = row['meta'] if hasattr(row, 'keys') else row[0]
        if isinstance(raw, dict):
            current = copy.deepcopy(raw)
        else:
            try:
                current = json.loads(raw or '{}')
            except (json.JSONDecodeError, TypeError) as exc:
                logger.debug('[PaperReports] malformed stored metadata: %s', exc)
                current = {}
        if not isinstance(current, dict):
            current = {}

        replacement = mutator(current)
        updated = current if replacement is None else replacement
        if not isinstance(updated, dict):
            raise TypeError('paper report metadata mutator must return dict or None')
        cursor = db.execute(
            'UPDATE paper_reports SET meta=? WHERE paper_hash=? AND lang=?',
            (json.dumps(updated, ensure_ascii=False), paper_hash, lang),
        )
        if getattr(cursor, 'rowcount', 1) == 0:
            return None
        return copy.deepcopy(updated)


__all__ = ['mutate_paper_report_meta']
