"""Cross-thread lost-update guard for structured paper report metadata."""

from __future__ import annotations

import concurrent.futures
import json
import threading
import time

import pytest


pytestmark = pytest.mark.unit


def test_concurrent_meta_mutations_preserve_every_sibling_update():
    from lib.database import (
        DOMAIN_CHAT,
        db_execute_with_retry,
        get_thread_db,
        mutate_paper_report_meta,
    )

    paper_hash = f'meta-race-{time.time_ns()}'
    lang = 'en'
    db = get_thread_db(DOMAIN_CHAT)
    db_execute_with_retry(
        db,
        'INSERT INTO paper_reports '
        '(paper_hash, lang, report, model, meta, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (paper_hash, lang, 'body', 'model', '{}', int(time.time())),
    )

    workers = 12
    barrier = threading.Barrier(workers)

    def _write(index):
        worker_db = get_thread_db(DOMAIN_CHAT)
        barrier.wait(timeout=10)

        def _mutate(meta):
            meta.setdefault('workers', {})[str(index)] = index
            return meta

        return mutate_paper_report_meta(
            worker_db, paper_hash, lang, _mutate)

    try:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers) as pool:
            results = list(pool.map(_write, range(workers)))
        assert all(isinstance(result, dict) for result in results)

        row = db.execute(
            'SELECT meta FROM paper_reports WHERE paper_hash=? AND lang=?',
            (paper_hash, lang),
        ).fetchone()
        persisted = json.loads(row['meta'])
        assert persisted['workers'] == {
            str(index): index for index in range(workers)}
    finally:
        db_execute_with_retry(
            db, 'DELETE FROM paper_reports WHERE paper_hash=? AND lang=?',
            (paper_hash, lang))


def test_meta_mutator_exception_rolls_back_json_change():
    from lib.database import (
        DOMAIN_CHAT,
        db_execute_with_retry,
        get_thread_db,
        mutate_paper_report_meta,
    )

    paper_hash = f'meta-rollback-{time.time_ns()}'
    lang = 'en'
    db = get_thread_db(DOMAIN_CHAT)
    db_execute_with_retry(
        db,
        'INSERT INTO paper_reports '
        '(paper_hash, lang, report, model, meta, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (paper_hash, lang, 'body', 'model', '{"stable": true}',
         int(time.time())),
    )

    def _fail(meta):
        meta['stable'] = False
        raise RuntimeError('stop before update')

    try:
        with pytest.raises(RuntimeError, match='stop before update'):
            mutate_paper_report_meta(db, paper_hash, lang, _fail)
        row = db.execute(
            'SELECT meta FROM paper_reports WHERE paper_hash=? AND lang=?',
            (paper_hash, lang),
        ).fetchone()
        assert json.loads(row['meta']) == {'stable': True}
    finally:
        db_execute_with_retry(
            db, 'DELETE FROM paper_reports WHERE paper_hash=? AND lang=?',
            (paper_hash, lang))
