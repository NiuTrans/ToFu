"""Analyse forced-operation edit benchmark runs (pre-registered in PREREG.md).

Usage: python3 analyze.py --runs main [--runs more] --out out/main
Writes <out>.json (every number) and <out>.md (tables). Bootstrap: 10,000
task-cluster resamples, percentile 95% CI, fixed seed.
"""
import argparse
import json
import os
import re
from collections import Counter, defaultdict

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
B = 10000
ARMS = ['A1', 'A2', 'A3', 'A4']
TYPES = ['insert', 'replace', 'delete', 'multi']
STRATA = ['S', 'M', 'L']


def load_tasks():
    tasks = {}
    for line in open(os.path.join(ROOT, 'tasks', 'tasks.jsonl'), encoding='utf-8'):
        t = json.loads(line)
        with open(os.path.join(ROOT, 'tasks', 'files', f"{t['file_key']}.orig"), encoding='utf-8', newline='') as f:
            t['original'] = f.read()
        with open(os.path.join(ROOT, 'tasks', 'files', f"{t['id']}.expected"), encoding='utf-8', newline='') as f:
            t['expected'] = f.read()
        tasks[t['id']] = t
    return tasks


def load_runs(names):
    rows = {}
    for name in names:
        for line in open(os.path.join(ROOT, 'raw', f'{name}.jsonl'), encoding='utf-8'):
            r = json.loads(line)
            if r['key'] in rows and r['status'] == 'http_error':
                continue
            rows[r['key']] = r
    return list(rows.values())


def tool_error_class(arm, rec):
    if rec.get('ok'):
        return None
    out = rec.get('output') or ''
    if arm in ('A1', 'A2'):
        if not rec.get('admitted'):
            return 'schema'
        if 'not-found' in out or 'not found' in out:
            return 'not_found'
        if 'ambiguous' in out:
            return 'ambiguous'
        if 'echo-content' in out:
            return 'echo_refused'
        if 'path' in out.split('\n', 1)[-1][:200].lower() and 'invalid' in out.lower():
            return 'path'
        return 'other_tool_error'
    if arm == 'A3':
        kind = rec.get('error_kind')
        return {'schema': 'schema', 'json': 'schema', 'unknown_tool': 'schema', 'not_found': 'not_found',
                'ambiguous': 'ambiguous', 'path': 'path'}.get(kind, 'other_tool_error')
    if rec.get('error_kind') in ('json', 'schema', 'unknown_tool'):
        return 'schema'
    if 'invalid patch' in out or 'invalid hunk' in out:
        return 'schema'
    if 'Failed to find' in out:
        return 'not_found'
    if 'Failed to read' in out:
        return 'path'
    return 'other_tool_error'


def missing_operation(rec):
    err = rec.get('contract_error') or {}
    text = json.dumps(err) + (rec.get('output') or '')[:400]
    return (not rec.get('admitted', True)) and 'operation' in text


def splice(lines, at, block):
    return lines[:at] + block + lines[at:]


def final_class(task, final):
    if final == task['expected']:
        return None
    if final == task['original']:
        return 'no_change'
    orig = task['original'][:-1].split('\n')
    exp = task['expected']
    if len(task['sites']) == 1 and task['sites'][0]['kind'] == 'ins':
        s = task['sites'][0]
        block = s['text'][:-1].split('\n')
        at = s['line'] - 1 if s['position'] == 'after' else s['line']
        if '\n'.join(splice(orig, at, block)) + '\n' == final:
            return 'wrong_side'
    for s in task['sites']:
        if s['kind'] != 'ins':
            continue
        block = s['text'][:-1].split('\n')
        n = s['line']
        for k in (1, 2, 3):
            if s['position'] == 'after':
                anchor = orig[max(0, n - k):n]
                cand = '\n'.join(splice(orig, n, anchor + block)) + '\n'
            else:
                anchor = orig[n - 1:n - 1 + k]
                cand = '\n'.join(splice(orig, n - 1, block + anchor)) + '\n'
            others = [x for x in task['sites'] if x is not s]
            if not others and cand == final:
                return 'anchor_echoed'
            if others and re.sub(r'\s+', '', cand) != re.sub(r'\s+', '', final):
                pass
    if re.sub(r'\s+', '', final) == re.sub(r'\s+', '', exp):
        return 'whitespace'
    return 'other_wrong'


