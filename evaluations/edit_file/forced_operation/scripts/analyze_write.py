"""Analyse Experiment 2 (write_file modes). Usage: python3 analyze_write.py --runs e2 --out out/e2"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(__file__))
import analyze as an  # noqa: E402
import numpy as np  # noqa: E402

ROOT = an.ROOT
PAIRS = {'E2a': ('Wfull', 'Wappend'), 'E2b': ('Wfull', 'Wref'), 'E2c': ('Wfull', 'Eedit')}


EXPECTED = {}


def expected_text(task_id):
    if task_id not in EXPECTED:
        with open(os.path.join(ROOT, 'tasks', 'files', f'{task_id}.expected'), encoding='utf-8', newline='') as f:
            EXPECTED[task_id] = f.read()
    return EXPECTED[task_id]


def derive(r):
    calls = r['calls']
    recs = [t for c in calls for t in (c.get('tool_calls') or [])]
    usage = [c.get('usage') or {} for c in calls]
    comp = [int(u.get('completion_tokens') or u.get('output_tokens') or 0) for u in usage]
    used = Counter(k for t in recs for k in (t.get('used') or []))
    return {'key': r['key'], 'model': r['model'], 'arm': r['arm'], 'task': r['task'], 'exp': r['exp'],
            'size': r['size'], 'repeat': r['repeat'], 'status': r['status'],
            'final': int(bool(r['final_success'])), 'first': int(bool(r['first_try_success'])),
            'final_lenient': int(bool(r['final_success']) or (
                isinstance(r.get('final_text'), str)
                and r['final_text'].rstrip('\n') == expected_text(r['task']).rstrip('\n'))),
            'payload': sum(t.get('args_o200k') or 0 for t in recs),
            'completion': sum(comp), 'calls': len(calls),
            'latency': sum(c.get('latency_s') or 0 for c in calls),
            'truncated': int(any(c.get('finish_reason') == 'length' for c in calls)),
            'used_ref': int(used.get('content_ref', 0) > 0), 'used_append': int(used.get('append', 0) > 0),
            'ref_failed': int(any('content_ref resolution failed' in (t.get('output') or '') for t in recs))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--runs', action='append', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--models', help='comma list of models to include (default: all)')
    ap.add_argument('--exclude-tasks', help='comma list of task-id prefixes to drop')
    ap.add_argument('--only-tasks', help='comma list of task-id prefixes to keep')
    args = ap.parse_args()
    rng = np.random.default_rng(20261106)
    raw = an.load_runs(args.runs)
    if args.models:
        raw = [r for r in raw if r['model'] in args.models.split(',')]
    if args.exclude_tasks:
        raw = [r for r in raw if not r['task'].startswith(tuple(args.exclude_tasks.split(',')))]
    if args.only_tasks:
        raw = [r for r in raw if r['task'].startswith(tuple(args.only_tasks.split(',')))]
    rows = [derive(r) for r in raw if r['status'] != 'http_error']
    models = sorted({r['model'] for r in rows})
    res = {'n_attempts': len(rows), 'missing_http_error': [r['key'] for r in raw if r['status'] == 'http_error'],
           'models': models, 'arms': {}, 'paired': {}, 'by_size': {}}
    lines = [f'# Experiment 2 ({", ".join(args.runs)}): {len(rows)} attempts', '',
             '| exp | arm | final | first-try | payload o200k | completion | latency s | truncated |',
             '|---|---|---|---|---|---|---|---|']
    for exp, (base, mode) in PAIRS.items():
        for arm in (base, mode):
            sub = [r for r in rows if r['exp'] == exp and r['arm'] == arm]
            stats = {k: an.boot_ratio(an.grouped(sub, k, models), rng)
                     for k in ('final', 'first', 'final_lenient', 'payload', 'completion', 'latency', 'truncated',
                               'used_ref', 'used_append', 'ref_failed')}
            res['arms'][f'{exp}|{arm}'] = stats
            lines.append(f"| {exp} | {arm} | {an.fmt(stats['final'], True)} | {an.fmt(stats['first'], True)} | "
                         f"{an.fmt(stats['payload'], digits=0)} | {an.fmt(stats['completion'], digits=0)} | "
                         f"{an.fmt(stats['latency'])} | {an.fmt(stats['truncated'], True)} |")
            for size in sorted({r['size'] for r in sub}):
                cell = [r for r in sub if r['size'] == size]
                res['by_size'][f'{exp}|{arm}|{size}'] = {k: an.boot_ratio(an.grouped(cell, k, models), rng)
                                                        for k in ('final', 'payload', 'completion', 'latency')}
        exp_rows = [r for r in rows if r['exp'] == exp]
        res['paired'][f'{exp}|{mode}-{base}'] = {k: an.boot_ratio(an.paired(exp_rows, mode, base, k, models), rng)
                                                 for k in ('final', 'first', 'final_lenient', 'payload',
                                                           'completion', 'latency')}
        for size in sorted({r['size'] for r in exp_rows}):
            cell = [r for r in exp_rows if r['size'] == size]
            res['paired'][f'{exp}|{size}|{mode}-{base}'] = {
                k: an.boot_ratio(an.paired(cell, mode, base, k, models), rng)
                for k in ('final', 'payload', 'completion', 'latency')}
    lines += ['', '| paired | d final | d first | d payload | d completion | d latency s |', '|---|---|---|---|---|---|']
    for key, v in res['paired'].items():
        lines.append(f"| {key} | {an.fmt(v['final'], True)} | {an.fmt(v.get('first'), True)} | "
                     f"{an.fmt(v['payload'], digits=0)} | {an.fmt(v['completion'], digits=0)} | {an.fmt(v['latency'])} |")
    lines += ['', '| exp | arm | size | final | payload | completion | latency |', '|---|---|---|---|---|---|---|']
    for key, v in res['by_size'].items():
        exp, arm, size = key.split('|')
        lines.append(f"| {exp} | {arm} | {size} | {an.fmt(v['final'], True)} | {an.fmt(v['payload'], digits=0)} | "
                     f"{an.fmt(v['completion'], digits=0)} | {an.fmt(v['latency'])} |")
    per_model = defaultdict(lambda: [0, 0])
    for r in rows:
        per_model[(r['exp'], r['arm'], r['model'])][0] += r['final']
        per_model[(r['exp'], r['arm'], r['model'])][1] += 1
    res['per_model_final'] = {'|'.join(k): v for k, v in per_model.items()}
    lines += ['', 'Per model final success (successes/attempts): ' + '; '.join(
        f'{"|".join(k)} {v[0]}/{v[1]}' for k, v in sorted(per_model.items()))]
    with open(args.out + '.json', 'w', encoding='utf-8') as f:
        json.dump(res, f, indent=1)
    with open(args.out + '.rows.jsonl', 'w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')
    with open(args.out + '.md', 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
