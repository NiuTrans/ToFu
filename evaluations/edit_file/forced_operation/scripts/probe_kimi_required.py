"""Exploratory follow-up (not pre-registered): does kimi-k3 omit write_file `content` because
the production schema only requires `path`? Same Wfull arm, once as frozen and once with
`content` added to `required`; 2 tasks x 3 repeats each. Writes raw/probe_kimi_required.jsonl."""
import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import run_bench as rb  # noqa: E402
import run_write as rw  # noqa: E402


def main():
    arms = json.load(open(os.path.join(rw.ROOT, 'tasks', 'write_arms.json'), encoding='utf-8'))
    base = arms['Wfull']
    req = copy.deepcopy(base)
    req['tool']['function']['parameters']['required'] = ['path', 'content']
    tasks = [t for t in rw.load_tasks() if t['id'] in ('Wa-csv-S', 'Wa-svc-S')]
    worker = rb.TofuWorker('probe-kimi')
    out = os.path.join(rw.ROOT, 'raw', 'probe_kimi_required.jsonl')
    for variant, arm in (('frozen', base), ('content_required', req)):
        for task in tasks:
            for rep in range(3):
                rec = rw.run_attempt('kimi-k3', f'Wfull-{variant}', arm, task, rep, worker)
                shapes = [sorted(json.loads(t['arguments_head']).keys()) if t['arguments_head'].endswith('}') else ['<long>']
                          for c in rec['calls'] for t in (c.get('tool_calls') or [])]
                rec['arg_shapes'] = shapes
                with open(out, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + '\n')
                print(variant, task['id'], rep, rec['status'], shapes, flush=True)


if __name__ == '__main__':
    main()