def derive(r, task):
    calls = r['calls']
    tool_recs = [tc for c in calls for tc in (c.get('tool_calls') or [])]
    first = calls[0] if calls else {}
    first_tools = first.get('tool_calls') or []
    usage = [c.get('usage') or {} for c in calls]
    comp = [int(u.get('completion_tokens') or u.get('output_tokens') or 0) for u in usage]
    reas = [int(((u.get('completion_tokens_details') or {}).get('reasoning_tokens')) or 0) for u in usage]
    errors = [tool_error_class(r['arm'], tc) for tc in tool_recs]
    first_errors = [tool_error_class(r['arm'], tc) for tc in first_tools]
    final_text = None
    d = {
        'key': r['key'], 'model': r['model'], 'arm': r['arm'], 'task': r['task'], 'repeat': r['repeat'],
        'edit_type': task['edit_type'], 'stratum': task['stratum'], 'status': r['status'],
        'final': int(bool(r['final_success'])), 'first': int(bool(r['first_try_success'])),
        'payload': sum(tc.get('args_o200k') or 0 for tc in tool_recs),
        'payload_first': sum(tc.get('args_o200k') or 0 for tc in first_tools),
        'patch': sum(tc.get('patch_o200k') or 0 for tc in tool_recs),
        'completion': sum(comp), 'completion_first': comp[0] if comp else 0,
        'reasoning': sum(reas), 'calls': len(calls), 'tool_calls': len(tool_recs),
        'visible': sum(c.get('visible_o200k') or 0 for c in calls),
        'no_tool_first': int(bool(calls) and not first_tools),
        'schema_first': int(any(e == 'schema' for e in first_errors)),
        'missing_operation_first': int(any(missing_operation(tc) for tc in first_tools)),
        'first_error': next((e for e in errors if e), None),
        'tool_ok_but_wrong': 0,
    }
    if not r['final_success']:
        final_text = r['final_text'] if 'final_text' in r else reconstruct(r, task)
        d['final_class'] = final_class(task, final_text) if final_text is not None else 'unknown'
        d['tool_ok_but_wrong'] = int(bool(tool_recs) and all(tc.get('ok') for tc in tool_recs))
    else:
        d['final_class'] = None
    return d


def reconstruct(r, task):
    """Final text is not stored; the diff vs expected is. Re-derive from the diff."""
    diff = r.get('final_diff')
    if diff is None:
        return None
    if r.get('changed') is False:
        return task['original']
    exp = task['expected'].split('\n')
    out, i = [], 0
    lines = diff.split('\n')
    for line in lines[2:]:
        m = re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
        if m:
            start = int(m.group(1)) - (0 if m.group(2) == '0' else 1)
            out.extend(exp[i:start])
            i = start
            continue
        if line.startswith(' '):
            out.append(line[1:])
            i += 1
        elif line.startswith('-'):
            i += 1
        elif line.startswith('+'):
            out.append(line[1:])
    if len(lines) >= 80:
        return None
    out.extend(exp[i:])
    return '\n'.join(out)


