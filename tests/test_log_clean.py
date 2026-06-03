"""tests/test_log_clean.py — port-parity tests for lib.log_clean.

Each test exercises a specific cleaning pass with a fixture that
matches the JS implementation's behaviour. These fixtures are the
contract — if a test fails, either the Python port diverged from the
JS or the JS itself changed and the port needs to track it.
"""

from __future__ import annotations

import unittest

from lib.log_clean import (
    CleaningResult, _collapse_blank_lines, _collapse_progress_bars,
    _collapse_similar_lines, _deduplicate_worker_blocks,
    _extract_device_ids, _fingerprint, _format_device_range,
    _is_tqdm_line, _shorten_paths, detect_log_noise,
)


class HelpersTest(unittest.TestCase):

    def test_extract_device_ids_unique_sorted(self):
        ids = _extract_device_ids([
            'cuda:3 starting',
            'Worker 1 init',
            'cuda:1 done',
            'GPU: 0 ready',
            'cuda:3 keep',
            # rank=N is NOT recognised — separator must be ':' or '_'
            # to match the regex (parity with JS impl).
            'rank:2 hello',
        ])
        self.assertEqual(ids, [0, 1, 2, 3])

    def test_format_device_range(self):
        self.assertEqual(_format_device_range([]), '')
        self.assertEqual(_format_device_range([0]), '0')
        self.assertEqual(_format_device_range([0, 1, 2, 3]), '0-3')
        self.assertEqual(_format_device_range([0, 1, 3, 4, 7]), '0-1, 3-4, 7')

    def test_fingerprint_normalises_instance_noise(self):
        a = _fingerprint('Connection from 10.0.0.1:8443 ref=0xDEADBEEF tid=1234567')
        b = _fingerprint('Connection from 192.168.1.5:9000 ref=0xCAFEBABE tid=9999999')
        self.assertEqual(a, b)

    def test_fingerprint_keeps_quoted_messages_distinct(self):
        a = _fingerprint('error: "disk full"')
        b = _fingerprint('error: "out of memory"')
        self.assertNotEqual(a, b)

    def test_shorten_paths_skips_short(self):
        line = 'in /a/b/c/d/foo.py at line 5'  # 4 segments → not shortened
        self.assertEqual(_shorten_paths(line), line)

    def test_shorten_paths_collapses_long(self):
        line = ('File "/very/long/path/to/some/deeply/nested/folder/'
                'lib/foo.py", line 42')
        out = _shorten_paths(line)
        self.assertIn('.../lib/foo.py', out)
        self.assertLess(len(out), len(line))

    def test_is_tqdm_line(self):
        self.assertTrue(_is_tqdm_line(
            ' 50%|████      | 50/100 [00:05<00:05,  9.50it/s]'))
        # Not a tqdm — no rate-tail.
        self.assertFalse(_is_tqdm_line(
            ' 50%|████      | 50/100 [Worker 0] hello'))
        # Not a tqdm — no progress bar.
        self.assertFalse(_is_tqdm_line('50% complete'))


