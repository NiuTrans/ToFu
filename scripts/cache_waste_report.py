#!/usr/bin/env python3
"""Cache-waste distribution report — the reproducible form of a one-off audit.

WHY THIS EXISTS
===============
The 2026-07-29 cache-cost investigation produced its distribution table from
three throwaway shell one-liners. The conclusion was right, but the *process*
was not reproducible, and it was wrong twice on the way there:

  1. the denominator initially covered only part of the rotated logs;
  2. the price constants were hand-derived by dividing two audited totals, and
     came out 27x off;
  3. the percentile printer used ``if p50 else '-'``, so a legitimate
     ``gap_s == 0`` rendered as "missing" — which is precisely what hid 218
     cold-start rounds inside the ``no_break`` bucket until someone noticed the
     arithmetic did not add up.

All three are structural traps, not typos, so this script nails them down:

  * **Rates come from ``lib.cost.compute_cost``** — the same engine the product
    bills with. Never hand-write a CNY-per-token constant here; if the rate is
    wrong, it is wrong in exactly one place and the product is wrong with it.
  * **Non-waste classes are excluded from the recoverable denominator and
    reported separately.** A TTL expiry rebuilt an entry that expired on its
    own schedule; a cold start had no predecessor to read back. Neither is
    money we could have saved, and folding them in inflates the number people
    then go chasing.
  * **Percentiles treat 0 as a number.** ``gap_s == 0.0`` is a real, meaningful
    measurement (same-instant round-1), not a missing value.

USAGE
    python3 scripts/cache_waste_report.py                  # all rotated logs
    python3 scripts/cache_waste_report.py --glob 'logs/app.log'
    python3 scripts/cache_waste_report.py --min-write 20000 --json

The input is the ``[CacheRoundRecord]`` JSON line emitted once per LLM round by
lib/tasks_pkg/cache_tracking/_detect.py, which stamps its bucket via the
single-source ``classify_verdict``. Offline and live counts therefore cannot
drift.

TWO LIMITATIONS YOU MUST READ BEFORE QUOTING A NUMBER
=====================================================
1. **Buckets are stamped at write time, not re-derived here.** That is what
   keeps offline and live counts identical, but it means records written
   BEFORE a classifier change still carry the old bucket. Rounds logged before
   the 2026-07-29 ``ttl_expiry`` fix (commit b402b696) are still stamped
   ``body_change`` / ``other``; the ``ttl_expiry`` row only fills in as newer
   traffic accumulates. Do NOT patch that up by re-deriving the rule here — a
   second copy of the bucketing logic is exactly the drift this report exists
   to prevent. If you need history restated, replay the records through
   ``classify_verdict`` itself.
2. **Rows written before the ``model`` field are priced at ``--model``.** Each
   record now carries the model it ran on, so those rows are priced EXACTLY at
   their own rate and ``--model`` is only a fallback. But records are stamped at
   write time, so rows logged before that field existed will never gain one —
   for them ``--model`` still sets the price, and their CNY remains an
   approximation. The header states how many rows fell back, so you can see at
   a glance how much of a given table is exact. Token counts and round counts
   are always exact regardless.
"""

from __future__ import annotations

import argparse
import glob as _glob
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RECORD_MARKER = '[CacheRoundRecord]'

# Buckets that are NOT recoverable waste.
#
# ★ The name is IMPORTED from the production taxonomy, never retyped. A string
#   literal here keeps matching the OLD name after a rename in _detect.py,
#   silently moving TTL expiry back into the recoverable total — the exact
#   "the guard checked my own constant, not the real one" failure this report
#   exists to prevent. Measured: with the literal in place, renaming the
#   production constant put 900 CNY of a 1M-token TTL fixture back into
#   recoverable and nothing noticed.
try:
    from lib.tasks_pkg.cache_tracking._detect import BUCKET_TTL_EXPIRY
except Exception as _imp_err:  # pragma: no cover - fail loud, never guess
    raise SystemExit(
        'cannot import BUCKET_TTL_EXPIRY from the production taxonomy '
        f'({_imp_err}). Refusing to fall back to a hardcoded bucket name: a '
        'stale literal would silently misclassify TTL expiry as recoverable '
        'waste.')

NON_WASTE_BUCKETS = frozenset({BUCKET_TTL_EXPIRY})

COLD_START_LABEL = 'cold_start (gap=0, no predecessor)'

# Rate probe size. Large enough that per-token rounding inside the pricing
# engine is negligible when we divide back out.
_PROBE_TOKENS = 1_000_000


