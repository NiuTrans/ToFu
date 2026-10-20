"""Paired diagnostic against one configured application's public v4 API.

Entry: python -m evaluations.programmatic_pilot.native_api --live. Reuses the
frozen pack, scorer and client-handoff loop. HTTP owns task lifecycle only;
model routing, authority, program execution and settlement stay on the server.
A forced capability probe gates execution and never counts as adoption.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
from pathlib import Path
import re
import time
from types import SimpleNamespace
from urllib.error import HTTPError
import uuid

from evaluations.application_api import JsonAPI
from lib.agent_core.run_contract import project_agent_event_evidence
from .runner import (
    ARMS, ARTIFACT_LIMIT_BYTES, paired_report, preflight,
    private_json, run_task, source_fingerprint, usage_cost,
)
from .workloads import Workload as Workload, canonical as canonical, workload_pack as workload_pack, workloads as workloads

TERMINAL = {'done', 'error', 'aborted', 'interrupted'}


class HTTPExecution:
    def __init__(self, runtime, admission, timeout_s, expected_names,
                 *, expected_catalog_count):
        task_id = admission.get('task_id', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', task_id):
            raise ValueError('Invalid admitted task identity')
        self.runtime, self.task_id = runtime, task_id
        self.admission = admission
        self.deadline = time.monotonic() + timeout_s
        self.expected_names = set(expected_names)
        self.expected_catalog_count = expected_catalog_count
        self.events = []
        self.state = {'status': 'running'}
        self.cursor = 0
        self.fingerprint = None
        self.cleanup_failure = None
        self.fingerprints = []

    @property
    def status(self):
        return self.state.get('status', 'running')

    def _path(self, suffix=''):
        return '/api/v4/tasks/' + self.task_id + suffix

    def _task_state(self):
        state = self.runtime.api.call(self._path())
        if state.get('id') != self.task_id:
            raise ValueError('Foreign task state')
        return state

    async def events_async(self, *, timeout_s):
        deadline = min(self.deadline, time.monotonic() + timeout_s)
        while time.monotonic() < deadline:
            page = await asyncio.to_thread(self.runtime.api.call,
                                          self._path('/events?cursor=' + str(self.cursor)))
            if page.get('cursor', {}).get('reset'):
                raise ValueError('Replay evidence was evicted')
            next_cursor = page.get('next_cursor')
            if type(next_cursor) is not int or next_cursor < self.cursor:
                raise ValueError('Invalid replay cursor')
            rows = page.get('events', [])
            if not isinstance(rows, list):
                raise ValueError('Invalid event page')
            for event in rows:
                if event.get('taskId') not in {None, self.task_id}:
                    raise ValueError('Foreign task evidence')
                if event.get('type') == 'tool_wire_projection':
                    allowed_surfaces = [self.expected_names]
                    if 'execute_program' in self.expected_names and len(self.expected_names) > 1:
                        allowed_surfaces.append((self.expected_names - {'execute_program'}) | {'execute_tools'})
                    if (set(event.get('toolNames', [])) not in allowed_surfaces
                            or event.get('executableToolCount') != self.expected_catalog_count):
                        raise ValueError('Unexpected executable tool surface')
                    fingerprint = event.get('schemaFingerprint')
                    if not fingerprint:
                        raise ValueError('Missing schema fingerprint')
                    self.fingerprint = fingerprint
                    if not self.fingerprints or self.fingerprints[-1] != fingerprint:
                        self.fingerprints.append(fingerprint)
                projected = project_agent_event_evidence(event)
                self.events.append(projected)
                yield projected
            if rows and next_cursor == self.cursor:
                raise ValueError('Replay made no cursor progress')
            if not rows and not page.get('caught_up') and next_cursor == self.cursor:
                raise ValueError('Replay made no progress')
            self.cursor = next_cursor
            if page.get('done') and page.get('caught_up'):
                self.state = await asyncio.to_thread(self._task_state)
                if self.status not in TERMINAL:
                    raise ValueError('Terminal replay disagrees with task state')
                if not self.fingerprints:
                    raise ValueError('Missing native tool-surface evidence')
                return
            if page.get('caught_up'):
                await asyncio.sleep(0.5)
        raise TimeoutError('Native API task deadline exceeded')

    def resolve_custom_tool_call(self, call_id, content, *, is_error=False):
        result = self.runtime.api.call(self._path('/tool_result'),
            {'call_id': call_id, 'content': content, 'is_error': is_error})
        return (result.get('resolved') is True and result.get('callId') == call_id
                and result.get('taskId') == self.task_id)

    async def result_async(self, *, timeout_s):
        if self.status not in TERMINAL:
            raise TimeoutError('Task has no verified terminal state')
        return SimpleNamespace(status=self.status,
            finish_reason=self.state.get('finishReason'), content=self.state.get('content', ''))

    def evidence_snapshot(self):
        programs = {}
        for event in self.events:
            if event.get('type') not in {'program_start', 'program_output'}:
                continue
            key = event.get('programCallId')
            if not key:
                raise ValueError('Program evidence has no identity')
            run = programs.setdefault(key, {'callId': key})
            for field in ('source', 'backend', 'code', 'limits', 'status', 'result', 'stats'):
                if field in event:
                    run[field] = event[field]
        return {'status': self.status, 'usage': self.state.get('usage') or {},
                'apiRounds': self.state.get('apiRounds') or [],
                'programRuns': list(programs.values()), 'admission': self.admission,
                'schemaFingerprint': self.fingerprint, 'schemaFingerprints': self.fingerprints,
                'evidenceSource': 'native_v4_http',
                'cleanupFailure': self.cleanup_failure}

    def abort(self):
        if self.status in TERMINAL:
            return True
        try:
            self.runtime.api.call(self._path('/abort'), {})
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                self.state = self._task_state()
                if self.status in TERMINAL:
                    return True
                time.sleep(0.5)
            self.cleanup_failure = 'terminal_timeout'
        except Exception as exc:
            self.cleanup_failure = type(exc).__name__
        return False


class HTTPRuntime:
    def __init__(self, api, model, provider_id=None):
        self.api, self.model, self.provider_id = api, model, provider_id
        self.executions = []
        self.ambiguous_admission = False

    @property
    def in_flight(self):
        return int(self.ambiguous_admission) + sum(e.status not in TERMINAL for e in self.executions)

    def start(self, messages, *, config, custom_tools, custom_tools_mode,
              trajectory, request_id, timeout_s):
        if self.in_flight:
            raise RuntimeError('Previous owned task has not settled')
        if custom_tools_mode != 'exclusive':
            raise ValueError('Pilot requires exclusive custom tools')
        cfg = copy.deepcopy(config)
        # This is the public SDK's established explicit-tool precedence branch.
        # The API still mints and validates the request-local client environment.
        cfg['_explicitToolSchemas'] = [
            {'type': tool['type'], 'function': tool['function'],
             **({'decoded_result_schema': copy.deepcopy(tool['decoded_result_schema'])}
                if tool.get('decoded_result_schema') is not None else {})}
            for tool in custom_tools]
        cfg['_customToolsMode'] = 'exclusive'
        body = {'model': self.model, 'messages': messages, 'config': cfg,
                'tools': custom_tools, 'async': True, 'trajectory': trajectory,
                'id': request_id, 'timeout_s': timeout_s,
                'conversation_id': 'ptc-pilot-' + uuid.uuid4().hex}
        if self.provider_id:
            body['routing'] = {'preferred_provider_id': self.provider_id}
        names = [t['function']['name'] for t in custom_tools]
        if cfg['tools']['programmaticCalling'] != 'off':
            if cfg['tools'].get('programmaticExposure') == 'code_api':
                # Keep the entire admitted executable catalog behind its
                # gateway while exposing only the code action.
                # Additive fallback is a failed candidate projection here.
                names = ['execute_program']
            else:
                names.append('execute_program')
        try:
            admission = self.api.call('/api/v4/agent/run', body)
            execution = HTTPExecution(self, admission, timeout_s, names,
                                      expected_catalog_count=len(custom_tools))
        except Exception as exc:
            # These native API responses explicitly reject admission. They
            # cannot own a task. Timeouts, lost replies and server failures
            # remain ambiguous and must take the existing reconciliation path.
            if isinstance(exc, HTTPError) and exc.code in {
                    400, 401, 403, 404, 405, 413, 415, 422, 429}:
                raise
            # Never resubmit after an ambiguous POST. Stop the pilot unless the
            # owned correlation lookup proves the accepted task has settled.
            self.ambiguous_admission = True
            self._recover_admission(body['conversation_id'], timeout_s)
            raise
        self.executions.append(execution)
        if self.provider_id and admission.get('provider_id') != self.provider_id:
            execution.abort()
            raise ValueError('Provider changed after preregistration')
        return execution

    def _recover_admission(self, conversation_id, timeout_s):
        try:
            page = self.api.call('/api/v4/tasks/by-conv/' + conversation_id + '?limit=2')
            tasks = page.get('tasks', [])
            if len(tasks) != 1:
                return
            row = tasks[0]
            execution = HTTPExecution(self, {'task_id': row['id']}, timeout_s, [],
                                      expected_catalog_count=0)
            self.executions.append(execution)
            execution.abort()
            if execution.status in TERMINAL:
                self.ambiguous_admission = False
        except Exception:
            return

    def close(self):
        for execution in self.executions:
            execution.abort()
        if self.in_flight:
            raise RuntimeError('An owned HTTP task has not settled')


async def capability_probe(runtime, directory, thinking, timeout_s):
    source = ('const row=await tools.invoke("custom__read_dataset",{dataset:"probe"}); '
              'return {id:String(row.id),runtimeType:typeof row.id};')
    workload = Workload('runtime-probe', 'runtime_probe',
        'Instrumentation only, not an adoption benchmark. Call execute_program '
        'exactly once with this code: ' + source + ' Return its JSON result.',
        canonical({'probe': {'id': 9223372036854775807}}),
        canonical({'id': '9223372036854775807', 'runtimeType': 'bigint'}))
    record = await run_task(runtime, workload, 'programmatic', 0, directory,
                            thinking, timeout_s, None)
    evidence = runtime.executions[-1].evidence_snapshot()
    valid = (record['status'] == 'done' and record['runnerFailure'] is None
        and record['requiredReadsComplete'] and record['programCount'] == 1
        and len(evidence['programRuns']) == 1
        and all(p.get('source') == 'execute_program'
                and p.get('backend') == 'local_javascript'
                and p.get('status') == 'completed'
                and isinstance(p.get('stats'), dict)
                and p['stats'].get('engine') == 'quickjs-wasm'
                and p['stats'].get('outcome') == 'completed'
                and canonical(p.get('result')) == workload.expected_json
                for p in evidence['programRuns']))
    private_json(directory / 'gate.json', {'passed': valid,
        'countsAsAdoption': False, 'requiredBackend': 'local_javascript',
        'requiredEngine': 'quickjs-wasm', 'requiresExactInteger': True,
        'modelAnswerOraclePassed': record['oraclePassed'],
        'exactResultAuthority': 'native_program_output'})
    if not valid:
        raise RuntimeError('Native runtime capability gate failed; do not run paired trials')
    if runtime.in_flight:
        raise RuntimeError('Capability task is still active')
    return evidence['admission']['provider_id']


async def live_run(api, model, manifest, output, root, thinking, trials,
                   timeout_s, price_card):
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    identity = api.call('/api/v4/users/me')
    aggregate = api.call('/api/v4/model-routing')['model_routing']
    registered = next((m for m in aggregate['models']
        if m.get('creator_id') == model['creator_id'] and m.get('model_id') == model['model_id']), None)
    if not registered or registered.get('registration_state') != 'ready':
        raise ValueError('Choose an existing ready model; this runner registers no models')
    manifest = {**manifest, 'transport': 'native_v4_http', 'origin': api.origin,
        'model': model, 'registeredModel': registered, 'ownerId': identity['ownerId'],
        'serverSourceBytesVerified': False, 'sourceSha256Basis': 'local_client_checkout',
        'capabilityProbeCountsAsAdoption': False, 'runtimeWarmup': 'capability_probe_before_pairs',
        'firstUseProbeCostExcludedFromPairDeltas': True, 'priceCard': price_card}
    private_json(output / 'manifest.json', manifest)
    runtime = HTTPRuntime(api, model)
    records = []
    try:
        directory = output / 'runtime-probe'; directory.mkdir(mode=0o700)
        runtime.provider_id = await capability_probe(runtime, directory, thinking, timeout_s)
        private_json(output / 'provider.json', {'providerId': runtime.provider_id})
        for trial in range(trials):
            for workload in workload_pack(manifest.get('taskPack', 'baseline')):
                for arm in ARMS if trial % 2 == 0 else reversed(ARMS):
                    if source_fingerprint(root) != manifest['sourceSha256']:
                        raise RuntimeError('Client source changed after preregistration')
                    directory = output / f'{workload.task_id}-{arm}-{trial}'
                    directory.mkdir(mode=0o700)
                    record = await run_task(runtime, workload, arm, trial, directory,
                                            thinking, timeout_s, price_card)
                    records.append(record)
                    print(canonical({k: record[k] for k in (
                        'taskId','arm','oraclePassed','programCount','wallMs','runnerFailure')}), flush=True)
                    if runtime.in_flight:
                        raise RuntimeError('Previous execution is still active')
        if source_fingerprint(root) != manifest['sourceSha256']:
            raise RuntimeError('Client source changed during paired execution')
        report = paired_report(records, trials=trials, pack=manifest.get('taskPack', 'baseline'))
        report['transport'] = 'native_v4_http'
        report['serverSourceBytesVerified'] = False
        private_json(output / 'report.json', report)
        return report
    except Exception as exc:
        private_json(output / 'run-failure.json', {
            'type': type(exc).__name__, 'ambiguousAdmission': runtime.ambiguous_admission,
            'ownedAdmissions': [e.admission for e in runtime.executions]})
        raise
    finally:
        try:
            runtime.close()
        finally:
            private_json(output / 'cleanup.json', {
                'inFlight': runtime.in_flight, 'ambiguousAdmission': runtime.ambiguous_admission,
                'tasks': [{'taskId':e.task_id, 'status':e.status,
                           'cleanupFailure':e.cleanup_failure} for e in runtime.executions]})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--pack', choices=('baseline', 'adaptive', 'declared'), default='baseline')
    parser.add_argument('--origin', default='http://127.0.0.1:15000')
    parser.add_argument('--creator', default='deepseek')
    parser.add_argument('--model', default='deepseek-v4-flash')
    parser.add_argument('--api-token-env')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--price-card', type=Path)
    parser.add_argument('--thinking', choices=('low','medium','high','xhigh'), default='medium')
    parser.add_argument('--trials', type=int, choices=range(1,6), default=1)
    parser.add_argument('--timeout-seconds', type=int, default=180)
    args = parser.parse_args(argv)
    if not 10 <= args.timeout_seconds <= 600:
        parser.error('--timeout-seconds must be 10..600')
    root = Path(__file__).resolve().parents[2]
    manifest = preflight(root, thinking=args.thinking, trials=args.trials, timeout_s=args.timeout_seconds, pack=args.pack)
    if not args.live:
        print(canonical({**manifest, 'transport':'native_v4_http'}));return 0
    if args.output is None:
        parser.error('--live requires --output')
    token = os.environ.get(args.api_token_env, '') if args.api_token_env else None
    if args.api_token_env and not token:
        parser.error('Named API-token environment variable is empty')
    api = JsonAPI(args.origin, token=token, maximum_bytes=ARTIFACT_LIMIT_BYTES)
    price_card = json.loads(args.price_card.read_text()) if args.price_card else None
    usage_cost({}, price_card)
    asyncio.run(live_run(api, {'creator_id':args.creator,'model_id':args.model},
        manifest, args.output.resolve(), root, args.thinking, args.trials,
        args.timeout_seconds, price_card))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
