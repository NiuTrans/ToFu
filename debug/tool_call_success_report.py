#!/usr/bin/env python3
"""First-attempt tool-call pass-rate report (read-only, offline).

Aggregates the structured audit lines emitted by
``lib/tasks_pkg/tool_call_outcome.py`` (Proposal A of
``docs/TOOL_CALL_SUCCESS_RENDERING_DESIGN_PROPOSAL.md``) into pass rates
by model x tool x failure category, plus the PTC program authoring rate
for both backends. This is the measurement stick for the "tool-call
success rate is low" symptom: it confirms or refutes the claim per model
and names the dominant failure categories to fix.

Usage::

    python3 debug/tool_call_success_report.py                 # logs/audit.log
    python3 debug/tool_call_success_report.py --file path/to/audit.log
    python3 debug/tool_call_success_report.py --model kimi-k3 --top 15

Input is the JSONL audit trail (``logs/audit.log`` by default). Only
lines carrying ``"event"`` names in the tool-call outcome family are
read; everything else is ignored. The script never writes anything.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from typing import Any

#: Audit events this report understands. ``tool_gateway_repaired`` and
#: ``ptc_local_program`` predate the outcome taxonomy; they are folded in
#: as supplementary evidence (alias leaderboard / legacy program counts).
_OUTCOME_EVENT = 'tool_call_outcome'
_PROGRAM_EVENT = 'ptc_program_outcome'
_GATEWAY_REPAIR_EVENT = 'tool_gateway_repaired'

_OUTCOMES = ('ok', 'repaired', 'rejected', 'error', 'aborted',
             'unanswerable')


def _iter_events(paths: list[str]):
    """Yield parsed audit dicts, skipping blank/malformed lines."""
    for path in paths:
        try:
            handle = open(path, encoding='utf-8', errors='replace')
        except OSError as exc:
            print(f'warning: cannot open {path}: {exc}', file=sys.stderr)
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and 'event' in row:
                    yield row


def _rate(part: int, total: int) -> float:
    return round(part / total, 4) if total else 0.0


def build_report(paths: list[str], *, model: str = '') -> dict[str, Any]:
    """Aggregate outcome/program audit lines into a deterministic dict."""
    by_pair: dict[tuple[str, str], Counter] = {}
    rejection_kinds: Counter = Counter()
    repair_patterns: Counter = Counter()
    attempted_names: Counter = Counter()
    alias_pairs: Counter = Counter()
    models: Counter = Counter()
    programs: dict[tuple[str, str], Counter] = {}
    program_errors: Counter = Counter()
    totals = Counter()

    for row in _iter_events(paths):
        event = str(row.get('event') or '')
        row_model = str(row.get('model') or '')
        if model and row_model != model:
            continue
        if event == _OUTCOME_EVENT:
            tool = str(row.get('tool') or '') or '(unnamed)'
            outcome = str(row.get('outcome') or '') or 'error'
            pair = (row_model, tool)
            bucket = by_pair.setdefault(pair, Counter())
            bucket[outcome] += 1
            bucket['total'] += 1
            totals[outcome] += 1
            totals['total'] += 1
            models[row_model] += 1
            kind = str(row.get('rejection_kind') or '')
            if kind:
                rejection_kinds[kind] += 1
            for pattern in row.get('repairs') or ():
                repair_patterns[str(pattern)] += 1
            attempted = str(row.get('attempted') or '')
            if attempted:
                attempted_names[attempted] += 1
        elif event == _PROGRAM_EVENT:
            backend = str(row.get('backend') or '') or '(unknown)'
            pair = (row_model, backend)
            bucket = programs.setdefault(pair, Counter())
            bucket['total'] += 1
            status = str(row.get('status') or '')
            bucket[status or 'error'] += 1
            syntax_repairs = int(row.get('syntax_repairs') or 0)
            if status == 'completed' and syntax_repairs == 0:
                bucket['clean'] += 1
            bucket['children'] += int(row.get('children') or 0)
            bucket['rejected_children'] += int(
                row.get('rejected_children') or 0)
            code = str(row.get('error_code') or '')
            if code:
                program_errors[code] += 1
        elif event == _GATEWAY_REPAIR_EVENT:
            attempted = str(row.get('attempted') or '')
            resolved = str(row.get('resolved') or '')
            if attempted and resolved:
                alias_pairs[f'{attempted} -> {resolved}'] += 1

    def pair_rows(table: dict[tuple[str, str], Counter],
                  *, program: bool) -> list[dict[str, Any]]:
        rows = []
        for (pair_model, name), counts in table.items():
            total = int(counts.get('total') or 0)
            row: dict[str, Any] = {
                'model': pair_model, 'name': name, 'total': total,
            }
            if program:
                row['completed'] = int(counts.get('completed') or 0)
                row['error'] = int(counts.get('error') or 0)
                row['clean'] = int(counts.get('clean') or 0)
                row['clean_rate'] = _rate(row['clean'], total)
                row['children'] = int(counts.get('children') or 0)
                row['rejected_children'] = int(
                    counts.get('rejected_children') or 0)
            else:
                for outcome in _OUTCOMES:
                    row[outcome] = int(counts.get(outcome) or 0)
                row['first_attempt_rate'] = _rate(row['ok'], total)
                row['with_repair_rate'] = _rate(
                    row['ok'] + row['repaired'], total)
            rows.append(row)
        rows.sort(key=lambda item: (-item['total'], item['model'],
                                    item['name']))
        return rows

    total = int(totals.get('total') or 0)
    ok = int(totals.get('ok') or 0)
    repaired = int(totals.get('repaired') or 0)
    return {
        'files': list(paths),
        'model_filter': model,
        'tool_calls': {
            'total': total,
            'by_outcome': {outcome: int(totals.get(outcome) or 0)
                           for outcome in _OUTCOMES},
            'first_attempt_rate': _rate(ok, total),
            'with_repair_rate': _rate(ok + repaired, total),
            'by_model_tool': pair_rows(by_pair, program=False),
        },
        'rejection_kinds': rejection_kinds.most_common(),
        'repair_patterns': repair_patterns.most_common(),
        'hallucinated_names': attempted_names.most_common(),
        'gateway_alias_leaderboard': alias_pairs.most_common(),
        'programs': {
            'by_model_backend': pair_rows(programs, program=True),
            'error_codes': program_errors.most_common(),
        },
    }


def _fmt_pct(value: float) -> str:
    return f'{value * 100:.1f}%'


def render_text(report: dict[str, Any], *, top: int = 10) -> str:
    lines: list[str] = []
    calls = report['tool_calls']
    lines.append('== Tool-call first-attempt pass rate ==')
    lines.append(f"files: {', '.join(report['files'])}")
    if report['model_filter']:
        lines.append(f"model filter: {report['model_filter']}")
    lines.append(
        f"total calls: {calls['total']}  first-attempt: "
        f"{_fmt_pct(calls['first_attempt_rate'])}  with-repair: "
        f"{_fmt_pct(calls['with_repair_rate'])}")
    outcome_bits = [f"{name}={count}" for name, count
                    in calls['by_outcome'].items() if count]
    lines.append('outcomes: ' + (', '.join(outcome_bits) or '(none)'))
    lines.append('')
    lines.append('-- by model x tool (top %d) --' % top)
    for row in calls['by_model_tool'][:top]:
        lines.append(
            f"{row['total']:>5}  {_fmt_pct(row['first_attempt_rate']):>6} "
            f"ok={row['ok']} repaired={row['repaired']} "
            f"rejected={row['rejected']} error={row['error']}  "
            f"{row['model'] or '(unknown)'} / {row['name']}")
    if report['rejection_kinds']:
        lines.append('')
        lines.append('-- rejection kinds (top %d) --' % top)
        for kind, count in report['rejection_kinds'][:top]:
            lines.append(f'{count:>5}  {kind}')
    if report['hallucinated_names']:
        lines.append('')
        lines.append('-- hallucinated / attempted names (top %d) --' % top)
        for name, count in report['hallucinated_names'][:top]:
            lines.append(f'{count:>5}  {name}')
    if report['repair_patterns']:
        lines.append('')
        lines.append('-- harness repair patterns (top %d) --' % top)
        for pattern, count in report['repair_patterns'][:top]:
            lines.append(f'{count:>5}  {pattern}')
    if report['gateway_alias_leaderboard']:
        lines.append('')
        lines.append('-- gateway name aliases (top %d) --' % top)
        for pair, count in report['gateway_alias_leaderboard'][:top]:
            lines.append(f'{count:>5}  {pair}')
    programs = report['programs']['by_model_backend']
    if programs:
        lines.append('')
        lines.append('== PTC program authoring ==')
        for row in programs[:top]:
            lines.append(
                f"{row['total']:>5} programs  clean: "
                f"{_fmt_pct(row['clean_rate']):>6}  "
                f"completed={row['completed']} error={row['error']} "
                f"children={row['children']} "
                f"rejected_children={row['rejected_children']}  "
                f"{row['model'] or '(unknown)'} / {row['name']}")
        if report['programs']['error_codes']:
            lines.append('-- program error codes (top %d) --' % top)
            for code, count in report['programs']['error_codes'][:top]:
                lines.append(f'{count:>5}  {code}')
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--file', action='append', default=[],
                        help='audit log path (repeatable); default logs/audit.log')
    parser.add_argument('--model', default='',
                        help='only aggregate rows for this model')
    parser.add_argument('--top', type=int, default=10,
                        help='rows per section (default 10)')
    parser.add_argument('--json', action='store_true',
                        help='print the raw aggregate dict as JSON')
    args = parser.parse_args(argv)
    paths = args.file or [os.path.join('logs', 'audit.log')]
    report = build_report(paths, model=args.model)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report, top=max(1, args.top)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
