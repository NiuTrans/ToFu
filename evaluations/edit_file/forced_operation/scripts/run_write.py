"""Experiment 2 (write_file modes) runner and arm builder.

Usage:
  (hermetic env, PYTHONPATH=<worktree>) python3 run_write.py --build-arms
  EDITBENCH_TOFU_WORKTREE=<worktree> python3 run_write.py --run e2 --models a,b,c --repeats 3

Arms: Wfull (write_file content only), Wappend (write_file + append), Wref
(write_file + content_ref), Eedit (edit_file replace-only = Exp 1 arm A1).
E2a tasks compare Wfull vs Wappend, E2b Wfull vs Wref, E2c Wfull vs Eedit.
Execution: Tofu's real ingest_tool_call + lib.project_mod.tools._exec_write_file
(content_ref resolved by lib.tasks_pkg.executor._content_ref._resolve_content_ref,
as lib/tasks_pkg/handlers/project.py does) or tool_edit_file, in the hermetic worker.
Same stop rule as Exp 1 (expected file reached, no tool call, or 3 model calls).
"""
import argparse
import concurrent.futures as cf
import copy
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_MODEL_CALLS = 3
MAX_TOKENS = 32000
PAIRS = {'E2a': ('Wfull', 'Wappend'), 'E2b': ('Wfull', 'Wref'), 'E2c': ('Wfull', 'Eedit')}
STYLE = {
    'Wfull': ('Edit style for this session: call `write_file` with `content` set to the complete file '
              'content (every line of the file, including unchanged ones).'),
    'Wappend': ('Edit style for this session: call `write_file` with `append: true` and `content` set to '
                'only the new text to add at the end of the file.'),
    'Wref': ('Edit style for this session: call `write_file` with `content_ref` '
             '({tool_round, start, end}) so the earlier tool output is reused instead of retyped.'),
}
RUN_COMMAND_STUB = {'type': 'function', 'function': {
    'name': 'run_command', 'description': 'Run a shell command (not available in this session).',
    'parameters': {'type': 'object', 'properties': {'command': {'type': 'string'}},
                   'required': ['command'], 'additionalProperties': False}}}


def must_replace(text, old, new):
    if text.count(old) != 1:
        raise SystemExit(f'production write_file text drifted: {old!r}')
    return text.replace(old, new)


def build_arms():
    from lib.tools.project import PROJECT_TOOL_WRITE_FILE
    base = copy.deepcopy(PROJECT_TOOL_WRITE_FILE)
    desc = base['function']['description']
    append_sentence = (' Set append=true to append to the end (a missing file is created): send only the new '
                       'bytes, no read or anchor needed.')
    ref_clause = ' or content_ref to reuse all/a slice of a previous tool result'
    full = copy.deepcopy(base)
    d = must_replace(desc, 'Create or replace one whole file, or append to its end. For new files/major '
                           'rewrites; use edit_file for mid-file changes.', 'Create or replace one whole file.')
    d = must_replace(d, append_sentence, '')
    full['function']['description'] = must_replace(d, ref_clause, '')
    for key in ('append', 'content_ref'):
        del full['function']['parameters']['properties'][key]
    app = copy.deepcopy(base)
    app['function']['description'] = must_replace(desc, ref_clause, '')
    del app['function']['parameters']['properties']['content_ref']
    ref = copy.deepcopy(base)
    d = must_replace(desc, 'Create or replace one whole file, or append to its end.', 'Create or replace one whole file.')
    ref['function']['description'] = must_replace(d, append_sentence, '')
    del ref['function']['parameters']['properties']['append']
    arms1 = json.load(open(os.path.join(ROOT, 'tasks', 'arms.json'), encoding='utf-8'))
    system_base = arms1['meta']['system_base']
    arms = {k: {'name': k, 'tool': t, 'system': system_base + '\n\n' + STYLE[k]}
            for k, t in (('Wfull', full), ('Wappend', app), ('Wref', ref))}
    arms['Eedit'] = {'name': 'Eedit', 'tool': arms1['arms']['A1']['tool'], 'system': arms1['arms']['A1']['system']}
    with open(os.path.join(ROOT, 'tasks', 'write_arms.json'), 'w', encoding='utf-8') as f:
        json.dump(arms, f, ensure_ascii=False, indent=1)
    for k, a in arms.items():
        print(k, sorted(a['tool']['function']['parameters']['properties']))


