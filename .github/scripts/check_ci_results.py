#!/usr/bin/env python3
"""Validate GitHub's toJSON(needs) from stdin for the terminal CI job.

Only explicit success passes. The workflow may name jobs deliberately skipped
on this event (nightly jobs on PR/push); failure/cancellation never qualify.
No dependencies or network. docs/TESTING_STRATEGY.md owns the delivery policy.
"""
from __future__ import annotations

import argparse
import json
import sys


def dependency_failures(needs: object, allowed_skips: set[str]) -> list[str]:
    """Return actionable failures, including malformed or empty evidence."""
    if not isinstance(needs, dict) or not needs:
        return ['missing or invalid dependency results']
    failures = [f'{name}: allowed skip has no dependency result'
                for name in sorted(allowed_skips - needs.keys())]
    for name, dependency in sorted(needs.items()):
        result = dependency.get('result') if isinstance(dependency, dict) else None
        if result == 'success' or (result == 'skipped' and name in allowed_skips):
            continue
        failures.append(f'{name}: {result or "missing result"}')
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-skipped', nargs='*', default=[])
    args = parser.parse_args()
    try:
        needs = json.load(sys.stdin)
    except (ValueError, OSError) as error:
        print(f'Invalid CI dependency evidence: {error}', file=sys.stderr)
        return 1
    failures = dependency_failures(needs, set(args.allow_skipped))
    if failures:
        print('CI dependencies did not succeed:\n' + '\n'.join(failures), file=sys.stderr)
        return 1
    print('All required CI dependencies succeeded.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
