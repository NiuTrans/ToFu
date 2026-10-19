"""Execute a benchmark instruction through the durable production application.

Entry: ``python -m evaluations.application_agent --live --request FILE``.
The application must run in the task's isolated environment, with an admitted
model route and native TofuDB. This driver transports one task; the application
owns every model/tool decision. Harbor separately owns the external verifier.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError
import uuid

from evaluations.application_api import JsonAPI
from evaluations.swebench.tofu_kimi_runtime import _canonical_config
from lib.agent_core.run_contract import (
    project_agent_event_evidence, project_agent_evidence, project_agent_result,
)
from tofu_agent.models import AgentResult
from lib.storage.storage_projection import sanitize_usage_for_persist

TERMINAL = frozenset({'done', 'error', 'aborted', 'interrupted'})
_IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')
_RESULT_RESERVE_BYTES = 1024


def validate_request(value):
    required = {'model', 'provider_id', 'instruction', 'project_root', 'output_root',
                'config', 'thinking', 'timeout_seconds', 'artifact_max_bytes'}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError('Application trial requires the exact documented request fields')
    model = value['model']
    if (not isinstance(model, dict) or set(model) != {'creator_id', 'model_id'}
            or any(not isinstance(v, str) or not _IDENTIFIER.fullmatch(v) for v in model.values())
            or not isinstance(value['provider_id'], str)
            or not _IDENTIFIER.fullmatch(value['provider_id'])):
        raise ValueError('Explicit registered model and provider identities are required')
    if not isinstance(value['instruction'], str) or not value['instruction'].strip():
        raise ValueError('Nonempty benchmark instruction required')
    for name, lower, upper in (('timeout_seconds', 1, 604800),
                               ('artifact_max_bytes', 2048, 4 * 1024 ** 3)):
        if type(value[name]) is not int or not lower <= value[name] <= upper:
            raise ValueError('Invalid explicit ' + name)
    if value['thinking'] not in {'low', 'medium', 'high', 'max'}:
        raise ValueError('Explicit supported reasoning effort required')
    config, _ = _canonical_config(value['config'])
    if type(config.get('maxTokens')) is not int or config['maxTokens'] < 1:
        raise ValueError('An explicit output token limit is required')
    if 'projectPath' in config or 'project_remote' in config or 'remote' in config:
        raise ValueError('The trial owns its project binding')
    project = Path(value['project_root'])
    output = Path(value['output_root'])
    if not project.is_absolute() or not project.is_dir() or not output.is_absolute():
        raise ValueError('Absolute task workspace and output paths required')
    if output.exists():
        raise ValueError('Trial output must be a new directory')
    return {**value, 'model': dict(model), 'config': config,
            'project_root': str(project.resolve()), 'output_root': str(output)}


class _Artifacts:
    def __init__(self, root, maximum):
        self.root, self.maximum, self.used = Path(root), maximum, 0
        self.root.mkdir(mode=0o700, parents=False, exist_ok=False)
        self.events = None

    def write(self, name, value):
        raw = (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True) + '\n').encode()
        if self.used + len(raw) > self.maximum:
            raise ValueError('Trial evidence byte budget exhausted')
        with (self.root / name).open('xb') as stream:
            os.chmod(stream.name, 0o600)
            stream.write(raw)
        self.used += len(raw)

    def event(self, value):
        raw = (json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n').encode()
        if self.used + len(raw) > self.maximum:
            raise ValueError('Trial evidence byte budget exhausted')
        if self.events is None:
            self.events = (self.root / 'events.jsonl').open('xb')
            os.chmod(self.events.name, 0o600)
        self.events.write(raw)
        self.events.flush()
        self.used += len(raw)

    def close(self):
        if self.events is not None:
            self.events.close()


def _storage_identity(api):
    health = api.call('/api/health', unwrap=False)
    storage = health.get('storage', {})
    if storage.get('backend') != 'tofudb' or storage.get('ready') is not True:
        raise ValueError('A ready native TofuDB application is required')
    if (type(storage.get('pid')) is not int or storage['pid'] < 1
            or type(storage.get('port')) is not int or not 1 <= storage['port'] <= 65535):
        raise ValueError('Missing native storage authority identity')
    return {key: storage.get(key) for key in ('backend', 'pid', 'port')}


def _remaining_timeout(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Application trial deadline exceeded')
    return min(10, remaining)


def _owned_state(api, task_id, *, durable=False, timeout=10):
    state = api.call('/api/v4/tasks/' + task_id + ('?view=durable' if durable else ''), timeout=timeout)
    if state.get('id') != task_id:
        raise ValueError('Foreign task response')
    if durable and state.get('readSource') != 'durable':
        raise ValueError('Application did not honor the durable read contract')
    return state


def _terminal_identity(state):
    identity = {key: state.get(key) for key in ('id', 'status', 'content', 'thinking',
            'finishReason', 'streamState', 'usage', 'model', 'error')}
    identity['usage'] = sanitize_usage_for_persist(identity['usage'])
    return identity


def _cancel_and_join(api, task_id, *, seconds=30):
    # Cancellation is sent once. A lost reply must not prevent reconciliation.
    try:
        api.call('/api/v4/tasks/' + task_id + '/abort', {})
    except Exception:
        pass
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if _owned_state(api, task_id).get('status') in TERMINAL:
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def run_trial(request, api, *, settlement_seconds=30):
    """Run once, preserve failures, and prove terminal readback from storage."""
    request = validate_request(request)
    artifacts = _Artifacts(request['output_root'], request['artifact_max_bytes'] - _RESULT_RESERVE_BYTES)
    correlation = 'benchmark-' + uuid.uuid4().hex
    task_id, terminal, admission_attempted = None, False, False
    report = {'contractVersion': 'tofu.application-trial/v1', 'formal_score': None,
              'trialState': 'failed', 'conversationId': correlation,
              'taskId': None, 'durableReadbackVerified': False, 'cleanupVerified': True}
    try:
        artifacts.write('request.json', request)
        storage = _storage_identity(api)
        artifacts.write('storage-start.json', storage)
        config = {**request['config'], 'projectPath': request['project_root'],
                  'thinking': request['thinking'], 'disableModelFallback': True}
        body = {'model': request['model'], 'routing': {'preferred_provider_id': request['provider_id']},
                'messages': [{'role': 'user', 'content': request['instruction']}],
                'config': config, 'async': True, 'conversation_id': correlation,
                'id': correlation, 'timeout_s': request['timeout_seconds']}
        deadline = time.monotonic() + request['timeout_seconds']
        admission_attempted = True
        admission = api.call('/api/v4/agent/run', body, timeout=_remaining_timeout(deadline))
        candidate_id = admission.get('task_id')
        if not isinstance(candidate_id, str) or not _IDENTIFIER.fullmatch(candidate_id):
            raise ValueError('Invalid admitted task identity')
        task_id = candidate_id
        report['taskId'] = task_id
        artifacts.write('admission.json', {key: admission.get(key)
            for key in ('task_id', 'provider_id', 'model', 'status')})
        if admission.get('provider_id') != request['provider_id']:
            raise ValueError('Provider changed during admission')
        cursor = 0
        while time.monotonic() < deadline:
            page = api.call('/api/v4/tasks/' + task_id + '/events?cursor=' + str(cursor),
                            timeout=_remaining_timeout(deadline))
            next_cursor, events = page.get('next_cursor'), page.get('events')
            if (type(next_cursor) is not int or next_cursor < cursor
                    or not isinstance(events, list) or page.get('cursor', {}).get('reset')):
                raise ValueError('Invalid or evicted task replay')
            for event in events:
                if not isinstance(event, dict) or event.get('taskId') not in {None, task_id}:
                    raise ValueError('Foreign task event')
                artifacts.event(project_agent_event_evidence(event))
            if next_cursor == cursor and (events or not page.get('caught_up')):
                raise ValueError('Task replay made no progress')
            cursor = next_cursor
            if page.get('done') and page.get('caught_up'):
                state = _owned_state(api, task_id, timeout=_remaining_timeout(deadline))
                terminal = state.get('status') in TERMINAL
                if not terminal:
                    raise ValueError('Task state disagrees with terminal replay')
                break
            if page.get('caught_up'):
                time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        else:
            raise TimeoutError('Application trial deadline exceeded')
        artifacts.write('runtime-evidence.json', project_agent_evidence(state,
            model=request['model']['model_id'], provider_id=request['provider_id'], requested_id=correlation))
        settle_deadline = time.monotonic() + settlement_seconds
        while True:
            try:
                durable = _owned_state(api, task_id, durable=True)
            except HTTPError as exc:
                if exc.code != 404:
                    raise
                durable = {}
            if durable.get('status') in TERMINAL and _terminal_identity(durable) == _terminal_identity(state):
                break
            if time.monotonic() >= settle_deadline:
                raise TimeoutError('Durable terminal readback did not converge')
            time.sleep(0.25)
        artifacts.write('durable-evidence.json', project_agent_evidence(durable,
            model=request['model']['model_id'], provider_id=request['provider_id'], requested_id=correlation))
        if _storage_identity(api) != storage:
            raise ValueError('Storage authority changed during the trial')
        report['durableReadbackVerified'] = True
        report['terminalStatus'] = state['status']
        verdict = AgentResult.from_payload(project_agent_result(
            state, model=request['model']['model_id']))
        report['trialState'] = 'completed' if verdict.ok else 'agent_failed'
    except Exception as exc:
        # Exception messages can contain provider URLs or response bodies.
        report['failureType'] = type(exc).__name__
    finally:
        if admission_attempted and task_id is None:
            report['cleanupVerified'] = False
            try:
                rows = api.call('/api/v4/tasks/by-conv/' + correlation + '?limit=2').get('tasks', [])
                if len(rows) == 1 and isinstance(rows[0].get('id'), str) and _IDENTIFIER.fullmatch(rows[0]['id']):
                    task_id = rows[0]['id']
                    report['taskId'] = task_id
            except Exception:
                pass
        if task_id is not None and not terminal:
            report['cleanupVerified'] = _cancel_and_join(api, task_id, seconds=settlement_seconds)
        artifacts.close()
        # The receipt reserve is part of the declared artifact budget, so an
        # exhausted trajectory can still retain its failure without overflow.
        receipt = Path(request['output_root']) / 'result.json'
        raw = (json.dumps(report, ensure_ascii=False, sort_keys=True) + '\n').encode()
        if len(raw) > _RESULT_RESERVE_BYTES:
            raise ValueError('Trial receipt exceeds its reserved byte budget')
        with receipt.open('xb') as stream:
            os.chmod(stream.name, 0o600)
            stream.write(raw)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--api-token-env')
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args(argv)
    with args.request.open('rb') as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        parser.error('Request exceeds 1 MiB')
    value = validate_request(json.loads(raw))
    if not args.live:
        print(json.dumps({'status': 'validated', 'formal_score': None}))
        return 0
    token = os.environ.get(args.api_token_env) if args.api_token_env else None
    if args.api_token_env and not token:
        parser.error('The selected API credential environment variable is empty')
    report = run_trial(value, JsonAPI(args.origin, token=token))
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['trialState'] == 'completed' and report['cleanupVerified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