def load_tasks():
    rows = [json.loads(line) for line in open(os.path.join(ROOT, 'tasks', 'write_tasks.jsonl'), encoding='utf-8')]
    for row in rows:
        for ext in ('orig', 'expected'):
            with open(os.path.join(ROOT, 'tasks', 'files', f"{row['id']}.{ext}"), encoding='utf-8', newline='') as f:
                row['original' if ext == 'orig' else 'expected'] = f.read()
    return rows


def messages_for(task, arm):
    import run_bench as rb
    root = '/workspace/project'
    msgs = [{'role': 'system', 'content': arm['system'].replace('{root}', root)}]
    if task['exp'] == 'E2b':
        msgs += [{'role': 'user', 'content': f"Run `{task['command']}`."},
                 {'role': 'assistant', 'content': '', 'tool_calls': [{
                     'id': 'call_round1', 'type': 'function',
                     'function': {'name': 'run_command', 'arguments': json.dumps({'command': task['command']})}}]},
                 {'role': 'tool', 'tool_call_id': 'call_round1', 'content': task['tool_content']},
                 {'role': 'user', 'content': task['instruction']}]
        return msgs, root
    view = '\n'.join(f'{n:>6}\t{line}' for n, line in enumerate(task['original'][:-1].split('\n'), 1))
    msgs.append({'role': 'user', 'content': (f"Current content of `{task['path']}` (absolute path: "
                                             f"{root}/{task['path']}):\n\n{view}\n\n{task['instruction']}")})
    rb.ntok('')
    return msgs, root