def derive_rates(model_id: str, provider_id: str | None = None):
    """Return (write_cny_per_token, read_cny_per_token) from the REAL engine.

    Derived by pricing a known token count through ``lib.cost.compute_cost``
    and dividing back out — so this script can never disagree with what the
    product actually charges. A hand-written constant here was the source of a
    27x error in the audit that motivated this file.
    """
    from lib.cost import compute_cost

    def _cny(usage):
        got = compute_cost(usage, model_id, provider_id)
        if not got:
            raise SystemExit(
                f'compute_cost returned nothing for model_id={model_id!r} — '
                'pass a --model that exists in the pricing table.')
        return float(got.get('costCny') or 0.0)

    # ── Reject an unrecognised model instead of pricing at the fallback rate.
    #   compute_cost() NEVER fails on an unknown id: it silently applies a
    #   default (0.13575 CNY/1k write — 3x Opus-5). Since this script prices
    #   EVERY row at one rate, accepting that fallback would triple the answer
    #   with no visible signal. A typo'd --model must stop the run, not quietly
    #   invent a number. This is the same class of error as the hand-derived
    #   constant that came out 27x off in the audit that motivated this file.
    _UNKNOWN_PROBE = 'zzz-nonexistent-model-probe'
    _fallback = compute_cost(
        {'prompt_tokens': 0, 'completion_tokens': 0,
         'cache_creation_input_tokens': _PROBE_TOKENS}, _UNKNOWN_PROBE, None)
    _fallback_cny = float((_fallback or {}).get('costCny') or 0.0)
    _actual_cny = _cny({'prompt_tokens': 0, 'completion_tokens': 0,
                        'cache_creation_input_tokens': _PROBE_TOKENS})
    if _fallback_cny and _actual_cny == _fallback_cny:
        raise SystemExit(
            f'model_id={model_id!r} is not in the pricing table — it priced at '
            f'the generic fallback rate ({_fallback_cny / _PROBE_TOKENS * 1000:.5f} '
            'CNY/1k), which would silently misprice every row of this report. '
            'Pass a model that is actually priced (e.g. claude-opus-5).')

    w = _actual_cny / _PROBE_TOKENS
    r = _cny({'prompt_tokens': 0, 'completion_tokens': 0,
              'cache_read_input_tokens': _PROBE_TOKENS}) / _PROBE_TOKENS
    if w <= 0:
        raise SystemExit(
            f'derived a non-positive cache-write rate ({w}) for {model_id!r}; '
            'the pricing table has no cache pricing for this model.')
    return w, r


def load_records(pattern: str, since: str = '', until: str = '') -> list[dict]:
    """Collect ``[CacheRoundRecord]`` payloads, optionally bounded by log time.

    ``since`` / ``until`` are compared as PREFIX strings against the log line's
    leading ``YYYY-MM-DD HH:MM:SS`` stamp, so any left-anchored precision works
    ('2026-07-29', '2026-07-29 14', '2026-07-29 14:30:00'). ``until`` is
    INCLUSIVE of the whole prefix it names.

    Why this exists: the log is live and still growing, so an unbounded run
    reports a moving total. A report that quotes a figure MUST be reproducible
    by whoever reads it, which means naming the window the figure came from.
    """
    out = []
    for fn in sorted(_glob.glob(pattern)):
        try:
            with open(fn, errors='ignore') as fh:
                for line in fh:
                    i = line.find(RECORD_MARKER)
                    if i == -1:
                        continue
                    # Leading 'YYYY-MM-DD HH:MM:SS' written by lib/log.py.
                    # A torn write during log rotation can leave NUL padding at
                    # the head of a line; such a stamp sorts BELOW every real
                    # date, so trusting it blanks the reported window and
                    # silently widens any --since filter. Only a stamp that
                    # actually looks like a date is usable.
                    stamp = line[:19]
                    if not (len(stamp) == 19 and stamp[:4].isdigit()
                            and stamp[4] == '-' and stamp[10] == ' '):
                        stamp = ''
                    if since and (not stamp or stamp < since):
                        continue
                    if until and (not stamp or not stamp <= until + '\uffff'):
                        continue
                    try:
                        rec = json.loads(line[i + len(RECORD_MARKER):].strip())
                    except ValueError:
                        continue
                    rec['_ts'] = stamp
                    out.append(rec)
        except OSError as e:
            print(f'warn: cannot read {fn}: {e}', file=sys.stderr)
    return out


def percentile(values, p):
    """Nearest-rank percentile. Returns None ONLY for an empty input.

    ``0.0`` is a legitimate value and must survive — a falsy check here is the
    exact bug that hid the cold-start rounds during the original audit.
    """
    if not values:
        return None
    ordered = sorted(values)
    return ordered[int(round((len(ordered) - 1) * p / 100.0))]


def bucket_of(rec: dict) -> str:
    """Report bucket for one round.

    Cold start is resolved HERE rather than trusted from the record: a round
    with no predecessor had nothing to read back, so its zero readback is not a
    miss regardless of which verdict bucket the live detector assigned it.
    """
    if float(rec.get('gap_s') or 0.0) == 0.0 and int(rec.get('call') or 0) <= 1:
        return COLD_START_LABEL
    return rec.get('bucket') or 'other'