def boot_ratio(groups, rng):
    """groups: list (per model) of dict task -> (sum, n). Equal-weight mean over models
    of ratio estimators; tasks resampled jointly. Returns (point, lo, hi, n)."""
    tasks = sorted(set().union(*[g.keys() for g in groups]))
    if not tasks:
        return None
    idx = {t: i for i, t in enumerate(tasks)}
    S = np.zeros((len(groups), len(tasks)))
    N = np.zeros((len(groups), len(tasks)))
    for m, g in enumerate(groups):
        for t, (s, n) in g.items():
            S[m, idx[t]] = s
            N[m, idx[t]] = n
    def stat(weights):
        num = S @ weights
        den = N @ weights
        with np.errstate(invalid='ignore', divide='ignore'):
            vals = num / den
        return np.nanmean(vals, axis=0)
    point = float(stat(np.ones(len(tasks))))
    draws = rng.integers(0, len(tasks), size=(B, len(tasks)))
    W = np.zeros((B, len(tasks)))
    for b in range(B):
        np.add.at(W[b], draws[b], 1)
    vals = stat(W.T)
    lo, hi = np.nanpercentile(vals, [2.5, 97.5])
    return {'est': point, 'lo': float(lo), 'hi': float(hi), 'n': int(N.sum()), 'tasks': len(tasks)}


def grouped(rows, metric, models):
    out = []
    for m in models:
        g = defaultdict(lambda: [0.0, 0])
        for r in rows:
            if r['model'] == m and r.get(metric) is not None:
                g[r['task']][0] += r[metric]
                g[r['task']][1] += 1
        out.append({t: tuple(v) for t, v in g.items()})
    return [g for g in out if g]


def paired(rows, a, b, metric, models):
    by = {(r['model'], r['arm'], r['task'], r['repeat']): r for r in rows}
    out = []
    for m in models:
        g = defaultdict(lambda: [0.0, 0])
        for (model, arm, task, rep), r in by.items():
            if model != m or arm != a:
                continue
            other = by.get((model, b, task, rep))
            if other is None:
                continue
            g[task][0] += r[metric] - other[metric]
            g[task][1] += 1
        if g:
            out.append({t: tuple(v) for t, v in g.items()})
    return out


