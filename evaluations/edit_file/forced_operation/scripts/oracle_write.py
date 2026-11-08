"""Offline oracle for Experiment 2: one correct call per (task, arm) through the hermetic worker."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import run_bench as rb  # noqa: E402
import run_write as rw  # noqa: E402


def oracle_args(task, arm):
    if arm == 'Wfull':
        return {'path': task['path'], 'content': task['expected']}
    if arm == 'Wappend':
        return {'path': task['path'], 'append': True, 'content': task['expected'][len(task['original']):]}
    if arm == 'Wref':
        start = task['tool_content'].index(task['expected'])
        return {'path': task['path'], 'content_ref': {'tool_round': 1, 'start': start, 'end': start + len(task['expected'])}}
    lines = task['original'][:-1].split('\n')
    exp = task['expected'][:-1].split('\n')
    i = next(k for k in range(len(lines)) if lines[k] != exp[k])
    j = i - 1
    while task['original'].count('\n'.join(lines[j:i])) != 1:
        j -= 1
    anchor = '\n'.join(lines[j:i])
    return {'path': task['path'], 'edits': [{'operation': 'replace', 'anchor': anchor, 'content': anchor + '\n' + exp[i]}]}


def main():
    tasks = rw.load_tasks()
    arms = json.load(open(os.path.join(rw.ROOT, 'tasks', 'write_arms.json'), encoding='utf-8'))
    worker = rb.TofuWorker('oracle-write')
    rows, bad = [], []
    for task in tasks:
        for arm in rw.PAIRS[task['exp']]:
            args = oracle_args(task, arm)
            name = arms[arm]['tool']['function']['name']
            raw = json.dumps(args, ensure_ascii=False)
            reply = worker.call({'id': task['id'], 'tool_call': {'function': {'name': name, 'arguments': raw}},
                                 'schema': arms[arm]['tool'], 'path': task['path'],
                                 'content': task['original'] if task['exp'] != 'E2b' else None,
                                 'virtual_root': '/workspace/project', 'known_tools': [name],
                                 'prior_rounds': ([{'roundNum': 1, 'toolContent': task['tool_content']}]
                                                  if task['exp'] == 'E2b' else [])})
            ok = reply['content'] == task['expected']
            rows.append({'task': task['id'], 'exp': task['exp'], 'size': task['size'], 'arm': arm,
                         'ok': ok, 'args_o200k': rb.ntok(raw)})
            if not ok:
                bad.append((task['id'], arm, reply['output'][:300]))
    with open(os.path.join(rw.ROOT, 'tasks', 'oracle_write.jsonl'), 'w', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row) + '\n')
    print(len(rows), 'oracle calls,', len(bad), 'failures')
    for b in bad:
        print(b)
    for row in rows:
        print(row['task'], row['arm'], row['args_o200k'])


if __name__ == '__main__':
    main()
