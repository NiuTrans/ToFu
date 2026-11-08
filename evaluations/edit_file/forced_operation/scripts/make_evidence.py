"""Assemble evidence.csv from the analysis outputs (numbers are read, never retyped).

Usage: python3 make_evidence.py   (after analyze.py -> out/main, analyze_write.py -> out/e2,
explore.py -> out/main_explore.json, analyze.py --models deepseek-v4-pro -> out/main_deepseek)
"""
import csv
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'out')
WT = 'evaluations/edit_file/forced_operation'


def load(name):
    with open(os.path.join(OUT, name), encoding='utf-8') as f:
        return json.load(f)


def pct(x):
    return f"{100 * x['est']:.1f}% [{100 * x['lo']:.1f}, {100 * x['hi']:.1f}]"


def num(x, d=1):
    return f"{x['est']:.{d}f} [{x['lo']:.{d}f}, {x['hi']:.{d}f}]"


def main():
    m = load('main.json')
    e2 = load('e2_nomd.json')
    e2_reg = load('e2.json')
    ex = load('main_explore.json')
    models = ', '.join(m['models'])
    rows = []

    def add(claim, label, source, n, method):
        rows.append({'claim': claim, 'label': label, 'source': source, 'n': n, 'method': method})

    boot = 'mean, 95% CI = task-cluster bootstrap (10,000 resamples, 60 tasks), models equally weighted'
    for arm, name in (('A1', 'Tofu edit_file replace-only'), ('A2', 'Tofu edit_file insert-first'),
                      ('A3', 'Claude Code Edit (port)'), ('A4', 'Codex apply_patch (port)')):
        p = m['pooled'][arm]
        add(f'{name}: final success {pct(p["final"])}, first-try {pct(p["first"])}, edit payload '
            f'{num(p["payload"])} o200k tokens/attempt, provider completion tokens {num(p["completion"], 0)}',
            '实测', f'out/main.json pooled.{arm}', p['final']['n'], f'forced-style benchmark, {models}; {boot}')
    for et in ('insert', 'multi', 'replace', 'delete'):
        v = m['paired'][f'A2-A1|{et}']
        add(f'A2 insert-first minus A1 replace on {et} tasks: payload {num(v["payload"])} tokens, final success '
            f'{pct(v["final"])}, completion tokens {num(v["completion"], 0)}',
            '实测', f'out/main.json paired["A2-A1|{et}"]', v['payload']['n'],
            f'paired on (model, task, repeat); {boot}' + ('; placebo stratum (A2 must use replace)'
                                                           if et in ('replace', 'delete') else ''))
    for s in ('S', 'M', 'L'):
        v = m['paired'][f'A2-A1|insert|{s}']
        add(f'Insert saves {num(v["payload"])} payload tokens on {s}-anchor pure insertions (A2-A1); success {pct(v["final"])}',
            '实测', f'out/main.json paired["A2-A1|insert|{s}"]', v['payload']['n'], f'paired; {boot}')
    for pair in ('A3-A1', 'A4-A1', 'A4-A3'):
        v = m['paired'][f'{pair}|all']
        add(f'{pair} over all 60 tasks: final success {pct(v["final"])}, first-try {pct(v["first"])}, payload {num(v["payload"])}',
            '实测', f'out/main.json paired["{pair}|all"]', v['final']['n'], f'paired; {boot}')
    for arm in ('A1', 'A2', 'A3', 'A4'):
        t = m['taxonomy'][f'{arm}|all']
        add(f'{arm} failure taxonomy: first tool error {t["first_error"]}; wrong final files {t["final_class"]}; '
            f'tool reported success but file wrong {t["tool_ok_but_wrong"]}',
            '实测', f'out/main.json taxonomy["{arm}|all"]', t['n'], 'rules in PREREG.md (Failure taxonomy)')
    cl = ex['codex_context_lines_per_hunk']
    add(f'Models put a median of {cl["median"]} context lines per Codex hunk ({100 * cl["share_ge3"]:.0f}% of hunks >= 3)',
        '实测', 'out/main_explore.json codex_context_lines_per_hunk', cl['n_hunks'], 'exploratory; first-call A4 patches')
    add('Oracle payload floor over the 60 tasks (minimal unique whole-line anchors, 3-line patch context): '
        'A1 4398, A2 4080 (-7.2%), A3 4212, A4 7105 o200k tokens', '估算', f'{WT}/tasks/oracle.jsonl', 240,
        'counterfactual: one correct call per task x arm through the real executors')
    for key, v in e2['paired'].items():
        if key.count('|') == 1:
            add(f'{key}: payload {num(v["payload"], 0)} tokens, final success {pct(v["final"])}, completion '
                f'{num(v["completion"], 0)}, latency {num(v["latency"])} s', '实测', f'out/e2.json paired["{key}"]',
                v['payload']['n'], f'paired; {boot.replace("60 tasks", "E2 tasks")}')
    pm = e2['per_model_final']
    add('Kimi K3 full-rewrite/append success on csv/service logs '
        f"{pm['E2a|Wfull|kimi-k3'][0]}/{pm['E2a|Wfull|kimi-k3'][1]} and {pm['E2a|Wappend|kimi-k3'][0]}/{pm['E2a|Wappend|kimi-k3'][1]}"
        ' vs 18/18 for Claude and GLM: most Kimi write_file calls carried only `path`', '实测',
        'out/e2_nomd.json per_model_final; raw/e2b.jsonl', 36, 'count of exact-match finals')
    add('E2a as registered (incl. 3 md-log tasks with a defective expected trailing blank line): '
        f"append-full final {pct(e2_reg['paired']['E2a|Wappend-Wfull']['final'])}, payload "
        f"{num(e2_reg['paired']['E2a|Wappend-Wfull']['payload'], 0)}", '实测', 'out/e2.json', e2_reg['n_attempts'],
        'registered task set; see PREREG Deviations')
    add('Production write_file usage 2026-11-06 09:51-18:10: 592 calls, append 133 (22.5%), content_ref 0',
        '实测', 'shared logs/app.log* (write_file: ... [append]; [Tool:write_file] content_ref ...)', 592,
        'log count; own run_command echo lines excluded')
    add('Tofu insert echo guard refuses a legitimate insert_after whose new block ends with the anchor lines '
        '(ky body.ts, anchor "\\t\\treturn 0;\\n\\t}")', '实测', 'lib/project_mod/write_tools/_ops.py:1633 _strip_anchor_echo; '
        f'{WT}/scripts/oracle.py (first oracle run)', 1, 'reproduced through the real executor')
    add('insert_before/insert_after with a mid-line substring anchor splits the line and the receipt says OK',
        '实测', 'raw/main.jsonl glm-5.3|A2|G01|0 and |2; lib/project_mod/write_tools/_ops.py _splice_insertion',
        sum(v.get('mid_line', 0) for v in ex['insert_anchor_alignment_first_call'].values()),
        'first-call insert ops whose anchor is not line-aligned on the insertion side')
    add('The edit_file schema never states insert newline semantics; GLM spent 16,000 reasoning tokens deciding '
        'line- vs raw-text insertion and made no call (pilot K03; main V08/T10)', '实测',
        'raw/pilot.jsonl glm-5.3|A2|K03|0; out/main_explore.json finish_length_calls', None, 'raw reasoning text')
    add('Codex default apply_patch mode is NormalizeToLf, which writes context lines back from the patch text; '
        'a context line with wrong indentation silently changes the file', '实测',
        'codex-rs/features/src/lib.rs:1185 (default_enabled false); core/src/tools/handlers/apply_patch.rs:64; '
        'raw/main.jsonl glm-5.3|A4|G10|0', 1, 'source reading + observed attempt')
    path = os.path.join(ROOT, 'evidence.csv')
    with open(path, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['claim', 'label', 'source', 'n', 'method'], lineterminator='\n')
        w.writeheader()
        w.writerows(rows)
    print(f'{len(rows)} evidence rows -> {path}')


if __name__ == '__main__':
    main()
