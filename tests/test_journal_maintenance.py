"""tests/test_journal_maintenance.py — JOURNAL.md injection budgeting + rotation.

Covers :mod:`lib.project_mod.journal`:
  * split_entries: header / newest-first entry split
  * build_injection: newest entries verbatim + older entries as a title index,
    size roughly bounded regardless of file size
  * maybe_rotate: below-threshold no-op; above-threshold archives the OLDEST
    entries to .tofu/journal-archive/ and shrinks the live file, losing nothing
  * concurrency: a rotation racing a top-append never drops the appended entry

Run:  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_journal_maintenance.py
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest

import pytest

from lib.project_mod import journal as J


def _entry(date: str, title: str, body_kb: int = 0) -> str:
    filler = ('x' * 1000 + '\n') * body_kb
    return f'### {date} — {title}\n{filler}- detail for {title}\n\n'


def _journal(entries: list[str], header: str = '# Project Journal\n\nintro prose\n\n') -> str:
    return header + ''.join(entries)


@pytest.mark.unit
class SplitEntriesTest(unittest.TestCase):
    def test_split_header_and_entries_in_file_order(self):
        e_new = _entry('2026-07-18', 'newest')
        e_old = _entry('2026-06-01', 'oldest')
        header, entries = J.split_entries(_journal([e_new, e_old]))
        self.assertIn('intro prose', header)
        self.assertEqual(len(entries), 2)
        self.assertIn('newest', entries[0])   # newest-at-top preserved
        self.assertIn('oldest', entries[1])

    def test_no_entries_returns_whole_text_as_header(self):
        header, entries = J.split_entries('# Just a header\nnothing else\n')
        self.assertEqual(entries, [])
        self.assertIn('Just a header', header)


@pytest.mark.unit
class BuildInjectionTest(unittest.TestCase):
    def test_recent_verbatim_older_as_index(self):
        # All entries fat (~3KB) so the byte budget cleanly cuts the newest 3
        # into the verbatim window and pushes the rest into the title index.
        recent = [_entry(f'2026-07-1{i}', f'recent{i}', body_kb=3) for i in range(3)]
        older = [_entry('2026-05-01', f'old{i}', body_kb=3) for i in range(20)]
        text = _journal(recent + older)
        out = J.build_injection(text, recent_bytes=11_000, index_max=80)
        # Newest entry present in full (its filler body shows).
        self.assertIn('recent0', out)
        self.assertIn('detail for recent0', out)
        # Old entries appear as title-index lines, NOT full bodies.
        self.assertIn('- 2026-05-01 — old0', out)
        self.assertIn('older entries below are shown as an INDEX', out)
        # The oldest entry's body filler must NOT be injected verbatim.
        self.assertNotIn('detail for old0', out)

    def test_injection_size_bounded_regardless_of_file_size(self):
        # A giant file: 500 fat entries.
        entries = [_entry(f'2026-0{1 + i % 9}-01', f'e{i}', body_kb=4) for i in range(500)]
        text = _journal(entries)
        out = J.build_injection(text, recent_bytes=20_000, index_max=80)
        # Injected block is a tiny fraction of the multi-MB source.
        self.assertLess(len(out.encode('utf-8')), 60_000)
        self.assertLess(len(out), len(text))

    def test_index_capped_with_overflow_note(self):
        recent = [_entry('2026-07-18', 'r', body_kb=1)]
        older = [_entry('2026-05-01', f'old{i}') for i in range(200)]
        out = J.build_injection(_journal(recent + older), recent_bytes=3_000, index_max=50)
        self.assertIn('and', out)
        self.assertIn('more older entries', out)

    def test_single_huge_latest_entry_still_shown(self):
        big = _entry('2026-07-18', 'huge', body_kb=40)
        out = J.build_injection(_journal([big]), recent_bytes=1_000)
        self.assertIn('huge', out)   # kept even though it alone blows the budget


@pytest.mark.unit
class RotationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='journal-rot-')
        self.jpath = os.path.join(self._tmp, 'JOURNAL.md')

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write(self, text):
        with open(self.jpath, 'w', encoding='utf-8') as f:
            f.write(text)

    def _read(self):
        with open(self.jpath, encoding='utf-8') as f:
            return f.read()

    def test_below_threshold_is_noop(self):
        self._write(_journal([_entry('2026-07-18', 'small')]))
        res = J.maybe_rotate(self.jpath, self._tmp, threshold=1_000_000)
        self.assertIsNone(res)

    def test_rotation_archives_oldest_and_shrinks_live_file(self):
        # Newest-first: e0 newest … e29 oldest. Each ~2KB.
        entries = [_entry(f'2026-07-{29 - i:02d}', f'e{i}', body_kb=2) for i in range(30)]
        self._write(_journal(entries))
        before = os.path.getsize(self.jpath)
        n = J.maybe_rotate(self.jpath, self._tmp, threshold=10_000, keep_bytes=10_000)
        self.assertIsNotNone(n)
        self.assertGreater(n, 0)
        after = self._read()
        # Live file shrank and still holds the NEWEST entry.
        self.assertLess(len(after.encode('utf-8')), before)
        self.assertIn('e0', after)
        # The OLDEST entry moved out of the live file …
        self.assertNotIn('### 2026-07-00 — e29', after)  # oldest heading gone
        # … into a monthly archive under .tofu/journal-archive/.
        adir = os.path.join(self._tmp, '.tofu', 'journal-archive')
        self.assertTrue(os.path.isdir(adir))
        archived = ''.join(
            open(os.path.join(adir, f), encoding='utf-8').read()
            for f in os.listdir(adir))
        self.assertIn('e29', archived)

    def test_no_entry_lost_across_rotation(self):
        entries = [_entry(f'2026-07-{29 - i:02d}', f'e{i}', body_kb=2) for i in range(30)]
        self._write(_journal(entries))
        J.maybe_rotate(self.jpath, self._tmp, threshold=10_000, keep_bytes=10_000)
        live = self._read()
        adir = os.path.join(self._tmp, '.tofu', 'journal-archive')
        archived = ''.join(
            open(os.path.join(adir, f), encoding='utf-8').read()
            for f in os.listdir(adir))
        # Every entry's title survives SOMEWHERE (live or archive).
        for i in range(30):
            title = f'— e{i}\n'
            self.assertTrue(title in live or title in archived, f'e{i} lost')

    def test_header_preserved_in_live_file(self):
        entries = [_entry(f'2026-07-{29 - i:02d}', f'e{i}', body_kb=2) for i in range(30)]
        self._write(_journal(entries, header='# Project Journal\n\nSACRED HEADER\n\n'))
        J.maybe_rotate(self.jpath, self._tmp, threshold=10_000, keep_bytes=10_000)
        self.assertIn('SACRED HEADER', self._read())


@pytest.mark.unit
class ConcurrencyTest(unittest.TestCase):
    """A top-append landing during rotation must not be dropped."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='journal-conc-')
        self.jpath = os.path.join(self._tmp, 'JOURNAL.md')

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_concurrent_append_survives_rotation(self):
        entries = [_entry(f'2026-07-{29 - i:02d}', f'e{i}', body_kb=2) for i in range(30)]
        header = '# Project Journal\n\nintro\n\n'
        with open(self.jpath, 'w', encoding='utf-8') as f:
            f.write(header + ''.join(entries))

        def rotate():
            for _ in range(20):
                J.maybe_rotate(self.jpath, self._tmp, threshold=5_000, keep_bytes=8_000)

        def append():
            # Prepend a fresh newest entry via the same locked RMW discipline.
            from lib.json_store import _interprocess_lock, _path_lock, read_text, write_text_atomic
            for k in range(20):
                lk = _path_lock(self.jpath)
                with lk, _interprocess_lock(self.jpath):
                    cur = read_text(self.jpath, default='')
                    h, es = J.split_entries(cur)
                    new_entry = _entry('2026-08-01', f'appended{k}', body_kb=1)
                    write_text_atomic(self.jpath, h + new_entry + ''.join(es))

        t1 = threading.Thread(target=rotate)
        t2 = threading.Thread(target=append)
        t1.start(); t2.start()
        t1.join(); t2.join()

        # Final safety rotation to settle.
        J.maybe_rotate(self.jpath, self._tmp, threshold=5_000, keep_bytes=8_000)

        live = open(self.jpath, encoding='utf-8').read()
        adir = os.path.join(self._tmp, '.tofu', 'journal-archive')
        archived = ''
        if os.path.isdir(adir):
            archived = ''.join(
                open(os.path.join(adir, f), encoding='utf-8').read()
                for f in os.listdir(adir))
        # Every appended entry must survive somewhere.
        for k in range(20):
            tag = f'appended{k}\n'
            self.assertTrue(tag in live or tag in archived, f'appended{k} lost')


if __name__ == '__main__':
    unittest.main()
