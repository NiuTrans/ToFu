"""Offline oracle: build a correct minimal-context tool call per (task, arm), execute it
through the same executors as the benchmark, and record its payload size.

Checks every task is solvable in every arm and that the executors agree with the
reference expected file. Payload sizes are the counterfactual floor per arm (o200k).
Anchors are the shortest whole-line block at the site that is unique in the file
(the habit the edit tools' descriptions ask for). Codex patches use difflib hunks
with 3 context lines (the Codex patch convention); a 1-context variant is recorded too.

Usage: EDITBENCH_TOFU_WORKTREE=<worktree> python3 oracle.py
"""
import difflib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import run_bench as rb  # noqa: E402


def unique_block(lines, start, end, grow):
    """Smallest block lines[start:end] (0-based, end exclusive) grown in direction `grow`
    ('up', 'down' or 'both') until its text occurs exactly once as a substring of the
    file (what edit_file, Edit and patch context matching all require)."""
    text = '\n'.join(lines) + '\n'
    while True:
        block = '\n'.join(lines[start:end])
        if block.strip() and text.count(block) == 1:
            return start, end
        if grow in ('up', 'both') and start > 0:
            start -= 1
        if grow in ('down', 'both') and end < len(lines):
            end += 1
        if start == 0 and end == len(lines):
            return start, end


def echoes(anchor, content, position):
    """Tofu's far-side echo guard: content ending (after) / starting (before) with the
    complete whole-line anchor is refused, so a careful caller widens the anchor."""
    a = anchor.split('\n')
    body = content[:-1] if content.endswith('\n') else content
    c = body.split('\n')
    if len(c) < len(a):
        return False
    run = c[-len(a):] if position == 'after' else c[:len(a)]
    return run == a and any(ch.isalnum() for ch in anchor)


def site_edits(task, arm_key):
    lines = task['original'][:-1].split('\n')
    edits = []
    for site in task['sites']:
        kind = site['kind']
        if kind == 'ins':
            n = site['line'] - 1
            new = site['text'][:-1]
            if site['position'] == 'after':
                s, e = unique_block(lines, n, n + 1, 'up')
                while arm_key == 'A2' and s > 0 and echoes('\n'.join(lines[s:e]), site['text'], 'after'):
                    s -= 1
                anchor = '\n'.join(lines[s:e])
                if arm_key == 'A2':
                    edits.append({'operation': 'insert_after', 'anchor': anchor, 'content': site['text']})
                else:
                    edits.append({'operation': 'replace', 'anchor': anchor, 'content': anchor + '\n' + new})
            else:
                s, e = unique_block(lines, n, n + 1, 'down')
                while arm_key == 'A2' and e < len(lines) and echoes('\n'.join(lines[s:e]), site['text'], 'before'):
                    e += 1
                anchor = '\n'.join(lines[s:e])
                if arm_key == 'A2':
                    edits.append({'operation': 'insert_before', 'anchor': anchor, 'content': site['text']})
                else:
                    edits.append({'operation': 'replace', 'anchor': anchor, 'content': new + '\n' + anchor})
        elif kind in ('rep_span', 'del_span'):
            n = site['line'] - 1
            s, e = unique_block(lines, n, n + 1, 'both')
            old = '\n'.join(lines[s:e])
            changed = list(lines[s:e])
            changed[n - s] = changed[n - s].replace(site['old'], site.get('new', ''), 1)
            edits.append({'operation': 'replace', 'anchor': old, 'content': '\n'.join(changed)})
        elif kind == 'rep_lines':
            s, e = unique_block(lines, site['start'] - 1, site['end'], 'both')
            old = '\n'.join(lines[s:e])
            new = lines[s:site['start'] - 1] + site['text'][:-1].split('\n') + lines[site['end']:e]
            edits.append({'operation': 'replace', 'anchor': old, 'content': '\n'.join(new)})
        else:
            s, e = unique_block(lines, site['start'] - 1, site['end'], 'both')
            keep_before = lines[s:site['start'] - 1]
            keep_after = lines[site['end']:e]
            old = '\n'.join(lines[s:e]) + '\n'
            new = '\n'.join(keep_before + keep_after)
            edits.append({'operation': 'replace', 'anchor': old, 'content': new + '\n' if new else ''})
    return edits


def codex_patch_text(task, context):
    a = task['original'].split('\n')
    b = task['expected'].split('\n')
    out = ['*** Begin Patch', f"*** Update File: {task['path']}"]
    for group in difflib.SequenceMatcher(None, a, b).get_grouped_opcodes(context):
        out.append('@@')
        for tag, i1, i2, j1, j2 in group:
            if tag == 'equal':
                out += [' ' + line for line in a[i1:i2]]
            else:
                out += ['-' + line for line in a[i1:i2]]
                out += ['+' + line for line in b[j1:j2]]
    out.append('*** End Patch')
    return '\n'.join(out)


def calls_for(task, arm_key):
    if arm_key in ('A1', 'A2'):
        args = {'path': task['path'], 'edits': site_edits(task, arm_key)}
        return [{'function': {'name': 'edit_file', 'arguments': json.dumps(args, ensure_ascii=False)}}]
    if arm_key == 'A3':
        calls = []
        for e in site_edits(task, 'A1'):
            args = {'file_path': f"{rb.virtual_root(task)}/{task['path']}",
                    'old_string': e['anchor'], 'new_string': e['content']}
            calls.append({'function': {'name': 'Edit', 'arguments': json.dumps(args, ensure_ascii=False)}})
        return calls
    return [{'function': {'name': 'apply_patch',
                          'arguments': json.dumps({'input': codex_patch_text(task, 3)}, ensure_ascii=False)}}]


def main():
    tasks = rb.load_tasks()
    arms = json.load(open(os.path.join(rb.ROOT, 'tasks', 'arms.json'), encoding='utf-8'))['arms']
    worker = rb.TofuWorker('oracle')
    rows, bad = [], []
    for task in tasks:
        for arm_key, arm in arms.items():
            content = task['original']
            recs = []
            for call in calls_for(task, arm_key):
                content, rec = rb.execute(arm_key, arm, task, call, content, worker, 'oracle', task['id'])
                recs.append(rec)
            ok = content == task['expected']
            row = {'task': task['id'], 'arm': arm_key, 'edit_type': task['edit_type'],
                   'stratum': task['stratum'], 'ok': ok,
                   'args_o200k': sum(r['args_o200k'] for r in recs), 'n_calls': len(recs)}
            if arm_key == 'A4':
                row['patch_o200k'] = recs[0].get('patch_o200k')
                row['patch1_o200k'] = rb.ntok(codex_patch_text(task, 1))
            rows.append(row)
            if not ok:
                bad.append((task['id'], arm_key, [r['output'][:300] for r in recs]))
    with open(os.path.join(rb.ROOT, 'tasks', 'oracle.jsonl'), 'w', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row) + '\n')
    print(f'{len(rows)} oracle executions, {len(bad)} failures')
    for item in bad[:20]:
        print(item)
    sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