_RATE_MEMO: dict = {}


def _rate_cache(model_id, provider_id=None, _memo=None):
    """Memoised (write, read) rate lookup, tolerant of unknown models.

    Returns None when the model is absent from the pricing table so the caller
    can fall back. This script is run over HISTORICAL logs, where a row may
    name a model that has since been renamed or removed — dying on one row
    would make the whole report unusable.

    The process-lifetime memo lives in ``_RATE_MEMO`` (not a mutable argument
    default); tests may inject an isolated dict via ``_memo``.
    """
    if _memo is None:
        _memo = _RATE_MEMO
    key = (model_id, provider_id)
    if key in _memo:
        return _memo[key]
    try:
        _memo[key] = derive_rates(model_id, provider_id)
    except SystemExit:
        _memo[key] = None
    except Exception:
        _memo[key] = None
    return _memo[key]


def build_report(records, min_write, w_rate, r_rate, model_id=''):
    rounds = [r for r in records
              if int(r.get('cache_read') or 0) == 0
              and int(r.get('cache_write') or 0) > min_write]

    def _rates_for(rec):
        """Per-ROW rates. The record's own model wins; the CLI --model is the
        fallback for rows written before the model field existed.

        A single fleet-wide rate was an unbounded approximation: the real mix
        spans two orders of magnitude, so per-row pricing is what turns the CNY
        column from an estimate into a figure.
        """
        m = rec.get('model')
        if m:
            got = _rate_cache(m)
            if got:
                return got
        return w_rate, r_rate

    agg = defaultdict(lambda: {'n': 0, 'tok': 0, 'gaps': [], 'under18': 0,
                               'paid': 0.0, 'recover': 0.0})
    by_model = defaultdict(lambda: {'n': 0, 'tok': 0, 'paid': 0.0,
                                    'recover': 0.0, 'priced': False})
    for r in rounds:
        _w, _r = _rates_for(r)
        tok = int(r.get('cache_write') or 0)
        a = agg[bucket_of(r)]
        a['n'] += 1
        a['tok'] += tok
        a['paid'] += tok * _w
        a['recover'] += tok * (_w - _r)
        gap = r.get('gap_s')
        if gap is not None:
            gap = float(gap)
            a['gaps'].append(gap)
            if gap < 18.0:
                a['under18'] += 1
        bm = by_model[r.get('model') or f'(unrecorded — priced as {model_id})']
        bm['n'] += 1
        bm['tok'] += tok
        bm['paid'] += tok * _w
        bm['recover'] += tok * (_w - _r)
        bm['priced'] = bool(r.get('model'))

    rows = []
    for name, a in agg.items():
        rows.append({
            'bucket': name,
            'n': a['n'],
            'wasted_tokens': a['tok'],
            'paid_cny': a['paid'],
            'recoverable_cny': a['recover'],
            'gap_p50': percentile(a['gaps'], 50),
            'gap_p90': percentile(a['gaps'], 90),
            'pct_under_18s': (100.0 * a['under18'] / len(a['gaps'])
                              if a['gaps'] else None),
            'is_waste': name not in NON_WASTE_BUCKETS and name != COLD_START_LABEL,
        })
    rows.sort(key=lambda x: (not x['is_waste'], -x['recoverable_cny']))

    true_recoverable = sum(x['recoverable_cny'] for x in rows if x['is_waste'])
    for x in rows:
        x['share_of_recoverable'] = (
            100.0 * x['recoverable_cny'] / true_recoverable
            if x['is_waste'] and true_recoverable else None)

    _stamps = [r['_ts'] for r in records if r.get('_ts')]
    _model_rows = sorted(
        ({'model': m, 'n': v['n'], 'wasted_tokens': v['tok'],
          'paid_cny': v['paid'], 'recoverable_cny': v['recover'],
          'priced_from_record': v['priced']}
         for m, v in by_model.items()),
        key=lambda x: -x['recoverable_cny'])
    _priced_n = sum(x['n'] for x in _model_rows if x['priced_from_record'])
    return {
        'records_scanned': len(records),
        'zero_readback_rounds': len(rounds),
        'min_write': min_write,
        'model': model_id,
        'by_model': _model_rows,
        'rows_priced_from_record': _priced_n,
        'rows_priced_from_fallback': len(rounds) - _priced_n,
        'window_first': min(_stamps) if _stamps else '',
        'window_last': max(_stamps) if _stamps else '',
        'write_cny_per_1k': w_rate * 1000,
        'read_cny_per_1k': r_rate * 1000,
        'true_recoverable_cny': true_recoverable,
        'excluded_cny': sum(x['paid_cny'] for x in rows if not x['is_waste']),
        'rows': rows,
    }