class CollapseProgressBarsTest(unittest.TestCase):

    def _make_bars(self, n: int) -> list:
        return [f' {pct}%|{"█"*(pct//10)} | {pct}/100 '
                f'[00:00<00:00, 5.00it/s]'
                for pct in range(0, 101, max(1, 100 // (n - 1)))][:n]

    def test_keeps_short_runs_intact(self):
        bars = self._make_bars(3)
        out, dropped = _collapse_progress_bars(bars)
        self.assertEqual(out, bars)
        self.assertEqual(dropped, 0)

    def test_collapses_long_runs(self):
        bars = self._make_bars(20)
        out, dropped = _collapse_progress_bars(bars)
        # Should keep ~3 picks + 1 summary line
        self.assertLess(len(out), len(bars))
        self.assertGreater(dropped, 0)
        self.assertTrue(any('more progress updates' in l for l in out))

    def test_preserves_non_progress_lines(self):
        text = ['hello'] + self._make_bars(20) + ['done']
        out, _ = _collapse_progress_bars(text)
        self.assertEqual(out[0], 'hello')
        self.assertEqual(out[-1], 'done')


class CollapseSimilarLinesTest(unittest.TestCase):

    def test_consecutive_run(self):
        lines = [f'Connection from 10.0.0.{i} done' for i in range(10)]
        out, dropped = _collapse_similar_lines(lines)
        self.assertEqual(dropped, 9)
        self.assertEqual(len(out), 2)  # first + summary
        self.assertIn('more similar', out[1])

    def test_below_threshold_kept(self):
        lines = ['hello world'] * 4
        out, dropped = _collapse_similar_lines(lines)
        self.assertEqual(dropped, 0)
        self.assertEqual(len(out), 4)

    def test_scattered_duplicates(self):
        lines = (['Connection from 1.2.3.4 ok'] * 3
                  + ['unrelated']
                  + ['Connection from 5.6.7.8 ok'] * 3
                  + ['done'])
        # Total fingerprint count is 6 ≥ 5 → Pass B fires
        out, dropped = _collapse_similar_lines(lines)
        # Should keep just the first occurrence + summary; rest dropped.
        self.assertGreater(dropped, 0)


class CollapseBlankLinesTest(unittest.TestCase):

    def test_consecutive_blanks_collapse(self):
        lines = ['a', '', '', '', 'b', '', 'c']
        out = _collapse_blank_lines(lines)
        self.assertEqual(out, ['a', '', 'b', '', 'c'])


class DeduplicateWorkerBlocksTest(unittest.TestCase):

    def test_identical_worker_tracebacks_dedup(self):
        lines = [
            '(Worker_A pid=1) Traceback (most recent call last):',
            '(Worker_A pid=1)   File "foo.py", line 1, in <module>',
            '(Worker_A pid=1) RuntimeError: oh no',
            '(Worker_B pid=2) Traceback (most recent call last):',
            '(Worker_B pid=2)   File "foo.py", line 1, in <module>',
            '(Worker_B pid=2) RuntimeError: oh no',
        ]
        out, deduped, count = _deduplicate_worker_blocks(lines)
        self.assertEqual(deduped, 1)
        self.assertEqual(count, 2)
        # Second worker's lines should be gone
        self.assertNotIn('(Worker_B pid=2) Traceback (most recent call last):',
                          out)

    def test_single_worker_passthrough(self):
        lines = ['(Worker_A pid=1) hello'] * 3
        out, deduped, count = _deduplicate_worker_blocks(lines)
        self.assertEqual(deduped, 0)
        self.assertEqual(count, 1)
        self.assertEqual(out, lines)


class DetectLogNoiseTest(unittest.TestCase):

    def test_empty_input_returns_none(self):
        self.assertIsNone(detect_log_noise(''))
        self.assertIsNone(detect_log_noise('hello'))
        self.assertIsNone(detect_log_noise('a\nb\nc'))  # < 5 lines

    def test_no_noise_returns_none(self):
        text = '\n'.join([f'line {i}' for i in range(20)])
        self.assertIsNone(detect_log_noise(text))

    def test_python_log_prefix_stripped(self):
        text = '\n'.join([
            'INFO 2026-01-01 10:00:00,000 module.foo Starting up',
            'INFO 2026-01-01 10:00:01,000 module.foo Connecting',
            'INFO 2026-01-01 10:00:02,000 module.foo Connected',
            'INFO 2026-01-01 10:00:03,000 module.foo Working',
            'INFO 2026-01-01 10:00:04,000 module.foo Done',
        ] * 3)  # 15 lines so threshold trips
        result = detect_log_noise(text)
        self.assertIsNotNone(result)
        # Prefixes gone
        self.assertNotIn('INFO 2026-01-01', result.cleanedText)
        # Operations recorded
        op_names = {op.name for op in result.ops}
        self.assertIn('prefix', op_names)

    def test_http_access_log_noise_removed(self):
        text = '\n'.join([
            '127.0.0.1 - - [03/Mar/2026 00:00:00] "GET /a HTTP/1.1" 200 -',
            '127.0.0.1 - - [03/Mar/2026 00:00:01] "GET /b HTTP/1.1" 200 -',
            '127.0.0.1 - - [03/Mar/2026 00:00:02] "GET /c HTTP/1.1" 200 -',
            '127.0.0.1 - - [03/Mar/2026 00:00:03] "GET /d HTTP/1.1" 200 -',
            '127.0.0.1 - - [03/Mar/2026 00:00:04] "GET /e HTTP/1.1" 200 -',
            'REAL ERROR: something broke',
        ])
        result = detect_log_noise(text)
        self.assertIsNotNone(result)
        self.assertNotIn('"GET /a HTTP', result.cleanedText)
        self.assertIn('REAL ERROR', result.cleanedText)
        op_names = {op.name for op in result.ops}
        self.assertIn('noise', op_names)

    def test_pointer_lines_removed(self):
        text = '\n'.join([
            'def foo():',
            '    bar()',
            '    ^^^^',
            '~~~~~~~',
            '    baz()',
            '    ^^^^^',
            'plus more text',
            'and another',
        ])
        result = detect_log_noise(text)
        # Only pointer lines + small savings — may not trip threshold.
        # If it does trip, pointers must be gone.
        if result is not None:
            self.assertNotIn('^^^^', result.cleanedText)

    def test_below_savings_threshold_returns_none(self):
        # 5 short lines with one tiny prefix — savings < 8% → None
        text = '\n'.join([
            'a',
            'b',
            'c',
            '[2026-01-01T00:00:00] short',
            'd',
        ])
        self.assertIsNone(detect_log_noise(text))

    def test_progress_bar_collapse(self):
        bars = '\n'.join(
            f'{pct}%|{"█" * (pct // 5)} | {pct}/100 '
            f'[00:00<00:01,  5.0it/s]'
            for pct in range(0, 101, 5))
        # 21 lines of progress bars → must collapse
        result = detect_log_noise(bars)
        self.assertIsNotNone(result)
        self.assertGreater(result.progressBarsCollapsed, 0)
        self.assertIn('more progress updates', result.cleanedText)

    def test_serialisable_output(self):
        text = '\n'.join([
            'INFO 2026-01-01 10:00:00,000 module.foo Starting',
        ] * 30)
        result = detect_log_noise(text)
        self.assertIsNotNone(result)
        d = result.to_dict()
        # Mirrors the JS object's keys exactly
        for key in ('originalText', 'cleanedText', 'ops',
                     'prefixExample', 'prefixLabel',
                     'prefixLinesStripped', 'noiseLinesRemoved',
                     'pointerLinesRemoved', 'pathsShortenedCount',
                     'similarLinesCollapsed', 'progressBarsCollapsed',
                     'workersDeduplicated', 'workerCount',
                     'totalLines', 'savedChars', 'savedPct'):
            self.assertIn(key, d, f'missing key in to_dict: {key}')
        # ops must be a list of dicts with name+desc
        self.assertTrue(d['ops'])
        self.assertIn('name', d['ops'][0])
        self.assertIn('desc', d['ops'][0])

    def test_real_world_traceback(self):
        """A composite log: prefixes + worker dedup + noise + tracebacks."""
        text = '\n'.join([
            'INFO 2026-01-01 10:00:00,000 server Starting up',
            '127.0.0.1 - - [01/Jan/2026 10:00:00] "GET /api/health HTTP/1.1" 200 -',
            'INFO 2026-01-01 10:00:01,000 server Listening',
            '127.0.0.1 - - [01/Jan/2026 10:00:01] "GET /api/health HTTP/1.1" 200 -',
            '(Worker_0 pid=100) ERROR 01-01 10:00:02 [foo:42] CUDA out of memory',
            '(Worker_0 pid=100) Traceback (most recent call last):',
            '(Worker_0 pid=100)   File "model.py", line 1, in forward',
            '(Worker_0 pid=100) RuntimeError: CUDA out of memory',
            '(Worker_1 pid=101) ERROR 01-01 10:00:02 [foo:42] CUDA out of memory',
            '(Worker_1 pid=101) Traceback (most recent call last):',
            '(Worker_1 pid=101)   File "model.py", line 1, in forward',
            '(Worker_1 pid=101) RuntimeError: CUDA out of memory',
            '(Worker_2 pid=102) ERROR 01-01 10:00:02 [foo:42] CUDA out of memory',
            '(Worker_2 pid=102) Traceback (most recent call last):',
            '(Worker_2 pid=102)   File "model.py", line 1, in forward',
            '(Worker_2 pid=102) RuntimeError: CUDA out of memory',
            'INFO 2026-01-01 10:00:03,000 server Recovering',
        ])
        result = detect_log_noise(text)
        self.assertIsNotNone(result)
        # Dedup ran
        self.assertGreater(result.workersDeduplicated, 0)
        # HTTP noise removed
        self.assertNotIn('"GET /api/health', result.cleanedText)
        # Real ERROR survived
        self.assertIn('CUDA out of memory', result.cleanedText)


if __name__ == '__main__':
    unittest.main()
