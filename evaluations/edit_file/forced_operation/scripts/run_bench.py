"""Run the forced-operation edit benchmark through Tofu's own model route.

Usage:
  python3 run_bench.py --run pilot --models glm-5.3 --tasks A01,K03,F06,T08,V10 --repeats 1
  python3 run_bench.py --run main --models glm-5.3,deepseek-v4-pro,claude-opus-5 --repeats 3

One attempt = (model, arm, task, repeat): system+user prompt, then up to
MAX_MODEL_CALLS model calls. After every model response its tool calls are executed
in order by the arm's executor (A1/A2: Tofu's production ingest+executor in a hermetic
worker; A3: CC Edit port; A4: Codex apply_patch port). The attempt stops when the file
equals the expected result, when the model answers without a tool call, or after
MAX_MODEL_CALLS calls. One JSON line per finished attempt is appended to
raw/<run>.jsonl; finished keys are skipped on restart (resumable).
"""
import argparse
import concurrent.futures as cf
import difflib
import hashlib
import json
import os
import random
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import tiktoken

sys.path.insert(0, os.path.dirname(__file__))
import cc_edit  # noqa: E402
import codex_patch  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKTREE = os.environ.get('EDITBENCH_TOFU_WORKTREE', '')
URL = 'http://127.0.0.1:15000/v1/chat/completions'
MAX_MODEL_CALLS = 3
MAX_TOKENS = 16000
HTTP_RETRIES = 5
ENC = tiktoken.get_encoding('o200k_base')
WRITE_LOCK = threading.Lock()


def ntok(text):
    return len(ENC.encode(text or '', disallowed_special=()))


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def load_tasks():
    rows = [json.loads(line) for line in open(os.path.join(ROOT, 'tasks', 'tasks.jsonl'), encoding='utf-8')]
    for row in rows:
        with open(os.path.join(ROOT, 'tasks', 'files', f"{row['file_key']}.orig"), encoding='utf-8', newline='') as f:
            row['original'] = f.read()
        with open(os.path.join(ROOT, 'tasks', 'files', f"{row['id']}.expected"), encoding='utf-8', newline='') as f:
            row['expected'] = f.read()
        assert sha(row['original']) == row['source_sha256'] and sha(row['expected']) == row['expected_sha256']
    return rows


def virtual_root(task):
    return f"/workspace/{task['repo']}"


def user_message(task):
    view = '\n'.join(f'{n:>6}\t{line}' for n, line in enumerate(task['original'][:-1].split('\n'), 1))
    root = virtual_root(task)
    return (f"Current content of `{task['path']}` (absolute path: {root}/{task['path']}):\n\n"
            f"{view}\n\n{task['instruction']}")


class TofuWorker:
    def __init__(self, tag):
        self.lock = threading.Lock()
        scratch = os.path.join(ROOT, 'scratch', f'tofu-{tag}-{os.getpid()}')
        for name in ('home', 'tmp', 'runtime', 'cache'):
            os.makedirs(os.path.join(scratch, name), exist_ok=True)
        env = {'PATH': '/usr/bin:/bin', 'HOME': f'{scratch}/home', 'TMPDIR': f'{scratch}/tmp',
               'TOFU_DATA_DIR': f'{scratch}/runtime', 'XDG_CACHE_HOME': f'{scratch}/cache',
               'XDG_DATA_HOME': f'{scratch}/runtime', 'PYTHONDONTWRITEBYTECODE': '1',
               'PYTHONHASHSEED': '0', 'PYTHONPATH': WORKTREE, 'LANG': 'C.UTF-8'}
        self.proc = subprocess.Popen(
            [sys.executable, '-B', os.path.join(ROOT, 'scripts', 'tofu_worker.py'), scratch],
            cwd=scratch, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=open(os.path.join(ROOT, 'logs', f'tofu_worker_{tag}.err'), 'a'), text=True, bufsize=1)
        ready = json.loads(self.proc.stdout.readline())
        assert ready.get('ready'), ready

    def call(self, req):
        with self.lock:
            self.proc.stdin.write(json.dumps(req) + '\n')
            self.proc.stdin.flush()
            return json.loads(self.proc.stdout.readline())


def make_resolver(task):
    root = virtual_root(task)

    def resolve(path):
        if not isinstance(path, str):
            return None
        p = path.strip()
        if p.startswith(root + '/'):
            p = p[len(root) + 1:]
        while p.startswith('./'):
            p = p[2:]
        return p if p == task['path'] else None
    return resolve