def _fmt(v, spec, dash='-'):
    return dash if v is None else format(v, spec)


def print_report(rep):
    print(f"records scanned          : {rep['records_scanned']:,}")
    if rep.get('window_first'):
        print(f"observed window          : {rep['window_first']} → "
              f"{rep['window_last']}")
        print(f"  └ the log is LIVE; to reproduce THIS table exactly, pin the "
              f"upper bound:\n    python3 scripts/cache_waste_report.py "
              f"--until '{rep['window_last']}'")
    print(f"zero-readback rounds     : {rep['zero_readback_rounds']:,} "
          f"(cache_read==0 and cache_write>{rep['min_write']:,})")
    print(f"rates (from lib.cost)    : write {rep['write_cny_per_1k']:.5f} "
          f"CNY/1k · read {rep['read_cny_per_1k']:.5f} CNY/1k")
    print(f"  └ priced as {rep['model']!r} — records carry no model id, so ALL "
          f"rows use this rate;\n    token/round counts are exact, CNY is only "
          f"as right as --model.")
    print()
    hdr = (f"{'bucket':34s}{'n':>6s}{'wasted_tok':>15s}{'paid':>10s}"
           f"{'recover':>10s}{'share':>8s}{'p50':>9s}{'p90':>9s}{'%<18s':>8s}")
    print(hdr)
    print('-' * len(hdr))
    for x in rep['rows']:
        share = ('   n/a' if x['share_of_recoverable'] is None
                 else f"{x['share_of_recoverable']:6.1f}%")
        print(f"{x['bucket']:34s}{x['n']:6d}{x['wasted_tokens']:15,d}"
              f"{x['paid_cny']:10,.0f}"
              f"{(x['recoverable_cny'] if x['is_waste'] else 0):10,.0f}"
              f"{share:>8s}"
              f"{_fmt(x['gap_p50'], '9.1f')}{_fmt(x['gap_p90'], '9.1f')}"
              f"{_fmt(x['pct_under_18s'], '7.1f')}%")
    print('-' * len(hdr))
    print(f"TRUE recoverable (waste buckets only) : {rep['true_recoverable_cny']:,.0f} CNY")
    print(f"excluded as NOT waste (TTL + cold)    : {rep['excluded_cny']:,.0f} CNY paid, "
          f"0 recoverable")

    _bm = rep.get('by_model') or []
    if _bm:
        print()
        _exact = rep.get('rows_priced_from_record', 0)
        _fb = rep.get('rows_priced_from_fallback', 0)
        print(f"per-model split — {_exact:,} rows priced from their OWN recorded "
              f"model, {_fb:,} from --model fallback")
        if _fb:
            print(f"  └ fallback rows predate the model field; their CNY is an "
                  f"approximation at the {rep['model']!r} rate.")
        mh = f"  {'model':40s}{'n':>7s}{'wasted_tok':>15s}{'recover':>11s}"
        print(mh)
        print('  ' + '-' * (len(mh) - 2))
        for m in _bm[:12]:
            print(f"  {m['model'][:40]:40s}{m['n']:7d}{m['wasted_tokens']:15,d}"
                  f"{m['recoverable_cny']:11,.0f}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--glob', default='logs/app.log*',
                    help="log glob (default: all rotated logs)")
    ap.add_argument('--min-write', type=int, default=20000,
                    help='only count rounds writing more than this (default 20000)')
    ap.add_argument('--model', default='claude-opus-5',
                    help='model id used to derive rates from lib.cost. The '
                         'records do NOT carry a model, so this sets the price '
                         'for EVERY row — pass the model that actually produced '
                         'the traffic. Default claude-opus-5 (0.04525 CNY/1k '
                         'write), which reproduces the audited 2026-07-29 '
                         'figure. Probe fails loudly on an unknown id rather '
                         'than falling back to a guess.')
    ap.add_argument('--provider', default=None)
    ap.add_argument('--since', default='',
                    help="only count records at/after this log stamp prefix "
                         "(e.g. '2026-07-29' or '2026-07-29 14:00:00')")
    ap.add_argument('--until', default='',
                    help="only count records at/before this log stamp prefix — "
                         "use it to REPRODUCE a published table exactly, since "
                         "the log keeps growing")
    ap.add_argument('--json', action='store_true', help='emit JSON instead of a table')
    args = ap.parse_args(argv)

    w, r = derive_rates(args.model, args.provider)
    records = load_records(args.glob, args.since, args.until)
    if not records:
        print(f'no {RECORD_MARKER} lines matched {args.glob!r}', file=sys.stderr)
        return 1
    rep = build_report(records, args.min_write, w, r, args.model)
    if args.json:
        print(json.dumps(rep, indent=2, ensure_ascii=False))
    else:
        print_report(rep)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