def fmt(x, pct=False, digits=1):
    if not x:
        return 'n/a'
    if pct:
        return f"{100 * x['est']:.{digits}f}% [{100 * x['lo']:.{digits}f}, {100 * x['hi']:.{digits}f}]"
    return f"{x['est']:.{digits}f} [{x['lo']:.{digits}f}, {x['hi']:.{digits}f}]"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--runs', action='append', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--models', help='comma list of models to include (default: all)')
    args = ap.parse_args()
    rng = np.random.default_rng(20261106)
    tasks = load_tasks()
    raw = load_runs(args.runs)
    if args.models:
        raw = [r for r in raw if r['model'] in args.models.split(',')]
    rows = [derive(r, tasks[r['task']]) for r in raw if r['status'] != 'http_error']
    missing = [r['key'] for r in raw if r['status'] == 'http_error']
    models = sorted({r['model'] for r in rows})
    res = {'n_attempts': len(rows), 'missing_http_error': missing, 'models': models,
           'per_arm_model': {}, 'pooled': {}, 'paired': {}, 'taxonomy': {}, 'by_type': {}}
    metrics = ['final', 'first', 'payload', 'payload_first', 'completion', 'completion_first', 'reasoning',
               'calls', 'schema_first', 'missing_operation_first', 'no_tool_first', 'patch', 'visible']
    for arm in ARMS:
        sub = [r for r in rows if r['arm'] == arm]
        for m in models:
            res['per_arm_model'][f'{arm}|{m}'] = {k: boot_ratio(grouped([r for r in sub if r['model'] == m], k, [m]), rng)
                                                 for k in ['final', 'first', 'payload', 'completion', 'schema_first', 'calls']}
        res['pooled'][arm] = {k: boot_ratio(grouped(sub, k, models), rng) for k in metrics}
        for et in TYPES:
            st = [r for r in sub if r['edit_type'] == et]
            res['by_type'][f'{arm}|{et}'] = {k: boot_ratio(grouped(st, k, models), rng)
                                            for k in ['final', 'first', 'payload', 'completion']}
            for s in STRATA:
                cell = [r for r in st if r['stratum'] == s]
                res['by_type'][f'{arm}|{et}|{s}'] = {k: boot_ratio(grouped(cell, k, models), rng)
                                                    for k in ['final', 'first', 'payload']}
    pairs = [('A2', 'A1'), ('A3', 'A1'), ('A4', 'A1'), ('A4', 'A3'), ('A3', 'A2')]
    for a, b in pairs:
        for et in TYPES + ['all']:
            st = rows if et == 'all' else [r for r in rows if r['edit_type'] == et]
            res['paired'][f'{a}-{b}|{et}'] = {k: boot_ratio(paired(st, a, b, k, models), rng)
                                             for k in ['final', 'first', 'payload', 'completion']}
            if (a, b) == ('A2', 'A1') and et != 'all':
                for s in STRATA:
                    cell = [r for r in st if r['stratum'] == s]
                    res['paired'][f'{a}-{b}|{et}|{s}'] = {k: boot_ratio(paired(cell, a, b, k, models), rng)
                                                         for k in ['final', 'first', 'payload', 'completion']}
    for arm in ARMS:
        for m in models + ['all']:
            sub = [r for r in rows if r['arm'] == arm and (m == 'all' or r['model'] == m)]
            res['taxonomy'][f'{arm}|{m}'] = {
                'first_error': dict(Counter(r['first_error'] for r in sub if r['first_error'])),
                'final_class': dict(Counter(r['final_class'] for r in sub if r['final_class'])),
                'tool_ok_but_wrong': sum(r['tool_ok_but_wrong'] for r in sub),
                'n': len(sub)}
    with open(args.out + '.json', 'w', encoding='utf-8') as f:
        json.dump(res, f, indent=1)
    with open(args.out + '.rows.jsonl', 'w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')
    lines = [f'# Results ({", ".join(args.runs)}): {len(rows)} attempts, models {", ".join(models)}',
             f'Missing (HTTP errors after retries): {len(missing)}', '',
             '## Pooled over models (equal weight), 95% task-cluster bootstrap CI', '',
             '| arm | final success | first-try success | payload o200k / attempt | provider completion tokens | schema-refused first call | model calls |',
             '|---|---|---|---|---|---|---|']
    for arm in ARMS:
        p = res['pooled'][arm]
        lines.append(f"| {arm} | {fmt(p['final'], True)} | {fmt(p['first'], True)} | {fmt(p['payload'])} | "
                     f"{fmt(p['completion'], digits=0)} | {fmt(p['schema_first'], True)} | {fmt(p['calls'], digits=2)} |")
    lines += ['', '## Per model', '', '| arm | model | final | first-try | payload | completion |', '|---|---|---|---|---|---|']
    for arm in ARMS:
        for m in models:
            p = res['per_arm_model'][f'{arm}|{m}']
            lines.append(f"| {arm} | {m} | {fmt(p['final'], True)} | {fmt(p['first'], True)} | {fmt(p['payload'])} | {fmt(p['completion'], digits=0)} |")
    lines += ['', '## Paired differences (row arm minus column arm), pooled', '',
              '| pair | subset | d final | d first-try | d payload | d completion |', '|---|---|---|---|---|---|']
    for key, val in res['paired'].items():
        pair, subset = key.split('|', 1)
        lines.append(f"| {pair} | {subset} | {fmt(val['final'], True)} | {fmt(val['first'], True)} | "
                     f"{fmt(val['payload'])} | {fmt(val['completion'], digits=0)} |")
    lines += ['', '## Failure taxonomy (all models)', '']
    for arm in ARMS:
        t = res['taxonomy'][f'{arm}|all']
        lines.append(f"- {arm} (n={t['n']}): first tool error {t['first_error']}; wrong final {t['final_class']}; "
                     f"tool said OK but file wrong {t['tool_ok_but_wrong']}")
    with open(args.out + '.md', 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print('\n'.join(lines[:40]))


if __name__ == '__main__':
    main()