def execute(arm_key, arm, task, call, content, worker, model, attempt_id):
    """Run one tool call; return (new_content, record)."""
    fn = call.get('function') or {}
    name, raw = fn.get('name'), fn.get('arguments')
    rec = {'name': name, 'arguments': raw, 'args_o200k': ntok(raw if isinstance(raw, str) else json.dumps(raw))}
    if arm['executor'] == 'tofu':
        reply = worker.call({'id': attempt_id, 'tool_call': {'function': {'name': name, 'arguments': raw}},
                             'schema': arm['tool'], 'path': task['path'], 'content': content,
                             'virtual_root': virtual_root(task), 'model': model})
        rec.update(output=reply['output'], admitted=reply['admitted'], repair_log=reply['repair_log'],
                   contract_error=reply['contract_error'], harness_error=reply.get('harness_error', False))
        new = reply['content'] if reply['content'] is not None else content
        ok = reply['admitted'] and reply['output'].startswith('Applied ') and ' failed' not in reply['output'].split('\n')[0]
        rec['ok'] = bool(ok)
        return new, rec
    files = {task['path']: content}
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as e:
        rec.update(ok=False, output=f'Error: tool arguments are not valid JSON: {e}', error_kind='json')
        return content, rec
    expected_name = arm['tool']['function']['name']
    if name != expected_name:
        rec.update(ok=False, output=f'Error: No such tool available: {name}', error_kind='unknown_tool')
        return content, rec
    if arm['executor'] == 'cc':
        ok, out, kind = cc_edit.cc_edit(files, args, make_resolver(task))
        rec.update(ok=ok, output=out, error_kind=kind)
    else:
        patch = args.get('input') if isinstance(args, dict) else None
        if not isinstance(patch, str):
            rec.update(ok=False, output='Error: missing required argument `input`.', error_kind='schema')
            return content, rec
        rec['patch_o200k'] = ntok(patch)
        ok, out = codex_patch.apply_patch(patch, files, make_resolver(task))
        rec.update(ok=ok, output=out, error_kind=None if ok else 'patch')
    return files.get(task['path'], content), rec


def post(body):
    data = json.dumps(body).encode('utf-8')
    last = None
    for attempt in range(HTTP_RETRIES + 1):
        req = urllib.request.Request(URL, data=data, headers={'Content-Type': 'application/json'})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                return json.loads(r.read()), r.status, time.time() - t0, attempt
        except urllib.error.HTTPError as e:
            last = (e.code, e.read()[:2000].decode('utf-8', 'replace'))
            if e.code == 400 and 'model_route_unavailable' not in last[1]:
                return {'error': last[1]}, e.code, time.time() - t0, attempt
        except Exception as e:  # noqa: BLE001 - transport failures are retried, then recorded
            last = (None, repr(e)[:500])
        time.sleep(min(240, 20 * (2 ** attempt)) * (0.75 + random.random() / 2))
    return {'error': last[1]}, last[0], 0.0, HTTP_RETRIES


