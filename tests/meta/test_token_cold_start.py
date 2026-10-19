"""Fresh-process count-kernel receipts cannot be replaced with warm clocks."""
import copy
import unittest
from benchmarks.token_cold_start import ENCODINGS, summarize


def cohort():
    raw, pid = {}, 1
    for encoding in ENCODINGS:
        raw[encoding] = []
        for _ in range(100):
            record = {}
            for arm in ('baseline', 'candidate'):
                record[arm] = dict(encoding=encoding, pid=pid, cold_only=True,
                                   calls=0, cold_wall_ns=1000, cold_cpu_ns=1000)
                pid += 1
            raw[encoding].append(record)
    return raw


class ColdCountEvidenceTests(unittest.TestCase):
    def test_complete_independent_receipts_and_exact_guard_boundary(self):
        raw = cohort()
        for records in raw.values():
            for record in records:
                record['candidate']['cold_wall_ns'] = 1050
        summary, failures = summarize(raw)
        self.assertFalse(failures)
        self.assertEqual(summary['cl100k_base']['cold_wall_ns_p99']['ratio'], 1.05)

    def test_cpu_regression_is_not_hidden_by_wall_gain(self):
        raw = cohort()
        for record in raw['o200k_base']:
            record['candidate'].update(cold_wall_ns=800, cold_cpu_ns=1060)
        _, failures = summarize(raw)
        self.assertEqual({row['metric'] for row in failures},
                         {'cold_cpu_ns_p50', 'cold_cpu_ns_p95', 'cold_cpu_ns_p99', 'cold_cpu_ns_mean'})

    def test_insufficient_or_reused_process_receipts_refuse(self):
        raw = cohort()
        incomplete = copy.deepcopy(raw)
        incomplete['cl100k_base'].pop()
        with self.assertRaises(ValueError):
            summarize(incomplete)
        raw['o200k_base'][0]['candidate']['pid'] = raw['cl100k_base'][0]['baseline']['pid']
        with self.assertRaises(ValueError):
            summarize(raw)

    def test_warm_invalid_encoding_or_missing_clock_refuse(self):
        for change in (dict(calls=1), dict(cold_only=False), dict(encoding='wrong'),
                       dict(cold_wall_ns=0), dict(cold_cpu_ns=True)):
            with self.subTest(change=change):
                raw = cohort()
                raw['cl100k_base'][0]['candidate'].update(change)
                with self.assertRaises(ValueError):
                    summarize(raw)