def run_attempt(model, arm_key, arm, task, repeat, worker):
    import run_bench as rb
    key = f'{model}|{arm_key}|{task["id"]}|{repeat}'
    messages, root = messages_for(task, arm)
    tools = [arm['tool']] + ([RUN_COMMAND_STUB] if task['exp'] == 'E2b' else [])
    content = task['original'] if task['exp'] != 'E2b' else None
    record = {'key': key, 'model': model, 'arm': arm_key, 'task': task['id'], 'exp': task['exp'],
              'size': task['size'], 'repeat': repeat, 'calls': [], 'started': time.time()}
    first_try, status = None, 'max_calls'
    for i in range(MAX_MODEL_CALLS):
        body = {'model': model, 'messages': messages, 'tools': tools, 'tool_choice': 'auto',
                'stream': False, 'max_tokens': MAX_TOKENS, 'timeout_s': 900}
        payload, http, latency, retries = rb.post(body)
        call = {'i': i, 'http': http, 'latency_s': round(latency, 2), 'http_retries': retries}
        if http != 200 or 'choices' not in payload:
            call['error'] = payload.get('error') if isinstance(payload, dict) else str(payload)[:2000]
            record['calls'].append(call)
            status = 'http_error'
            break
        msg = payload['choices'][0]['message']
        tcs = msg.get('tool_calls') or []
        call.update(usage=payload.get('usage'), finish_reason=payload['choices'][0].get('finish_reason'),
                    content=(msg.get('content') or '')[:4000], reasoning_chars=len(msg.get('reasoning_content') or ''),
                    tool_calls=[])
        assistant = {'role': 'assistant', 'content': msg.get('content') or ''}
        if msg.get('reasoning_content'):
            assistant['reasoning_content'] = msg['reasoning_content']
        if tcs:
            assistant['tool_calls'] = tcs
        messages.append(assistant)
        for tc in tcs:
            fn = tc.get('function') or {}
            rec = {'name': fn.get('name'), 'arguments_head': (fn.get('arguments') or '')[:3000],
                   'args_o200k': rb.ntok(fn.get('arguments') or '')}
            if fn.get('name') == arm['tool']['function']['name']:
                reply = worker.call({'id': key, 'tool_call': {'function': {'name': fn.get('name'),
                                                                            'arguments': fn.get('arguments')}},
                                     'schema': arm['tool'], 'path': task['path'], 'content': content,
                                     'virtual_root': root, 'model': model, 'known_tools': [fn.get('name')],
                                     'prior_rounds': ([{'roundNum': task['tool_round'], 'toolContent': task['tool_content']}]
                                                      if task['exp'] == 'E2b' else [])})
                rec.update(output=reply['output'][:3000], admitted=reply['admitted'],
                           contract_error=reply['contract_error'])
                if reply['content'] is not None:
                    content = reply['content']
                try:
                    parsed = json.loads(fn.get('arguments') or '{}')
                    rec['used'] = sorted(k for k in ('content', 'content_ref', 'append') if k in parsed)
                    if isinstance(parsed.get('content_ref'), dict):
                        rec['content_ref'] = parsed['content_ref']
                except json.JSONDecodeError:
                    rec['used'] = ['bad_json']
            else:
                rec['output'] = f"Error: tool {fn.get('name')!r} is not available in this session."
            call['tool_calls'].append(rec)
            messages.append({'role': 'tool', 'tool_call_id': tc.get('id'), 'content': rec['output']})
        record['calls'].append(call)
        if i == 0:
            first_try = content == task['expected']
        if content == task['expected']:
            status = 'success'
            break
        if not tcs:
            status = 'stopped'
            break
    record.update(status=status, first_try_success=bool(first_try), final_success=content == task['expected'],
                  n_model_calls=len(record['calls']), finished=time.time())
    if not record['final_success']:
        record['final_text'] = content
    return record


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--build-arms', action='store_true')
    ap.add_argument('--run')
    ap.add_argument('--models')
    ap.add_argument('--tasks', default='all')
    ap.add_argument('--repeats', type=int, default=3)
    ap.add_argument('--concurrency', default='3', help='N, or model=N,... (unlisted models get 1)')
    args = ap.parse_args()
    if args.build_arms:
        return build_arms()
    import run_bench as rb
    tasks = load_tasks()
    if args.tasks != 'all':
        tasks = [t for t in tasks if t['id'] in args.tasks.split(',')]
    arms = json.load(open(os.path.join(ROOT, 'tasks', 'write_arms.json'), encoding='utf-8'))
    out_path = os.path.join(ROOT, 'raw', f'{args.run}.jsonl')
    done = rb.done_keys(out_path)
    worker = rb.TofuWorker(args.run)
    jobs = [(m, a, t, r) for m in args.models.split(',') for r in range(args.repeats) for t in tasks
            for a in PAIRS[t['exp']] if f'{m}|{a}|{t["id"]}|{r}' not in done]
    random.Random(20261106).shuffle(jobs)
    print(f'{len(jobs)} attempts ({len(done)} done) -> {out_path}', flush=True)
    by_model = {}
    for job in jobs:
        by_model.setdefault(job[0], []).append(job)
    if '=' in args.concurrency:
        limits = {k: int(v) for k, v in (item.split('=') for item in args.concurrency.split(','))}
    else:
        limits = {m: int(args.concurrency) for m in by_model}
    pools = {m: cf.ThreadPoolExecutor(max_workers=limits.get(m, 1)) for m in by_model}
    futures = [pools[j[0]].submit(run_attempt, j[0], j[1], arms[j[1]], j[2], j[3], worker)
               for js in by_model.values() for j in js]
    for n, fut in enumerate(cf.as_completed(futures), 1):
        rec = fut.result()
        with rb.WRITE_LOCK, open(out_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
        print(f"[{n}/{len(jobs)}] {rec['key']} {rec['status']} first={rec['first_try_success']}", flush=True)


if __name__ == '__main__':
    main()
