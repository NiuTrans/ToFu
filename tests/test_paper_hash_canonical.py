"""Tests for the paper-identity canonical hash + the fork backfill
(epic pt_c9a103fe — owner-locked 2026-07-25: "最长期最优雅").

The fork: ingest stored ``_paper_hash(raw parsed_text)`` while report-start
stripped the client text before hashing — so ~70% of papers (any parsed text
with trailing whitespace) got TWO identities and ``has_report`` / the podcast
gate keyed on the library hash was falsely false (live-DB evidence: 15/21
library rows; reports ALWAYS under the strip hash).

Layers:
  1. canonical-hash unit (strip-insensitive, idempotent, '' for empty);
  2. resolve_paper_hash unit (prefer client hash / fall back to text /
     reject traversal-shaped client hash);
  3. SQLite end-to-end: seed the REAL fork shape (library row under the raw
     hash, report + translation + podcast rows under the strip hash,
     collision cases both directions, on-disk figure dir under the old
     hash), assert the gate is FALSE before the heal (the bug, i.e. the
     failing-first / NEUTER arm), run the backfill, assert the gate turns
     TRUE, dependents moved with the newer-row-wins collision rule, the
     asset dir renamed, and a second run is a no-op;
  4. static wiring pin: report-start resolves the identity via
     resolve_paper_hash (never re-derives when the client presents one).

Run:  pytest tests/test_paper_hash_canonical.py -m unit
"""
from __future__ import annotations

import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


def _legacy_raw_hash(text: str) -> str:
    """The PRE-FIX hash (no strip) — what ingest stored before the fix."""
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:32]


# ═══ 1. canonical hash ═══

class TestCanonicalPaperHash:
    def test_strip_insensitive(self):
        from lib.paper.hashing import _paper_hash
        assert _paper_hash('  abc def \n\n') == _paper_hash('abc def')

    def test_idempotent(self):
        """strip is idempotent, so hashing an already-stripped text equals
        hashing the raw one — the property that lets raw/stripped call sites
        converge on ONE identity."""
        from lib.paper.hashing import _paper_hash
        raw = 'body text\n\n'
        assert _paper_hash(raw) == _paper_hash(raw.strip())

    def test_empty_returns_empty(self):
        from lib.paper.hashing import _paper_hash
        assert _paper_hash('') == ''
        assert _paper_hash('   \n\t ') == ''
        assert _paper_hash(None) == ''

    def test_matches_manual_strip_sha(self):
        from lib.paper.hashing import _paper_hash
        text = 'the quick brown paper\n\n'
        expect = hashlib.sha256(text.strip().encode('utf-8')).hexdigest()[:32]
        assert _paper_hash(text) == expect


# ═══ 2. resolve_paper_hash ═══

class TestResolvePaperHash:
    def test_prefers_valid_client_hash(self):
        from lib.paper.hashing import resolve_paper_hash
        assert resolve_paper_hash('a1b2c3d4e5f6', 'some text') == 'a1b2c3d4e5f6'

    def test_falls_back_to_text_hash_when_absent(self):
        from lib.paper.hashing import resolve_paper_hash, _paper_hash
        assert resolve_paper_hash('', 'some text\n') == _paper_hash('some text\n')
        assert resolve_paper_hash(None, 'some text\n') == _paper_hash('some text\n')

    def test_rejects_traversal_shaped_client_hash(self):
        """A non-hex / traversal client value must NOT be trusted — fall
        back to the text hash instead of keying (or path-joining) on it."""
        from lib.paper.hashing import resolve_paper_hash, _paper_hash
        assert resolve_paper_hash('../../etc/passwd', 'some text') == \
            _paper_hash('some text')
        assert resolve_paper_hash('zz-not-hex', 'some text') == \
            _paper_hash('some text')


# ═══ 3. SQLite end-to-end fork heal ═══