def run_attempt(model, arm_key, arm, task, repeat, worker):
    attempt_id = f'{model}|{arm_key}|{task["id"]}|{repeat}'
    root = virtual_root(task)
    messages = [{'role': 'system', 'content': arm['system'].replace('{root}', root)},
                {'role': 'user', 'content': user_message(task)}]
    content = task['original']
    record = {'key': attempt_id, 'model': model, 'arm': arm_key, 'arm_name': arm['name'],
              'task': task['id'], 'repeat': repeat, 'edit_type': task['edit_type'],
              'stratum': task['stratum'], 'file_key': task['file_key'], 'started': time.time(),
              'prompt_sha256': sha(json.dumps(messages)), 'calls': []}
    first_try = None
    status = 'max_calls'
    for i in range(MAX_MODEL_CALLS):
        body = {'model': model, 'messages': messages, 'tools': [arm['tool']], 'tool_choice': 'auto',
                'stream': False, 'max_tokens': MAX_TOKENS, 'timeout_s': 900}
        payload, http, latency, retries = post(body)
        call = {'i': i, 'http': http, 'latency_s': round(latency, 2), 'http_retries': retries}
        if http != 200 or 'choices' not in payload:
            call['error'] = payload.get('error') if isinstance(payload, dict) else str(payload)[:2000]
            record['calls'].append(call)
            status = 'http_error'
            break
        msg = payload['choices'][0]['message']
        tool_calls = msg.get('tool_calls') or []
        call.update(usage=payload.get('usage'), finish_reason=payload['choices'][0].get('finish_reason'),
                    content=msg.get('content'), reasoning=(msg.get('reasoning_content') or '')[:20000],
                    reasoning_chars=len(msg.get('reasoning_content') or ''), tool_calls=[])
        call['visible_o200k'] = ntok(msg.get('content') or '') + sum(
            ntok((c.get('function') or {}).get('arguments') or '') for c in tool_calls)
        assistant = {'role': 'assistant', 'content': msg.get('content') or ''}
        if msg.get('reasoning_content'):
            assistant['reasoning_content'] = msg['reasoning_content']
        if tool_calls:
            assistant['tool_calls'] = tool_calls
        messages.append(assistant)
        for tc in tool_calls:
            content, rec = execute(arm_key, arm, task, tc, content, worker, model, f'{attempt_id}|{i}')
            rec['file_sha_after'] = sha(content)
            call['tool_calls'].append(rec)
            messages.append({'role': 'tool', 'tool_call_id': tc.get('id'), 'content': rec['output']})
        record['calls'].append(call)
        if i == 0:
            first_try = content == task['expected']
        if content == task['expected']:
            status = 'success'
            break
        if not tool_calls:
            status = 'stopped'
            break
    record.update(status=status, first_try_success=bool(first_try), final_success=content == task['expected'],
                  final_sha=sha(content), changed=content != task['original'],
                  n_model_calls=len(record['calls']),
                  n_tool_calls=sum(len(c.get('tool_calls') or []) for c in record['calls']),
                  finished=time.time())
    if not record['final_success']:
        diff = list(difflib.unified_diff(task['expected'].split('\n'), content.split('\n'),
                                         'expected', 'actual', lineterm='', n=1))
        record['final_diff'] = '\n'.join(diff[:80])
        record['final_text'] = content
    return record


def done_keys(path):
    keys = set()
    if os.path.exists(path):
        for line in open(path, encoding='utf-8'):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get('status') != 'http_error':
                keys.add(row['key'])
    return keys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', required=True)
    ap.add_argument('--models', required=True)
    ap.add_argument('--arms', default='A1,A2,A3,A4')
    ap.add_argument('--tasks', default='all')
    ap.add_argument('--repeats', type=int, default=3)
    ap.add_argument('--concurrency', default='4',
                    help='parallel attempts per model: N, or model=N,... (unlisted models get 1)')
    args = ap.parse_args()
    if not WORKTREE:
        raise SystemExit('set EDITBENCH_TOFU_WORKTREE to the Tofu worktree (read-only use)')
    tasks = load_tasks()
    if args.tasks != 'all':
        wanted = args.tasks.split(',')
        tasks = [t for t in tasks if t['id'] in wanted]
    arms = json.load(open(os.path.join(ROOT, 'tasks', 'arms.json'), encoding='utf-8'))['arms']
    out_path = os.path.join(ROOT, 'raw', f'{args.run}.jsonl')
    finished = done_keys(out_path)
    worker = TofuWorker(args.run)
    jobs = []
    for model in args.models.split(','):
        for repeat in range(args.repeats):
            for task in tasks:
                for arm_key in args.arms.split(','):
                    key = f'{model}|{arm_key}|{task["id"]}|{repeat}'
                    if key not in finished:
                        jobs.append((model, arm_key, task, repeat))
    random.Random(20261106).shuffle(jobs)
    print(f'{len(jobs)} attempts to run ({len(finished)} already done) -> {out_path}', flush=True)
    by_model = {}
    for job in jobs:
        by_model.setdefault(job[0], []).append(job)
    if '=' in args.concurrency:
        limits = {k: int(v) for k, v in (item.split('=') for item in args.concurrency.split(','))}
    else:
        limits = {m: int(args.concurrency) for m in by_model}
    pools = {m: cf.ThreadPoolExecutor(max_workers=limits.get(m, 1)) for m in by_model}
    futures = []
    for model, model_jobs in by_model.items():
        for job in model_jobs:
            futures.append(pools[model].submit(run_attempt, job[0], job[1], arms[job[1]], job[2], job[3], worker))
    n = 0
    for fut in cf.as_completed(futures):
        rec = fut.result()
        with WRITE_LOCK, open(out_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
        n += 1
        print(f"[{n}/{len(jobs)}] {rec['key']} {rec['status']} first={rec['first_try_success']} "
              f"calls={rec['n_model_calls']}", flush=True)
    for pool in pools.values():
        pool.shutdown()


if __name__ == '__main__':
    main()
