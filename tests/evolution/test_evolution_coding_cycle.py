"""Coding goal -> real Workers -> command verification -> review -> next input.

Only provider transport is scripted. HTTP admission, worker scheduling, tools,
files, source archives, accounting, settlement and context composition are real.
This is a wiring gate, not a measurement of model coding quality.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import shlex
import subprocess
import sys
import threading
import time
import uuid

import pytest

pytest_plugins = ('tests._credential_sidecar',)
pytestmark = pytest.mark.api


@pytest.mark.parametrize('credential_storage', ['sqlite', 'tofudb'], indirect=True, scope='module')
def test_coding_workers_feed_verified_learning_into_next_request(
        credential_storage, monkeypatch, tmp_path):
    from quart import Quart
    from lib.api_keys import create_key
    from lib.evolution import runs
    from lib.evolution.episodes import get_episode
    from lib.evolution.strategies import retrieve_strategies
    from lib.llm.stream_result import ProviderStreamResult
    from lib.llm_dispatch import reset_dispatcher
    from routes.api_v1.auth import attach_rate_headers, bearer_auth_before_request
    from routes.api_v1.evolution import api_v1_evolution_bp
    from tests.support.model_routing import install_native_test_model_route

    owner = 1
    install_native_test_model_route(owner_user_id=owner)
    reset_dispatcher()
    project = tmp_path / 'coding-project'
    project.mkdir()
    (project / 'clamp.py').write_text('def clamp(value, low, high):\n    return value\n')
    implementation = ('def clamp(value, low, high):\n'
                      '    if low > high:\n        raise ValueError("invalid bounds")\n'
                      '    return max(low, min(value, high))\n')
    tests = ('import unittest\nfrom clamp import clamp\n'
             'class ClampTests(unittest.TestCase):\n'
             '    def test_boundaries(self):\n'
             '        for value, expected in [(-2, 0), (0, 0), (4, 4), (10, 10), (12, 10)]:\n'
             '            self.assertEqual(clamp(value, 0, 10), expected)\n'
             '    def test_invalid_bounds(self):\n'
             '        with self.assertRaises(ValueError): clamp(0, 10, 0)\n')
    check = [sys.executable, '-m', 'unittest', 'discover', '-v']
    # Establish a failing baseline with the same checks before any model call.
    (project / 'test_clamp.py').write_text(tests)
    baseline = subprocess.run(check, cwd=project, capture_output=True, text=True, timeout=10)
    assert baseline.returncode != 0 and 'FAILED' in baseline.stderr
    (project / 'test_clamp.py').unlink()

    counts = {'root': 0, 'implementation': 0, 'tests': 0, 'review': 0}
    inputs = []
    lock = threading.Lock()
    workers_entered = threading.Barrier(2, timeout=15)
    next_goal = False
    source_offsets = {}

    def tool(name, arguments, call_id):
        return {'role': 'assistant', 'content': '', 'tool_calls': [{
            'id': call_id, 'type': 'function', 'function': {
                'name': name, 'arguments': json.dumps(arguments)}}]}

    def response(body, *, on_content=None, **kwargs):
        messages = body['messages']
        # Worker input begins with its delegated objective, unlike root history
        # which can also contain those words inside a prior spawn tool call.
        content = next(row.get('content', '') for row in messages if row['role'] == 'user')
        first_user = content if isinstance(content, str) else next(
            block['text'] for block in content if isinstance(block, dict) and block.get('type') == 'text')
        actor = ('review' if 'Independently assess the delivered work' in first_user else
                 'implementation' if first_user.startswith('IMPLEMENT_WORKER:') else
                 'tests' if first_user.startswith('TEST_WORKER:') else 'root')
        with lock:
            counts[actor] += 1
            step = counts[actor]
            inputs.append((actor, json.dumps(messages, ensure_ascii=False)))
        if actor in ('implementation', 'tests'):
            if step == 1:
                # Demonstrate demand starts both independent branches.
                workers_entered.wait()
                filename, content = ('clamp.py', implementation) if actor == 'implementation' else ('test_clamp.py', tests)
                code = f'from pathlib import Path; Path({filename!r}).write_text({content!r}); print("wrote {filename}")'
                message = tool('run_command', {'command': shlex.join([sys.executable, '-c', code]),
                                               'working_dir': str(project)}, actor + '-write')
            else:
                assert step == 2
                assert any(row['role'] == 'tool' and 'wrote ' in str(row.get('content')) for row in messages)
                message = {'role': 'assistant', 'content': actor + ' file saved; root must run the acceptance tests.'}
        elif actor == 'review':
            frozen = json.loads(first_user.rsplit('\n', 1)[-1])
            sources = [(kind, frozen[key]) for kind, key in (
                ('tool_evidence', 'observed_tool_results_source'),
                ('artifact_evidence', 'generated_artifact_source')) if key in frozen]
            assert any(kind == 'artifact_evidence' for kind, _ in sources)
            unread = next(((kind, source) for kind, source in sources
                           if source_offsets.get(kind, 0) < source['total_chars']), None)
            if unread:
                kind, source = unread
                offset = source_offsets.get(kind, 0)
                source_offsets[kind] = offset + 6000
                message = tool('evolution_source', {'action': 'read', 'run_id': source['run_id'],
                    'task_id': source['task_id'], 'source_sha256': source['source_sha256'],
                    'source_kind': kind, 'offset': offset}, 'review-read-' + str(step))
            else:
                received = json.dumps([row for row in messages if row['role'] == 'tool']) + json.dumps(frozen.get('observed_tool_results', []))
                assert 'Ran 2 tests' in received and 'clamp.py' in received
                assert 'return max(low, min(value, high))' in received
                assert 'with self.assertRaises(ValueError)' in received
                message = {'role': 'assistant', 'content': json.dumps({
                    'criterion_results': [{'criterion_id': key, 'outcome': 'pass',
                        'evidence_ref': 'Saved clamp implementation and tests; observed unittest ran 2 tests successfully.'}
                        for key in ('objective', 'evidence', 'delivery')],
                    'evidence_audit': {'complete': True, 'objections': []}})}
        elif next_goal:
            assert 'Strategies observed on similar goals' in json.dumps(messages)
            if step == 6:
                # Omit working_dir: execution must inherit the parent's project.
                message = tool('run_command', {'command': shlex.join(check)}, 'followup-verify')
            elif step == 7:
                assert any(row['role'] == 'tool' and 'Ran 2 tests' in str(row.get('content')) for row in messages)
                message = tool('evolution_artifact', {'paths': ['clamp.py', 'test_clamp.py']}, 'followup-freeze')
            else:
                assert step == 8
                message = tool('evolution_coordinate', {'action': 'submit_result',
                    'content': 'Continued in the same project, reran 2 passing tests and retained the verified implementation and test files.'}, 'followup-deliver')
        elif step == 1:
            message = tool('spawn_agents', {'agents': [
                {'id': 'implementation', 'role': 'general', 'tools': ['run_command'],
                 'objective': 'IMPLEMENT_WORKER: Repair clamp.py in the supplied project. Clamp to inclusive bounds and reject reversed bounds. Own only clamp.py; preserve concurrent work.',
                 'context': str(project)},
                {'id': 'tests', 'role': 'general', 'tools': ['run_command'],
                 'objective': 'TEST_WORKER: Write test_clamp.py with unittest for inclusive clamp bounds, out-of-range values and reversed bounds. Own only test_clamp.py; preserve concurrent work.',
                 'context': str(project)}]}, 'delegate')
        elif step == 2:
            message = tool('poll', {'ids': ['implementation', 'tests'], 'mode': 'all', 'wait_s': 30}, 'join-workers')
        elif step == 3:
            message = tool('run_command', {'command': shlex.join(check), 'working_dir': str(project)}, 'verify-project')
        elif step == 4:
            assert any(row['role'] == 'tool' and 'Ran 2 tests' in str(row.get('content'))
                       and 'OK' in str(row.get('content')) for row in messages)
            message = tool('evolution_artifact', {'paths': ['clamp.py', 'test_clamp.py']}, 'freeze-files')
        else:
            assert step == 5
            message = tool('evolution_coordinate', {'action': 'submit_result', 'content':
                'Implemented clamp.py and test_clamp.py through two Workers. The root ran unittest: 2 tests passed. Both files are frozen for independent review.'}, 'deliver')
        if message.get('content') and on_content:
            on_content(message['content'])
        return ProviderStreamResult.from_legacy(message, 'tool_calls' if message.get('tool_calls') else 'stop',
            {'prompt_tokens': 40, 'completion_tokens': 40, 'total_tokens': 80})

    provider_errors = []

    def transport(body, **kwargs):
        try:
            from types import SimpleNamespace
            from lib.llm.request import (ModelRequestBudgetScope, mark_model_request_dispatched,
                                         strip_internal_request_fields)
            with ModelRequestBudgetScope(body) as scope:
                lease = scope.body['_model_budget_admit'](input_tokens=40, max_output_tokens=40)
                mark_model_request_dispatched(SimpleNamespace(model_budget_lease=lease,
                    body=strip_internal_request_fields(scope.body)))
                result = response(scope.body, **kwargs)
                scope.usage = result.usage
                return result
        except Exception as exc:
            provider_errors.append(repr(exc))
            raise

    async def async_stream(body, **kwargs):
        return transport(body, **kwargs)

    monkeypatch.setattr('lib.llm.stream_chat', transport)
    monkeypatch.setattr(importlib.import_module('lib.llm.astream'), 'async_stream_chat', async_stream)
    app = Quart(__name__, static_folder=None)
    app.config['TESTING'] = True
    app.before_request(bearer_auth_before_request)
    app.after_request(attach_rate_headers)
    app.register_blueprint(api_v1_evolution_bp)
    _, token = create_key(owner_user_id=owner, name='coding-cycle', scopes=['tasks'])
    client = app.test_client()
    headers = {'Authorization': 'Bearer ' + token}
    submitted = []

    async def journey():
        nonlocal next_goal
        payload = {'objective': 'Repair clamp.py using independent implementation and test Workers; verify inclusive bounds and reversed bounds with unittest and deliver the saved files.',
                   'model': {'creator_id': 'tofu-test', 'model_id': 'stub-model'},
                   'project_path': str(project), 'max_attempts': 1, 'require_command_execution': True,
                   'executor_schema_budget_tokens': 4000,
                   'budget': {'max_tokens': 20000, 'max_tool_calls': 40, 'max_wall_clock_seconds': 90}}
        async def submit(body):
            request_headers = {**headers, 'Idempotency-Key': uuid.uuid4().hex}
            result = await client.post('/api/v4/evolution/runs', json=body, headers=request_headers)
            assert result.status_code == 201, await result.get_json()
            run = (await result.get_json())['run']
            submitted.append(run['run_id'])
            repeat = await client.post('/api/v4/evolution/runs', json=body, headers=request_headers)
            assert (await repeat.get_json())['run']['run_id'] == run['run_id']
            return run

        run = await submit(payload)
        deadline = time.monotonic() + 60
        while run['status'] not in runs.TERMINAL and time.monotonic() < deadline:
            assert not provider_errors, provider_errors
            await asyncio.sleep(.1)
            result = await client.get('/api/v4/evolution/runs/' + run['run_id'], headers=headers)
            run = (await result.get_json())['run']
        assert run['status'] == 'completed', (run['status'], run.get('error'), counts, provider_errors, run.get('review'))
        assert run['learning']['ok']
        assert len(run['receipts']) == 2
        episode = get_episode(run['episode_id'], user_id=owner)
        assert episode['status'] == 'success'
        assert episode['actuals']['tokens'] == run['actuals']['tokens']
        assert counts['implementation'] == counts['tests'] == 2
        assert run.get('actuals_complete', True)
        assert run['actuals']['tokens'] == sum(counts.values()) * 80
        strategies = retrieve_strategies(payload['objective'], user_id=owner)
        assert any(episode['episode_id'] in row['lineage'] for row in strategies)
        assert retrieve_strategies(payload['objective'], user_id=owner + 1) == []
        assert (project / 'clamp.py').read_text() == implementation
        final_check = await asyncio.to_thread(subprocess.run, check, cwd=project,
            capture_output=True, text=True, timeout=10)
        assert final_check.returncode == 0, final_check.stderr
        next_goal = True
        source_offsets.clear()
        prior_calls = sum(counts.values())
        followup_headers = {**headers, 'Idempotency-Key': uuid.uuid4().hex}
        guidance = {'kind': 'guidance', 'content': 'Keep the verified worker files; rerun their tests and explain the result without spawning new Workers.'}
        path = '/api/v4/evolution/runs/' + run['run_id'] + '/followups'
        response = await client.post(path, json=guidance, headers=followup_headers)
        assert response.status_code == 201, await response.get_json()
        child = (await response.get_json())['run']
        submitted.append(child['run_id'])
        repeated = await client.post(path, json=guidance, headers=followup_headers)
        assert (await repeated.get_json())['run']['run_id'] == child['run_id']
        assert child['parent_run_id'] == run['run_id']
        assert child['project_path'] == run['project_path']
        assert child['budget'] == run['budget']
        assert child['require_command_execution'] is True
        assert child['executor_schema_budget_tokens'] == 4000
        deadline = time.monotonic() + 60
        while child['status'] not in runs.TERMINAL and time.monotonic() < deadline:
            assert not provider_errors, provider_errors
            await asyncio.sleep(.1)
            response = await client.get('/api/v4/evolution/runs/' + child['run_id'], headers=headers)
            child = (await response.get_json())['run']
        assert child['status'] == 'completed', (child.get('error'), child.get('review'), provider_errors)
        assert child['actuals']['tokens'] == (sum(counts.values()) - prior_calls) * 80
        assert counts['implementation'] == counts['tests'] == 2
        assert any(actor == 'root' and 'Strategies observed on similar goals' in text
                   for actor, text in inputs)

    try:
        asyncio.run(journey())
    finally:
        for run_id in submitted:
            run = runs.get_run(run_id, user_id=owner)
            if run and run['status'] not in runs.TERMINAL:
                runs.cancel_run(run_id, user_id=owner)
        reset_dispatcher()