class TestForkBackfillE2E:
    """Seed the live-DB fork shape and drive the REAL bootstrap backfill.

    Runtime report gates now live behind the Storage Sidecar and must not open
    this bootstrap connection. The local row-existence checks below retain the
    same fork/heal negative control without crossing those process owners.
    """

    @pytest.fixture()
    def fresh_db(self, tmp_path):
        from lib.database import reset_sqlite_for_tests, restore_db_state
        snapshot = reset_sqlite_for_tests(str(tmp_path / 'hashfork.db'))
        try:
            yield
        finally:
            restore_db_state(snapshot)

    @staticmethod
    def _seed_paper(db, text, *, report=True, translation=False, podcast=False):
        """Seed one forked paper: library under the legacy RAW hash, artifacts
        under the strip (canonical) hash — the live-DB shape. Returns
        (paper_id, stored_hash, canonical_hash)."""
        from lib.paper.hashing import _paper_hash
        pid = 'p-' + hashlib.sha256(text.encode()).hexdigest()[:8]
        stored = _legacy_raw_hash(text)
        canonical = _paper_hash(text)
        assert stored != canonical, 'test text must be strip-divergent'
        db.execute(
            'INSERT INTO paper_library (id, user_id, paper_hash, parsed_text,'
            ' created_at, updated_at) VALUES (?, 1, ?, ?, 1, 1)',
            (pid, stored, text))
        if report:
            db.execute(
                'INSERT INTO paper_reports (paper_hash, lang, report,'
                ' created_at) VALUES (?, ?, ?, 1)',
                (canonical, 'zh', 'REPORT-' + pid))
        if translation:
            db.execute(
                'INSERT INTO paper_translations (paper_hash, lang, text,'
                ' created_at) VALUES (?, ?, ?, 1)',
                (canonical, 'en', 'TRANS-' + pid))
        if podcast:
            db.execute(
                'INSERT INTO paper_podcasts (paper_hash, mode, lang, voice,'
                ' status, created_at, updated_at)'
                " VALUES (?, 'short', 'zh', '', 'done', 1, 1)",
                (canonical,))
        db.commit()
        return pid, stored, canonical

    def test_heal_rekeys_library_and_gate_flips(self, fresh_db, tmp_path,
                                                monkeypatch):
        from lib.database import get_thread_db
        import lib.paper.hash_backfill as hb
        db = get_thread_db()

        text = '# Paper A\n\nSome body text about attention.\n\n  \n'
        pid, stored, canonical = self._seed_paper(
            db, text, report=True, translation=True, podcast=True)

        # On-disk figure dir under the OLD hash.
        img_base = tmp_path / 'img'
        (img_base / stored).mkdir(parents=True)
        (img_base / stored / 'manifest.json').write_text('{}')
        monkeypatch.setattr(hb, 'PAPER_IMG_DIR', str(img_base))
        monkeypatch.setattr(hb, 'PAPER_DIR', str(tmp_path / 'papers'))

        # ── NEUTER arm (pre-heal): the gate is FALSE for the forked paper —
        #    the user-visible bug ("已有报告却提示生成报告").
        def _has_local_report(paper_hash):
            return bool(db.execute(
                'SELECT 1 FROM paper_reports WHERE paper_hash = ? LIMIT 1',
                (paper_hash,)).fetchone())

        assert _has_local_report(stored) is False
        assert _has_local_report(canonical) is True  # report exists, wrong key

        stats = hb.backfill_paper_hash_canonical(db, force=True)

        assert stats['rekeyed'] == 1, stats
        row = db.execute(
            'SELECT paper_hash FROM paper_library WHERE id = ?', (pid,),
        ).fetchone()
        assert row['paper_hash'] == canonical
        # The gate now sees the paper's report under the (healed) library hash.
        assert _has_local_report(canonical) is True
        # Dependents are re-keyed OFF the old hash (they were seeded under
        # canonical already — the common live shape — so nothing should
        # remain under `stored` anywhere).
        for table in ('paper_reports', 'paper_translations', 'paper_podcasts'):
            leftover = db.execute(
                f'SELECT count(*) AS n FROM {table} WHERE paper_hash = ?',
                (stored,)).fetchone()
            assert leftover['n'] == 0, table
        # On-disk dir renamed to the canonical hash.
        assert not os.path.exists(img_base / stored)
        assert (img_base / canonical / 'manifest.json').exists()
        assert stats['dirs_renamed'] == 1
        # Second run is a no-op (idempotent).
        stats2 = hb.backfill_paper_hash_canonical(db, force=True)
        assert stats2['rekeyed'] == 0 and stats2['dependents_moved'] == 0

    def test_collision_newer_row_wins_both_directions(self, fresh_db):
        from lib.database import get_thread_db
        import lib.paper.hash_backfill as hb
        db = get_thread_db()

        # Paper B: fork-row NEWER (created 100) than canonical row (50) →
        # the fork's content wins the canonical key.
        text_b = '# Paper B\n\nTransformers are all you need.\n\n'
        pid_b, stored_b, canon_b = self._seed_paper(db, text_b, report=False)
        db.execute('INSERT INTO paper_reports (paper_hash, lang, report,'
                   ' created_at) VALUES (?, ?, ?, 100)', (stored_b, 'zh', 'FORK-B'))
        db.execute('INSERT INTO paper_reports (paper_hash, lang, report,'
                   ' created_at) VALUES (?, ?, ?, 50)', (canon_b, 'zh', 'STALE-B'))
        # Paper C: canonical row NEWER (100) → fork row (50) is dropped.
        text_c = '# Paper C\n\nSparse routing is fun.\n\n'
        pid_c, stored_c, canon_c = self._seed_paper(db, text_c, report=False)
        db.execute('INSERT INTO paper_reports (paper_hash, lang, report,'
                   ' created_at) VALUES (?, ?, ?, 50)', (stored_c, 'zh', 'FORK-C'))
        db.execute('INSERT INTO paper_reports (paper_hash, lang, report,'
                   ' created_at) VALUES (?, ?, ?, 100)', (canon_c, 'zh', 'FRESH-C'))
        db.commit()

        stats = hb.backfill_paper_hash_canonical(db, force=True)
        assert stats['rekeyed'] == 2, stats

        row_b = db.execute('SELECT report, created_at FROM paper_reports'
                           ' WHERE paper_hash = ? AND lang = ?',
                           (canon_b, 'zh')).fetchone()
        assert row_b['report'] == 'FORK-B' and row_b['created_at'] == 100
        row_c = db.execute('SELECT report, created_at FROM paper_reports'
                           ' WHERE paper_hash = ? AND lang = ?',
                           (canon_c, 'zh')).fetchone()
        assert row_c['report'] == 'FRESH-C' and row_c['created_at'] == 100
        for old in (stored_b, stored_c):
            n = db.execute('SELECT count(*) AS n FROM paper_reports'
                           ' WHERE paper_hash = ?', (old,)).fetchone()
            assert n['n'] == 0

    def test_flag_gates_the_scan(self, fresh_db):
        """Boot path (force=False): once the flag is written the scan is
        skipped — a converged deployment pays one schema_meta read per boot."""
        from lib.database import get_thread_db
        import lib.paper.hash_backfill as hb
        db = get_thread_db()
        text = '# Paper D\n\nFlag gating body.\n\n'
        self._seed_paper(db, text, report=True)
        # reset_sqlite_for_tests → init_db already ran the (empty) backfill
        # and set the flag — clear it to simulate a pre-heal deployment.
        db.execute("DELETE FROM schema_meta WHERE key = 'paper_hash_canonical_v1'")
        db.commit()
        first = hb.backfill_paper_hash_canonical(db)  # no force — sets flag
        assert first['rekeyed'] == 1
        second = hb.backfill_paper_hash_canonical(db)
        assert second['skipped'] == 'already done'

    def test_missing_table_is_graceful_skip(self, tmp_path):
        """Fresh-install shape: paper_library absent → skip WITHOUT setting
        the flag (the next boot retries against the real tables)."""
        import sqlite3
        from lib.paper.hash_backfill import backfill_paper_hash_canonical

        class _DB:  # minimal wrapper exposing the wrapper API the backfill uses
            def __init__(self, raw):
                self._conn = raw
            def execute(self, sql, params=None):
                cur = self._conn.cursor()
                cur.execute(sql, params or ())
                class _R:
                    def fetchone(self_inner):
                        row = cur.fetchone()
                        if row is None:
                            return None
                        cols = [d[0] for d in cur.description]
                        return dict(zip(cols, row))
                    def fetchall(self_inner):
                        cols = [d[0] for d in cur.description]
                        return [dict(zip(cols, r)) for r in cur.fetchall()]
                    rowcount = cur.rowcount
                return _R()
            def commit(self):
                self._conn.commit()
            def rollback(self):
                self._conn.rollback()

        raw = sqlite3.connect(str(tmp_path / 'bare.db'))
        stats = backfill_paper_hash_canonical(_DB(raw))
        assert stats['skipped'] == 'no paper_library table'


# ═══ 4. static wiring pins ═══

def test_report_start_prefers_client_hash_wiring():
    """The fork's code-side root: report-start must resolve the identity via
    resolve_paper_hash (prefer the client-presented ingest-minted hash), not
    re-derive it from the payload text."""
    src = open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'routes', 'paper.py'), encoding='utf-8').read()
    assert "phash = resolve_paper_hash(data.get('paper_hash'), paper_text)" in src, \
        'report-start no longer prefers the client-presented hash — the fork is back'


def test_ingest_paths_use_canonical_hash_wiring():
    """Ingest (upload + arXiv) mints the identity — via the canonical
    _paper_hash (strip-insensitive since this epic)."""
    from lib.paper.hashing import _paper_hash
    raw = 'parsed text with trailing whitespace\n\n  '
    assert _paper_hash(raw) == _paper_hash(raw.strip())


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-x', '-q', '-m', 'unit']))
