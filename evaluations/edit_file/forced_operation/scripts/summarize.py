"""Write RESULTS.md from the analysis outputs (out/main*.json, out/e2*.json, explore).

Usage: python3 summarize.py
"""
import json
import os
import statistics as st
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'out')


def load(name):
    with open(os.path.join(OUT, name), encoding='utf-8') as f:
        return json.load(f)


def rows(name):
    with open(os.path.join(OUT, name), encoding='utf-8') as f:
        return [json.loads(line) for line in f]


def pct(x):
    return 'n/a' if not x else f"{100 * x['est']:.1f}% [{100 * x['lo']:.1f}, {100 * x['hi']:.1f}]"


def num(x, d=1):
    return 'n/a' if not x else f"{x['est']:.{d}f} [{x['lo']:.{d}f}, {x['hi']:.{d}f}]"


ARM_NAME = {'A1': 'Tofu replace-only', 'A2': 'Tofu insert-first', 'A3': 'Claude Code Edit', 'A4': 'Codex apply_patch'}


def main():
    m = load('main.json')
    r = rows('main.rows.jsonl')
    ex = load('main_explore.json')
    L = [f"# Forced-operation edit benchmark: results ({', '.join(m['models'])})", '',
         f"Exp 1: {m['n_attempts']} attempts (60 tasks x 4 arms x 3 models x 3 repeats); missing after retries: "
         f"{len(m['missing_http_error'])}. Mean [95% task-cluster bootstrap CI], models equally weighted. Payload = o200k "
         'tokens of all tool-call arguments in the attempt; completion = provider-reported output tokens incl. reasoning.', '',
         '## Exp 1 pooled', '', '| arm | final success | first-try | payload | completion (mean) | completion (median) | '
         'calls hitting max_tokens | schema-refused first call |', '|---|---|---|---|---|---|---|---|']
    length_by_arm = defaultdict(int)
    for k, v in ex['finish_length_calls'].items():
        length_by_arm[k.split('|')[1]] += v
    for arm in ('A1', 'A2', 'A3', 'A4'):
        p = m['pooled'][arm]
        med = st.median(x['completion'] for x in r if x['arm'] == arm)
        L.append(f"| {arm} {ARM_NAME[arm]} | {pct(p['final'])} | {pct(p['first'])} | {num(p['payload'])} | "
                 f"{num(p['completion'], 0)} | {med:.0f} | {length_by_arm[arm]} | {pct(p['schema_first'])} |")
    L += ['', '## Exp 1 per model', '', '| model | arm | final | first-try | payload | completion mean / median |',
          '|---|---|---|---|---|---|']
    for model in m['models']:
        for arm in ('A1', 'A2', 'A3', 'A4'):
            p = m['per_arm_model'][f'{arm}|{model}']
            med = st.median(x['completion'] for x in r if x['arm'] == arm and x['model'] == model)
            L.append(f"| {model} | {arm} | {pct(p['final'])} | {pct(p['first'])} | {num(p['payload'])} | "
                     f"{num(p['completion'], 0)} / {med:.0f} |")
    L += ['', '## A2 insert-first minus A1 replace (paired on model, task, repeat)', '',
          '| subset | d payload | d final | d first-try | d completion | pairs |', '|---|---|---|---|---|---|']
    for et in ('insert', 'multi', 'replace', 'delete'):
        for key in [f'A2-A1|{et}'] + [f'A2-A1|{et}|{s}' for s in 'SML']:
            v = m['paired'][key]
            L.append(f"| {key[6:]} | {num(v['payload'])} | {pct(v['final'])} | {pct(v['first'])} | "
                     f"{num(v['completion'], 0)} | {v['payload']['n'] if v['payload'] else 0} |")
    L += ['', 'Per model, insertion tasks only:', '']
    for model in m['models']:
        pm = load(f'main_{model}.json')
        v = pm['paired']['A2-A1|insert']
        L.append(f"- {model}: d payload {num(v['payload'])}, d final {pct(v['final'])}, d completion {num(v['completion'], 0)}")
    L += ['', '## Cross-harness (paired, all 60 tasks)', '', '| pair | d final | d first-try | d payload | d completion |',
          '|---|---|---|---|---|']
    for pair in ('A3-A1', 'A4-A1', 'A4-A3', 'A3-A2'):
        for et in ('all', 'insert', 'replace', 'delete', 'multi'):
            v = m['paired'][f'{pair}|{et}']
            L.append(f"| {pair} {et} | {pct(v['final'])} | {pct(v['first'])} | {num(v['payload'])} | {num(v['completion'], 0)} |")
    L += ['', '## Failure taxonomy (all models)', '']
    for arm in ('A1', 'A2', 'A3', 'A4'):
        t = m['taxonomy'][f'{arm}|all']
        L.append(f"- {arm} {ARM_NAME[arm]} (n={t['n']}): first tool error {t['first_error']}; wrong final file "
                 f"{t['final_class']}; tool said success but file wrong: {t['tool_ok_but_wrong']}")
    L += ['', '## Exploratory', '', f"- Insert ops chosen on first call: {ex['insert_task_ops_first_call']}",
          f"- Median anchor tokens on insertion tasks (value, n): {ex['insert_anchor_o200k_median']}",
          f"- Codex hunks: {ex['codex_context_lines_per_hunk']}",
          f"- Insert anchors not line-aligned on the insertion side (first call): {ex['insert_anchor_alignment_first_call']}",
          f"- Calls ending at max_tokens: {ex['finish_length_calls']}"]
    if os.path.exists(os.path.join(OUT, 'main_deepseek.json')):
        d = load('main_deepseek.json')
        L += ['', f"## DeepSeek-V4-Pro partial run (stopped; excluded above): {d['n_attempts']} attempts", '']
        for arm in ('A1', 'A2', 'A3', 'A4'):
            p = d['pooled'][arm]
            L.append(f"- {arm}: final {pct(p['final'])}, payload {num(p['payload'])} (n={p['final']['n'] if p['final'] else 0})")
    sections = (('e2_nomd.md', 'Exp 2 (write_file modes), E2a without the three defective md-log tasks (see Deviations)'),
                ('e2.md', 'Exp 2 as registered (all 21 tasks)'),
                ('e2_md.md', 'Exp 2 md-log tasks only (defective expected trailing blank line)'))
    for name, title in sections:
        if os.path.exists(os.path.join(OUT, name)):
            with open(os.path.join(OUT, name), encoding='utf-8') as f:
                L += ['', f'## {title}', '', f.read()]
    probe = os.path.join(ROOT, 'raw', 'probe_kimi_required.jsonl')
    if os.path.exists(probe):
        agg = defaultdict(lambda: [0, 0, 0, 0])
        with open(probe, encoding='utf-8') as f:
            for line in f:
                p = json.loads(line)
                v = agg[p['arm']]
                v[0] += p['final_success']
                v[1] += 1
                v[2] += sum(1 for shape in p['arg_shapes'] if shape == ['path'])
                v[3] += len(p['arg_shapes'])
        L += ['', '## Exploratory probe: kimi-k3 and write_file `required`', '']
        L += [f'- {arm}: success {v[0]}/{v[1]}, write_file calls with only `path` {v[2]}/{v[3]}' for arm, v in agg.items()]
    with open(os.path.join(ROOT, 'RESULTS.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(L) + '\n')
    print('\n'.join(L[:40]))


if __name__ == '__main__':
    main()
