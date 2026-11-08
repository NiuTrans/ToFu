"""Exploratory measurements on Exp 1 raw records (labelled exploratory in the write-up).

- operation choice per arm (did A2 actually insert on insertion sites? did A1 try insert?)
- anchor tokens used (o200k) per edit item, by arm and stratum
- Codex patches: context lines per hunk
- reasoning-length outliers (finish=length)
Usage: python3 explore.py --runs main --out out/main_explore.json
"""
import argparse
import json
import os
import statistics as st
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(__file__))
import analyze as an  # noqa: E402
import run_bench as rb  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--runs', action='append', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--models', help='comma list of models to include (default: all)')
    args = ap.parse_args()
    tasks = an.load_tasks()
    raw = [r for r in an.load_runs(args.runs) if r['status'] != 'http_error']
    if args.models:
        raw = [r for r in raw if r['model'] in args.models.split(',')]
    ops = defaultdict(Counter)
    anchor_tok = defaultdict(list)
    content_tok = defaultdict(list)
    ctx_lines = []
    truncated = Counter()
    first_call_ops = defaultdict(Counter)
    misaligned = defaultdict(Counter)
    for r in raw:
        t = tasks[r['task']]
        for c in r['calls']:
            if c.get('finish_reason') == 'length':
                truncated[(r['model'], r['arm'])] += 1
        first = r['calls'][0] if r['calls'] else {}
        for tc in first.get('tool_calls') or []:
            try:
                a = json.loads(tc.get('arguments') or '{}')
            except json.JSONDecodeError:
                continue
            if r['arm'] in ('A1', 'A2') and isinstance(a.get('edits'), list):
                for e in a['edits']:
                    if not isinstance(e, dict):
                        continue
                    op = e.get('operation', '<missing>')
                    first_call_ops[(r['arm'], t['edit_type'])][op] += 1
                    anchor = e.get('anchor')
                    if op in ('insert_after', 'insert_before') and isinstance(anchor, str) and anchor:
                        text = t['original']
                        idx = text.find(anchor)
                        if idx >= 0 and text.count(anchor) == 1:
                            if op == 'insert_before':
                                aligned = idx == 0 or text[idx - 1] == '\n'
                            else:
                                end = idx + len(anchor)
                                aligned = end == len(text) or text[end] == '\n' or anchor.endswith('\n')
                            misaligned[r['model']]['aligned' if aligned else 'mid_line'] += 1
                            if not aligned:
                                misaligned[r['model']]['mid_line_final_ok' if r['final_success'] else 'mid_line_final_wrong'] += 1
                    if t['edit_type'] == 'insert':
                        ops[(r['arm'], t['stratum'])][op] += 1
                        if isinstance(e.get('anchor'), str):
                            anchor_tok[(r['arm'], t['stratum'])].append(rb.ntok(e['anchor']))
                        if isinstance(e.get('content'), str):
                            content_tok[(r['arm'], t['stratum'])].append(rb.ntok(e['content']))
            elif r['arm'] == 'A3' and t['edit_type'] == 'insert' and isinstance(a.get('old_string'), str):
                anchor_tok[('A3', t['stratum'])].append(rb.ntok(a['old_string']))
                content_tok[('A3', t['stratum'])].append(rb.ntok(a.get('new_string') or ''))
            elif r['arm'] == 'A4' and isinstance(a.get('input'), str):
                hunk_ctx = 0
                for line in a['input'].split('\n'):
                    if line.startswith('@@') or line.startswith('***'):
                        if hunk_ctx:
                            ctx_lines.append(hunk_ctx)
                        hunk_ctx = 0
                    elif line.startswith(' ') or line == '':
                        hunk_ctx += 1
                if hunk_ctx:
                    ctx_lines.append(hunk_ctx)
    out = {
        'insert_task_ops_first_call': {f'{k[0]}|{k[1]}': dict(v) for k, v in sorted(ops.items())},
        'ops_by_edit_type_first_call': {f'{k[0]}|{k[1]}': dict(v) for k, v in sorted(first_call_ops.items())},
        'insert_anchor_o200k_median': {f'{k[0]}|{k[1]}': (st.median(v), len(v)) for k, v in sorted(anchor_tok.items())},
        'insert_content_o200k_median': {f'{k[0]}|{k[1]}': (st.median(v), len(v)) for k, v in sorted(content_tok.items())},
        'codex_context_lines_per_hunk': {'median': st.median(ctx_lines) if ctx_lines else None,
                                         'mean': round(st.mean(ctx_lines), 2) if ctx_lines else None,
                                         'n_hunks': len(ctx_lines),
                                         'share_ge3': round(sum(1 for x in ctx_lines if x >= 3) / len(ctx_lines), 3) if ctx_lines else None},
        'finish_length_calls': {f'{k[0]}|{k[1]}': v for k, v in sorted(truncated.items())},
        'insert_anchor_alignment_first_call': {m: dict(v) for m, v in sorted(misaligned.items())},
    }
    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump(out, f, indent=1)
    print(json.dumps(out, indent=1))


if __name__ == '__main__':
    main()
