#!/usr/bin/env python3
"""Report upstream model releases the Model registry cannot publish yet.

Scheduler predicate contract (``condition_command``): exit 0 when new release
candidates exist, 1 when none (the first run records a baseline), 2 when no
live public catalogue is cached or the watch state is unreadable — never
evidence that nothing was released. ``--review`` and ``--baseline`` are
commands, not predicates: they exit 0 once done and 2 when refused. Reads the
cached feeds; never fetches.

  python3 scripts/model_release_watch.py             # concise report
  python3 scripts/model_release_watch.py --json      # full candidate leads
  python3 scripts/model_release_watch.py --baseline  # standing listings
  python3 scripts/model_release_watch.py --review google/example \\
      --decision deferred --reason "no official pricing yet" --source https://...

Run it from the deployment checkout whose data/ holds the feed cache. The
intake procedure is docs/MODEL_REGISTRATION.md "New-model intake workflow";
the policy lives in lib/model_catalog/release_watch.py.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.model_catalog.release_watch import (  # noqa: E402
    DECISIONS, MAX_REASON_CHARS, MAX_SOURCE_URLS, ReleaseWatchError, baseline_identities,
    record_review, release_watch_report,
)

EXIT_NEW, EXIT_NONE, EXIT_UNAVAILABLE = 0, 1, 2


def _price(pricing) -> str:
    if not isinstance(pricing, dict):
        return ''
    return f" relay ${pricing.get('input')}/${pricing.get('output')} per 1M {pricing.get('currency', '')}"


def _lines(candidate: dict) -> list[str]:
    missing = ', '.join(field.split(' ')[0] for field in candidate['missing'])
    lines = [f"NEW {candidate['id']}  missing: {missing}"]
    for lead in candidate['leads']:
        wires = ','.join(lead['wire_model_ids']) or '-'
        caps = ','.join(lead.get('capabilities', []))
        lines.append(f"    {lead['template']} {wires} ctx={lead.get('context_window', '?')} "
                     f"caps={caps}{_price(lead.get('actual_pricing'))}")
    return lines


def render(report: dict) -> str:
    feeds = ', '.join(
        f"{key} {datetime.fromtimestamp(feed['fetched_at']).isoformat(timespec='minutes')}"
        + (' (stale)' if feed['stale'] else '')
        for key, feed in report['feeds'].items())
    if report['status'] == 'unavailable':
        return ('release watch: no live public catalogue is cached; the daily model '
                'intelligence update refreshes it. Not evidence that nothing was released.')
    if report['status'] == 'initialized':
        return (f"release watch: baseline initialized with {report['baseline']} standing "
                f"listings; nothing new yet. Feeds: {feeds}")
    lines = [f"release watch: {len(report['new'])} new, {len(report['deferred'])} deferred, "
             f"{report['baseline']} baseline. Feeds: {feeds}"]
    for candidate in report['new']:
        lines.extend(_lines(candidate))
    for candidate in report['deferred']:
        review = candidate['review']
        lines.append(f"DEFERRED {candidate['id']} ({review['checked_at']}): {review['reason']}")
    return '\n'.join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n', 1)[0])
    parser.add_argument('--json', action='store_true', help='print the full report as JSON')
    parser.add_argument('--baseline', action='store_true', help='list baseline identities')
    parser.add_argument('--review', metavar='CREATOR/MODEL', help='record a decision for a candidate')
    parser.add_argument('--decision', choices=DECISIONS)
    parser.add_argument('--reason',
                        help=f'why it is not registered (at most {MAX_REASON_CHARS} characters)')
    parser.add_argument('--source', action='append', default=[], metavar='URL',
                        help=f'checked evidence HTTPS URL (repeatable, at most {MAX_SOURCE_URLS})')
    args = parser.parse_args(argv)
    try:
        if args.review:
            if not args.decision:
                parser.error('--review requires --decision and --reason')
            review = record_review(args.review, args.decision, args.reason, args.source)
            print(json.dumps({args.review: review}, ensure_ascii=False, indent=2))
            return 0
        if args.baseline:
            print('\n'.join(baseline_identities()))
            return 0
        report = release_watch_report()
    except ReleaseWatchError as exc:
        print(f'release watch: {exc}', file=sys.stderr)
        return EXIT_UNAVAILABLE
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report))
    if report['status'] == 'unavailable':
        return EXIT_UNAVAILABLE
    return EXIT_NEW if report['new'] else EXIT_NONE


if __name__ == '__main__':
    raise SystemExit(main())
