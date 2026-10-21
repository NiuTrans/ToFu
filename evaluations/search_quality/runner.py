"""Paired retrieval diagnostic using the native application agent/tool loop.

Entry: python -m evaluations.search_quality.runner (offline manifest), or
--live with explicit model, provider, worker interpreters and output directory.
Uses application_api.JsonAPI, programmatic_pilot.HTTPRuntime and the production
process-group retirement owner. No model transport, agent loop or interpreter.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

from evaluations.application_api import JsonAPI
from evaluations.programmatic_pilot.native_api import HTTPRuntime
from lib.project_mod.process_group import ProcessGroupRetirementError, retire_process_group
from .contracts import (
    ARMS, ARTIFACT_BYTES, CONTRACT, DDGS_VERSION, MAX_CALLS, PAGE_CHARS, RESULTS,
    SNIPPET_CHARS, SYSTEM, TASK_SECONDS, TOOL_SECONDS, agent_config, canonical, digest, score,
    python_tree_digest, task_prompt, tasks, tool_schemas, url_key,
)


def source_fingerprint(root):
    files = sorted((root / 'packages/tofu-search/tofu_search').rglob('*.py'))
    files += sorted((root / 'evaluations/search_quality').glob('*.py'))
    files += [root / p for p in (
        'packages/tofu-search/pyproject.toml',
        'evaluations/programmatic_pilot/native_api.py', 'evaluations/application_api.py',
        'lib/agent_core/run_contract.py', 'lib/project_mod/process_group.py')]
    return digest({str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in files})


def manifest(root, model, provider, trials, timeout, *, task_pack=None):
    default_pack = task_pack is None
    task_pack = json.loads(canonical(tasks() if default_pack else task_pack))
    if not isinstance(task_pack, list) or not task_pack:
        raise ValueError('A task pack must be a nonempty list')
    identifiers = [task.get('id') if isinstance(task, dict) else None for task in task_pack]
    if (any(not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,95}', name)
            for name in identifiers) or len(set(identifiers)) != len(identifiers)):
        raise ValueError('A task pack needs unique, safe artifact identifiers')
    return {'contract': CONTRACT, 'diagnostic_only': True, 'leadership_claim': False,
            'source_sha256': source_fingerprint(root), 'task_pack_sha256': digest(task_pack),
            'tofu_source_sha256': python_tree_digest(root / 'packages/tofu-search/tofu_search'),
            'reference_checked_on': '2026-10-21' if default_pack else None, 'tasks': task_pack,
            'task_prompts': {task['id']: task_prompt(task) for task in task_pack},
            'model': model, 'provider': provider, 'agent_config': agent_config(),
            'system': SYSTEM, 'tools': tool_schemas(timeout), 'trials': trials,
            'timeout_seconds': timeout, 'infrastructure_retries': 0,
            'limits': {'tool_calls': MAX_CALLS, 'tool_seconds': TOOL_SECONDS,
                       'results': RESULTS, 'snippet_chars': SNIPPET_CHARS,
                       'page_chars': PAGE_CHARS, 'artifact_bytes_per_task': ARTIFACT_BYTES},
            'arms': {'tofu': {'engines': 'default free web', 'fetch_pages': False,
                             'filter_pages': False, 'rerank': True},
                     'ddgs': {'version': DDGS_VERSION, 'backend': 'auto',
                              'request_timeout': 10, 'region': 'task language',
                              'safesearch': 'moderate'}},
            'common_fetch': 'current tofu-search, search-discovered URLs only',
            'network': 'one explicit proxy; TLS verified; no direct-path racing',
            'order': 'alternate arm order by task and repetition; sequential fresh workers',
            'seed': 20261021,
            'acceptance': 'all exact fact values, declared citation-host policies, observed quotations and '
                          'reference predicates; native task/model/tool provenance required',
            'limitations': [f'{len(task_pack)} diagnostic tasks; no population or leadership inference.',
                           'Retrieval comparison with common fetch, not two complete search products.',
                           'Reference predicates are not a semantic entailment judge.',
                           'JSON or one enclosing JSON fence is accepted; format compliance is separate.',
                           'References can change; revalidate before later cohorts.',
                           'Client source is pinned; running application source is not certified.',
                           'Recorded model cost is application accounting, not an invoice.']}


class Artifacts:
    def __init__(self, root):
        self.root, self.used = root, 0
        root.mkdir(mode=0o700, parents=True, exist_ok=False)

    def write(self, name, value):
        data = (canonical(value) + '\n').encode()
        if self.used + len(data) > ARTIFACT_BYTES:
            raise ValueError('Task artifact budget exhausted')
        with (self.root / name).open('xb') as stream:
            os.chmod(stream.name, 0o600)
            stream.write(data)
        self.used += len(data)


class Workers:
    def __init__(self, root, tofu_python, ddgs_python, proxy, *, env=None):
        self.root = root
        self.python = {'tofu': tofu_python, 'ddgs': ddgs_python}
        self.proxy = proxy
        inherited = os.environ if env is None else env
        self.env = {key: value for key, value in inherited.items() if key in {
            'PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'LD_LIBRARY_PATH',
            'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE',
            'PYTHONUTF8', 'PYTHONIOENCODING', 'PLAYWRIGHT_BROWSERS_PATH',
            'TOFU_SEARCH_EVAL_TOFU_LD_LIBRARY_PATH'}}
        self.env['PYTHONPATH'] = os.pathsep.join((str(root / 'packages/tofu-search'), str(root)))

    def call(self, arm, operation, args, *, region='us-en', seed=20261021, seconds=TOOL_SECONDS):
        request = dict(args, arm=arm, operation=operation, proxy=self.proxy, region=region, seed=seed)
        interpreter = self.python['tofu' if operation == 'fetch' else arm]
        environment = dict(self.env)
        library_path = environment.pop('TOFU_SEARCH_EVAL_TOFU_LD_LIBRARY_PATH', None)
        if library_path and (operation == 'fetch' or arm == 'tofu'):
            environment['LD_LIBRARY_PATH'] = library_path
        started = time.monotonic()
        process = subprocess.Popen(
            [interpreter, '-m', 'evaluations.search_quality.worker'], cwd=self.root,
            env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            raw, _ = process.communicate(canonical(request).encode(), timeout=seconds)
            if process.returncode:
                result = {'error': 'worker_exit', 'exit_code': process.returncode}
            elif len(raw) > 512 * 1024:
                result = {'error': 'worker_output_budget'}
            else:
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError('Worker result is not an object')
        except subprocess.TimeoutExpired:
            result = {'error': 'worker_timeout'}
        finally:
            # Reap the exact child AND retire any remaining browser descendants.
            # A failure here propagates and stops later dispatch; never call it clean.
            try:
                retire_process_group(process.pid,
                                     wait_leader=lambda timeout: process.wait(timeout=timeout))
            except Exception as exc:
                raise ProcessGroupRetirementError('Search worker cleanup not proven') from exc
            finally:
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        stream.close()
        return {**result, 'worker_seconds': time.monotonic() - started,
                'worker_pid': process.pid, 'worker_settled': True}


def execute_tool(workers, arm, task, name, arguments, calls, remaining):
    if len(calls) >= MAX_CALLS:
        raise ValueError('Tool-call budget exhausted')
    if name not in {'custom__search', 'custom__fetch'}:
        raise ValueError('Tool not admitted')
    field = 'query' if name == 'custom__search' else 'url'
    if (not isinstance(arguments, dict) or set(arguments) != {field}
            or not isinstance(arguments[field], str)
            or not 1 <= len(arguments[field]) <= (500 if field == 'query' else 4000)):
        return {'error': 'invalid_arguments'}
    if field == 'url':
        discovered = {url_key(r.get('url', '')) for call in calls
                      if call['name'] == 'custom__search' and call.get('accepted')
                      and not call.get('is_error')
                      for r in call['output'].get('results', [])}
        key = url_key(arguments['url'])
        if not key or key not in discovered:
            return {'error': 'url_not_returned_by_search'}
    if remaining <= 0:
        raise TimeoutError('Task deadline before tool dispatch')
    return workers.call(arm, 'search' if field == 'query' else 'fetch', arguments,
                        region=task['region'], seconds=min(TOOL_SECONDS, remaining))


def public_rounds(state):
    result = []
    for row in state.get('apiRounds') or []:
        usage = row.get('usage') or {}
        dispatch = usage.get('_dispatch') or {}
        result.append({'model': dispatch.get('model'), 'provider_id': dispatch.get('provider_id'),
                       'cost': row.get('cost'),
                       'usage': {k: v for k, v in usage.items()
                                 if k in {'prompt_tokens', 'completion_tokens', 'total_tokens',
                                          'prompt_tokens_details', 'completion_tokens_details'}}})
    return result


async def run_one(runtime, workers, task, arm, trial, directory, timeout):
    artifacts = Artifacts(directory)
    calls, events = [], []
    event_bytes = 0
    execution = None
    started = time.monotonic()
    report = {'task': task['id'], 'arm': arm, 'trial': trial, 'status': 'not_admitted',
              'failure': None, 'cleanup_failure': None, 'calls': calls}
    try:
        execution = runtime.start(
            [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': task_prompt(task)}],
            config=agent_config(), custom_tools=tool_schemas(timeout), custom_tools_mode='exclusive',
            trajectory='tofu-native', request_id=f'search-{task["id"]}-{arm}-{trial}',
            timeout_s=timeout)
        artifacts.write('admission.json', execution.admission)
        async for event in execution.events_async(timeout_s=timeout):
            event_bytes += len(canonical(event).encode())
            if event_bytes > ARTIFACT_BYTES:
                raise ValueError('Native event evidence exceeded task budget')
            # Keep wire/callback identities; full provider prompts and key hints are unnecessary.
            if event.get('type') in {'tool_wire_projection', 'custom_tool_call'}:
                events.append({k: v for k, v in event.items()
                               if k in {'type', 'taskId', 'callId', 'toolName', 'arguments',
                                        'schemaFingerprint', 'toolNames', 'executableToolCount'}})
            if event.get('type') != 'custom_tool_call':
                continue
            name, arguments = event.get('toolName'), event.get('arguments')
            receipt = await asyncio.to_thread(
                execute_tool, workers, arm, task, name, arguments, calls,
                timeout - (time.monotonic() - started))
            output = {k: v for k, v in receipt.items()
                      if not k.startswith('worker_') and k != 'operation_seconds'}
            record = {'name': name, 'arguments': arguments, 'output': output,
                      'is_error': 'error' in output, 'accepted': False,
                      'timing': {k: v for k, v in receipt.items()
                                 if k.startswith('worker_') or k == 'operation_seconds'}}
            calls.append(record)
            record['accepted'] = execution.resolve_custom_tool_call(
                event.get('callId'), canonical(output), is_error=record['is_error'])
            artifacts.write(f'call-{len(calls):02d}.json', record)
            if not record['accepted']:
                raise RuntimeError('Tool callback was not accepted')
        result = await execution.result_async(timeout_s=1)
        report.update(status=result.status, content=result.content, finish_reason=result.finish_reason)
    except Exception as exc:
        report['failure'] = type(exc).__name__
        if isinstance(exc, ProcessGroupRetirementError):
            report['cleanup_failure'] = 'worker_settlement_unproven'
    finally:
        if execution is not None:
            if not execution.abort():
                report['cleanup_failure'] = execution.cleanup_failure or 'unsettled'
            report['status'] = execution.status
            report['task_id'] = execution.task_id
            report['task_error'] = {k: v for k, v in (execution.state.get('error') or {}).items()
                                    if k in {'kind', 'failureStage', 'providerReached', 'authIssue'}}
            report['rounds'] = public_rounds(execution.state)
            report['usage'] = {k: v for k, v in (execution.state.get('usage') or {}).items()
                               if not k.startswith('_')}
            report['schema_fingerprints'] = execution.fingerprints
        report['seconds'] = time.monotonic() - started
        rounds = report.get('rounds', [])
        report['provenance_valid'] = bool(
            report['status'] == 'done' and not report['failure'] and not report['cleanup_failure']
            and rounds and all(row['model'] == runtime.model['model_id']
                               and row['provider_id'] == runtime.provider_id for row in rounds)
            and report.get('schema_fingerprints')
            and all(call['accepted'] for call in calls))
        report['score'] = score(task, report.get('content', ''), calls)
        report['passed'] = report['provenance_valid'] and report['score']['passed']
        artifacts.write('events.json', events)
        artifacts.write('record.json', report)
    return report


def summarize(records, expected):
    pairs = []
    identities = {(row['task'], row['trial']) for row in records}
    for task_id, trial in sorted(identities):
        rows = [r for r in records if (r['task'], r['trial']) == (task_id, trial)]
        comparable = (len(rows) == 2 and {r['arm'] for r in rows} == set(ARMS)
                      and all(r['provenance_valid'] for r in rows)
                      and rows[0]['schema_fingerprints'] == rows[1]['schema_fingerprints'])
        pairs.append({'task': task_id, 'trial': trial, 'comparable': comparable,
                      'passes': {r['arm']: r['passed'] for r in rows}})
    return {'diagnostic_only': True, 'leadership_claim': False,
            'complete': len(records) == expected and len({
                (r['task'], r['trial'], r['arm']) for r in records}) == expected,
            'pairs': pairs,
            'arms': {arm: {'attempts': len(rows), 'passes': sum(r['passed'] for r in rows),
                           'provenance_valid': sum(r['provenance_valid'] for r in rows),
                           'primary_page_fetched': sum(
                               r.get('score', {}).get('retrieval', {}).get('primary_page_fetched', False)
                               for r in rows),
                           'raw_json_compliant': sum(r.get('score', {}).get('format_valid', False) for r in rows),
                           'seconds': [r['seconds'] for r in rows],
                           'application_cost_usd': [r.get('usage', {}).get('cost_usd') for r in rows]}
                     for arm in ARMS for rows in [[r for r in records if r['arm'] == arm]]}}


async def live(args, root, registration, workers):
    registered_tasks = json.loads(canonical(registration['tasks']))
    if (digest(registered_tasks) != registration['task_pack_sha256'] or
            {task['id']: task_prompt(task) for task in registered_tasks} != registration['task_prompts']):
        raise ValueError('Registered task pack or prompts changed before execution')
    output = Artifacts(args.output)
    api = JsonAPI(args.origin, token=os.environ.get(args.api_token_env) if args.api_token_env else None)
    runtime = HTTPRuntime(api, registration['model'], args.provider)
    identities = {arm: workers.call(arm, 'identity', {}) for arm in ARMS}
    if (any('error' in receipt for receipt in identities.values())
            or identities['ddgs'].get('identity', {}).get('ddgs') != DDGS_VERSION
            or identities['tofu'].get('identity', {}).get('tofu_source_sha256')
            != registration['tofu_source_sha256']):
        output.write('preflight_failure.json', identities)
        raise ValueError('Worker identity preflight failed')
    registration = {**registration, 'worker_identities': identities,
                    'proxy_sha256': hashlib.sha256(workers.proxy.encode()).hexdigest(),
                    'capabilities_sha256': digest(api.call('/api/v4/capabilities'))}
    output.write('manifest.json', registration)
    records = []
    try:
        for trial in range(args.trials):
            for index, task in enumerate(registered_tasks):
                for arm in (ARMS if (trial + index) % 2 == 0 else ARMS[::-1]):
                    record = await run_one(runtime, workers, task, arm, trial,
                                           args.output / f'{trial}-{task["id"]}-{arm}', args.timeout)
                    records.append(record)
                    print(canonical({k: record[k] for k in
                                     ('task', 'arm', 'passed', 'provenance_valid', 'seconds', 'failure')}), flush=True)
                    if runtime.in_flight or record['cleanup_failure']:
                        raise RuntimeError('Owned work not settled; stopping cohort')
    finally:
        try:
            runtime.close()
        finally:
            summary = summarize(records, len(registered_tasks) * 2 * args.trials)
            summary['source_unchanged'] = source_fingerprint(root) == registration['source_sha256']
            if not summary['source_unchanged']:
                for pair in summary['pairs']:
                    pair['comparable'] = False
            output.write('summary.json', summary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--origin', default='http://127.0.0.1:15000')
    parser.add_argument('--api-token-env')
    parser.add_argument('--creator')
    parser.add_argument('--model')
    parser.add_argument('--provider')
    parser.add_argument('--tofu-python')
    parser.add_argument('--ddgs-python')
    parser.add_argument('--proxy-env', default='TOFU_SEARCH_EVAL_PROXY')
    parser.add_argument('--trials', type=int, choices=range(1, 4), default=1)
    parser.add_argument('--timeout', type=int, default=TASK_SECONDS)
    args = parser.parse_args(argv)
    if not 30 <= args.timeout <= 600:
        parser.error('--timeout must be 30..600')
    root = Path(__file__).resolve().parents[2]
    registration = manifest(root, {'creator_id': args.creator, 'model_id': args.model},
                            args.provider, args.trials, args.timeout)
    if not args.live:
        print(canonical(registration))
        return 0
    if not all((args.output, args.creator, args.model, args.provider, args.tofu_python, args.ddgs_python)):
        parser.error('--live requires output, creator, model, provider and both worker interpreters')
    if args.output.exists() or not args.output.is_absolute():
        parser.error('--output must be an absolute new directory')
    if args.proxy_env not in os.environ:
        parser.error('Set the explicit proxy environment variable (empty means direct)')
    workers = Workers(root, args.tofu_python, args.ddgs_python, os.environ[args.proxy_env])
    if args.api_token_env:
        workers.env.pop(args.api_token_env, None)
    asyncio.run(live(args, root, registration, workers))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
