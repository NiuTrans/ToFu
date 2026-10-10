#!/usr/bin/env python3
"""Capture/check native progress decisions against the actual Python owner.

Entry: --check refuses fixture drift; no provider, storage or runtime policy I/O.
Only supported native admitted call shapes are captured. Digests are private:
decisions/counters, rather than cross-runtime hash encodings, are the contract.
"""
import argparse
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lib.agent_core.progress_ledger import ProgressLedgerV2  # noqa: E402
TARGET = ROOT / 'packages/tofu-runtime/tests/fixtures/python_progress.json'


def generate():
    def step(arguments='{}', **kwargs):
        return {'calls': [{'id': 'response-local', 'function': {'name': 'read', 'arguments': arguments}}],
                'world_version': '', 'evidence_ids': ['visible'], 'verification': '',
                'evidence_complete': True, 'failures': [], **kwargs}
    cases = [
        [step() for _ in range(5)],
        [step(), step(evidence_ids=['new']), step(evidence_ids=['new']), step(world_version='v2'), step(world_version='v2', verification='OK')],
        [step(), step(), step(evidence_complete=False), step(), step()],
        [step('{"a":1,"b":"中文"}'), step(' {"b":"中文","a":1} '), step('{"a":1.0,"b":"中文"}'), step('{"a":1e0,"b":"中文"}')],
        [step(evidence_ids=['b', 'a', 'a']), step(evidence_ids=['a', 'b']), step(evidence_ids=['b']), step(evidence_ids=[])],
        [step(failures=[' permission ', 'permission']), step('{"tab":2}', failures=['permission']), step(failures=[]), step(failures=['permission']), step(failures=['other'])],
        [step(failures=['\x1c拒绝\x1f']), step(failures=['拒绝']), step(failures=['  '])],
        [step(), step(verification='passed'), step(verification='success'), step(verification='verified'), step(verification='ok')],
    ]
    for sequence in cases:
        ledger = ProgressLedgerV2()
        for row in sequence:
            decision = ledger.observe(row['calls'], world_version=row['world_version'], evidence_ids=row['evidence_ids'], verification=row['verification'], evidence_complete=row['evidence_complete'])
            failure = ledger.observe_nonretryable_failures(row['failures'])
            row['expected_no_progress'] = decision['noProgressStreak']
            row['expected_failure'] = failure['nonretryableFailureStreak']
    return json.dumps({'format': 'tofu.native-progress-oracle/v1', 'cases': cases}, ensure_ascii=False, indent=2) + '\n'


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--check', action='store_true'); args = parser.parse_args()
    value = generate()
    if args.check:
        if TARGET.read_text() != value: raise SystemExit('native progress oracle drift')
    else: TARGET.write_text(value)


if __name__ == '__main__': main()
