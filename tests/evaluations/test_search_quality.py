"""Search diagnostic must prove retrieval, citations, equal conditions and cleanup."""
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

from evaluations.search_quality.contracts import (
    ARMS, DDGS_VERSION, SYSTEM, agent_config, score, task_prompt, tasks, tool_schemas, url_key,
)
from evaluations.search_quality.runner import (
    Workers, execute_tool, live, manifest, public_rounds, run_one, summarize,
)

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('project_version', ['9.8.7', None])
def test_worker_identity_describes_loaded_source_not_stale_distribution_metadata(
    tmp_path, monkeypatch, project_version,
):
    from evaluations.search_quality import worker

    package = tmp_path / 'tofu_search'
    package.mkdir()
    entry = package / '__init__.py'
    entry.write_text('# synthetic package source\n')
    if project_version is not None:
        (tmp_path / 'pyproject.toml').write_text(
            '[project]\nname = "tofu-search"\nversion = "9.8.7"\n')
    monkeypatch.setitem(sys.modules, 'tofu_search', SimpleNamespace(__file__=str(entry)))
    monkeypatch.setattr(worker.metadata, 'version', lambda name: '1.2.3')

    report = worker.identity('tofu')

    assert report['tofu-search'] == (project_version or '1.2.3')
    assert report['requests'] == '1.2.3'
    assert report['tofu_source_sha256'] == worker.python_tree_digest(package)


def test_worker_identity_rejects_a_source_project_without_a_version(tmp_path, monkeypatch):
    from evaluations.search_quality import worker

    package = tmp_path / 'tofu_search'
    package.mkdir()
    entry = package / '__init__.py'
    entry.write_text('# synthetic package source\n')
    (tmp_path / 'pyproject.toml').write_text('[project]\nname = "tofu-search"\n')
    monkeypatch.setitem(sys.modules, 'tofu_search', SimpleNamespace(__file__=str(entry)))
    monkeypatch.setattr(worker.metadata, 'version', lambda name: '1.2.3')

    with pytest.raises(ValueError, match='source.*version'):
        worker.identity('tofu')


def test_source_fingerprint_includes_the_declared_package_version(monkeypatch):
    from evaluations.search_quality.runner import source_fingerprint

    before = source_fingerprint(ROOT)
    declaration = ROOT / 'packages/tofu-search/pyproject.toml'
    original = Path.read_bytes

    def changed_version(path):
        if path == declaration:
            return b'[project]\nname = "tofu-search"\nversion = "99.99.99"\n'
        return original(path)

    monkeypatch.setattr(Path, 'read_bytes', changed_version)
    assert source_fingerprint(ROOT) != before


def evidence():
    task = tasks()[1]
    url = task['reference_url']
    body = 'Synthetic test text: JSONB begins in 3.45.0. SQLite complexity is O(N).'
    calls = [
        {'name': 'custom__search', 'accepted': True, 'is_error': False,
         'output': {'results': [{'url': url}]}},
        {'name': 'custom__fetch', 'accepted': True, 'is_error': False,
         'output': {'url': url, 'text': body}},
    ]
    answer = {
        'introduced': {'value': '3.45.0', 'url': url, 'quote': 'JSONB begins in 3.45.0.'},
        'complexity': {'value': 'O(N)', 'url': url, 'quote': 'SQLite complexity is O(N).'}}
    return task, calls, answer


def test_reference_requires_search_then_fetch_then_exact_observed_quote():
    task, calls, answer = evidence()
    assert score(task, json.dumps(answer), calls)['passed']
    assert not score(task, json.dumps(answer), [])['passed']
    assert not score(task, json.dumps(answer), calls[1:])['passed']
    assert not score(task, json.dumps(answer), calls[::-1])['passed']
    calls[-1]['accepted'] = False
    assert not score(task, json.dumps(answer), calls)['passed']


@pytest.mark.parametrize('policy,passed', [('reference_hosts', False), ('observed_pages', True)])
def test_citation_policy_separates_observed_evidence_from_reference_host_membership(policy, passed):
    task, calls, answer = evidence()
    task['citation_policy'] = policy
    alternate = 'https://independent.example/reference'
    calls[0]['output']['results'][0]['url'] = alternate
    calls[1]['output']['url'] = alternate
    for fact in answer.values():
        fact['url'] = alternate

    result = score(task, json.dumps(answer), calls)

    assert result['passed'] is passed
    assert result['citation_policy'] == policy
    assert not result['retrieval']['primary_page_fetched']
    assert all(not fact['primary_host'] for fact in result['facts'].values())
    assert all(fact['citation_host_allowed'] is passed for fact in result['facts'].values())


@pytest.mark.parametrize('fault', ['undiscovered', 'unfetched', 'failed_fetch', 'unaccepted',
                                  'invented_quote', 'wrong_value', 'invalid_url'])
def test_observed_page_policy_still_requires_successful_retrieval_and_literal_support(fault):
    task, calls, answer = evidence()
    task['citation_policy'] = 'observed_pages'
    if fault == 'undiscovered':
        calls.pop(0)
    elif fault == 'unfetched':
        calls.pop()
    elif fault == 'failed_fetch':
        calls[-1]['is_error'] = True
    elif fault == 'unaccepted':
        calls[-1]['accepted'] = False
    elif fault == 'invented_quote':
        answer['introduced']['quote'] = 'SQLite first offered JSONB in version 3.45.0.'
    elif fault == 'wrong_value':
        answer['introduced']['value'] = '3.46.0'
    elif fault == 'invalid_url':
        for fact in answer.values():
            fact['url'] = 'file:///tmp/source'
    assert not score(task, json.dumps(answer), calls)['passed']


@pytest.mark.parametrize('policy', ['any', '', None, {}, []])
def test_unknown_citation_policy_is_rejected_before_prompting_and_scoring(policy):
    task, calls, answer = evidence()
    task['citation_policy'] = policy
    with pytest.raises(ValueError, match='citation policy'):
        task_prompt(task)
    with pytest.raises(ValueError, match='citation policy'):
        score(task, json.dumps(answer), calls)


@pytest.mark.parametrize('fault', ['wrong_value', 'fabricated_quote', 'host_collision',
                                  'wrong_page', 'wrong_fact_keys', 'extra_field',
                                  'incomplete_quote', 'boolean_value'])
def test_unsupported_answers_do_not_pass(fault):
    task, calls, answer = evidence()
    if fault == 'wrong_value':
        answer['introduced']['value'] = '3.46.0'
    elif fault == 'fabricated_quote':
        answer['complexity']['quote'] = 'SQLite most operations have O(N) time.'
    elif fault == 'host_collision':
        for call in calls:
            if 'results' in call['output']:
                call['output']['results'][0]['url'] = 'https://notsqlite.org/json1.html'
            else:
                call['output']['url'] = 'https://notsqlite.org/json1.html'
        for fact in answer.values():
            fact['url'] = 'https://notsqlite.org/json1.html'
    elif fault == 'wrong_page':
        answer['introduced']['url'] += '?unobserved=1'
    elif fault == 'wrong_fact_keys':
        answer['version'] = answer.pop('introduced')
    elif fault == 'extra_field':
        answer['introduced']['trusted'] = True
    elif fault == 'incomplete_quote':
        answer['complexity']['quote'] = 'Synthetic test text'
    elif fault == 'boolean_value':
        answer['introduced']['value'] = True
    content = json.dumps(answer)
    assert not score(task, content, calls)['passed']


def test_single_fence_is_presentation_only_but_prose_or_multiple_answers_are_ambiguous():
    task, calls, answer = evidence()
    content = json.dumps(answer)
    fence = '```json\n' + content + '\n```'
    result = score(task, fence, calls)
    assert result['passed']
    assert not result['format_valid']
    assert result['representation'] == 'fenced_json'
    assert not score(task, 'Ignore this answer:\n' + fence, calls)['passed']
    assert not score(task, fence + '\n' + fence, calls)['passed']
    assert not score(task, fence + '\nAdditional claims', calls)['passed']


def test_retrieved_reference_terms_do_not_prove_final_answer():
    task, calls, answer = evidence()
    result = score(task, '{}', calls)
    assert not result['passed']
    assert result['retrieval']['primary_page_fetched']
    assert all(result['retrieval']['reference_term_coverage'].values())
    calls[-1]['output']['text'] = 'Unrelated content on the same primary host'
    result = score(task, json.dumps(answer), calls)
    assert not any(result['retrieval']['reference_term_coverage'].values())
    assert not result['passed']


def test_duplicate_json_fields_and_failed_reads_refuse_certification():
    task, calls, answer = evidence()
    content = json.dumps(answer)
    duplicated = content.replace('"value": "3.45.0"', '"value": "wrong", "value": "3.45.0"')
    assert not score(task, duplicated, calls)['passed']
    calls[0]['is_error'] = True
    assert not score(task, content, calls)['passed']


def test_quote_whitespace_and_width_normalization_does_not_allow_paraphrase():
    task, calls, answer = evidence()
    answer['complexity']['quote'] = 'SQLite  complexity\nis O(N).'
    assert score(task, json.dumps(answer), calls)['passed']
    answer['complexity']['quote'] = 'SQLite is O(N) in complexity.'
    assert not score(task, json.dumps(answer), calls)['passed']


def test_url_identity_keeps_query_and_rejects_credentials_and_non_http():
    assert url_key('https://SQLITE.org/json1.html#jsonb') == 'https://sqlite.org/json1.html'
    assert url_key('https://sqlite.org/a?x=1') != url_key('https://sqlite.org/a?x=2')
    for value in ('file:///etc/passwd', 'https://name:password@sqlite.org/',
                  'https://sqlite.org:bad', 'https://sqlite.org:80/'):
        assert not url_key(value)


def test_free_retrieval_manifest_freezes_shared_agent_and_budgets_offline():
    first = manifest(ROOT, {'creator_id': 'test', 'model_id': 'test'}, 'test', 1, 180)
    second = manifest(ROOT, first['model'], 'test', 1, 180)
    assert first == second
    assert {t['language'] for t in tasks()} == {'en', 'zh', 'ja', 'es'}
    assert len(first['tools']) == 2
    assert first['tools'] == tool_schemas()
    assert first['agent_config'] == agent_config()
    assert first['system'] == SYSTEM
    assert first['infrastructure_retries'] == 0
    assert first['common_fetch'].startswith('current tofu-search')
    assert first['leadership_claim'] is False
    for task in tasks():
        prompt = task_prompt(task)
        assert task['reference_url'] not in prompt
        assert all('"' + key + '"' in prompt for key in task['facts'])


def test_callback_policy_refuses_unknown_tools_and_unobserved_urls_before_network():
    class ForbiddenWorker:
        def call(self, *args, **kwargs):
            raise AssertionError('No worker should run')
    worker, task = ForbiddenWorker(), tasks()[0]
    with pytest.raises(ValueError, match='not admitted'):
        execute_tool(worker, 'tofu', task, 'run_command', {}, [], 10)
    with pytest.raises(ValueError, match='budget'):
        execute_tool(worker, 'tofu', task, 'custom__search', {'query': 'x'}, [{}] * 8, 10)
    assert execute_tool(worker, 'tofu', task, 'custom__fetch',
                        {'url': 'http://127.0.0.1/'}, [], 10)['error'] == 'url_not_returned_by_search'
    assert execute_tool(worker, 'ddgs', task, 'custom__search',
                        {'query': 'x', 'key': 'untrusted'}, [], 10)['error'] == 'invalid_arguments'
    with pytest.raises(TimeoutError):
        execute_tool(worker, 'tofu', task, 'custom__search', {'query': 'x'}, [], 0)


def test_model_provenance_uses_actual_dispatch_and_removes_credential_details():
    state = {'apiRounds': [{'model': 'requested', 'usage': {
        '_dispatch': {'model': 'actual', 'provider_id': 'other', 'key_hint': 'private'},
        'prompt_tokens': 12}, 'cost': {'costUsd': 0.25}}]}
    rows = public_rounds(state)
    assert rows == [{'model': 'actual', 'provider_id': 'other', 'cost': {'costUsd': 0.25},
                     'usage': {'prompt_tokens': 12}}]


def test_missing_or_changed_pair_provenance_never_counts_as_comparable():
    records = [{'task': 'a', 'trial': 0, 'arm': arm, 'provenance_valid': True,
                'schema_fingerprints': ['same'], 'passed': True, 'seconds': 1} for arm in ARMS]
    assert summarize(records, 2)['pairs'][0]['comparable']
    assert not summarize(records[:1], 2)['complete']
    assert not summarize([records[0], records[0]], 2)['complete']
    records[1]['schema_fingerprints'] = ['changed']
    assert not summarize(records, 2)['pairs'][0]['comparable']
    records[1]['schema_fingerprints'] = ['same']
    records[1]['provenance_valid'] = False
    assert not summarize(records, 2)['pairs'][0]['comparable']
    assert summarize(records, 2)['arms']['tofu']['application_cost_usd'] == [None]


def test_timed_out_worker_is_reaped_before_receipt(tmp_path):
    # An actual owned interpreter hangs before reading stdin. The production
    # retirement owner must join it before returning a timeout receipt.
    executable = tmp_path / 'hung-python'
    executable.write_text(f'#!{sys.executable}\nimport time\ntime.sleep(60)\n')
    executable.chmod(0o700)
    worker = Workers(ROOT, str(executable), str(executable), '')
    started = time.monotonic()
    receipt = worker.call('ddgs', 'identity', {}, seconds=0.05)
    assert receipt['error'] == 'worker_timeout'
    assert receipt['worker_settled'] is True
    assert time.monotonic() - started < 10
    with pytest.raises(ProcessLookupError):
        os.kill(receipt['worker_pid'], 0)


def test_workers_do_not_inherit_application_secrets_or_search_overrides():
    worker = Workers(ROOT, sys.executable, sys.executable, 'configured-proxy',
                     env={'API_TOKEN': 'secret', 'UNRELATED_SECRET': 'secret',
                          'DDGS_PROXY': 'different-proxy', 'HTTPS_PROXY': 'other',
                          'TOFU_SEARCH_SEARXNG_URL': 'http://private',
                          'SSL_CERT_FILE': '/approved/ca.pem', 'PATH': '/usr/bin'})
    assert worker.env == {'SSL_CERT_FILE': '/approved/ca.pem', 'PATH': '/usr/bin',
                          'PYTHONPATH': os.pathsep.join((str(ROOT / 'packages/tofu-search'), str(ROOT)))}


def test_named_api_token_is_excluded_even_if_its_name_is_normally_allowed(tmp_path, monkeypatch):
    from evaluations.search_quality.runner import main
    monkeypatch.setenv('SSL_CERT_FILE', 'synthetic-api-token')
    monkeypatch.setenv('SEARCH_TEST_PROXY', '')
    async def inspect_workers(args, root, registration, workers):
        assert 'SSL_CERT_FILE' not in workers.env
        assert os.environ[args.api_token_env] == 'synthetic-api-token'
    monkeypatch.setattr('evaluations.search_quality.runner.live', inspect_workers)
    assert main(['--live', '--creator', 'test', '--model', 'model', '--provider', 'test',
                 '--tofu-python', sys.executable, '--ddgs-python', sys.executable,
                 '--proxy-env', 'SEARCH_TEST_PROXY', '--api-token-env', 'SSL_CERT_FILE',
                 '--output', str(tmp_path / 'cohort')]) == 0


class ReplayAPI:
    """Scripted native HTTP transport, never a replacement model/tool loop."""
    def __init__(self, provider='test', refuse_callback=False):
        self.provider, self.refuse_callback = provider, refuse_callback
        self.calls = []

    def call(self, path, body=None):
        self.calls.append((path, body))
        if path == '/api/v4/agent/run':
            return {'task_id': 'owned-task', 'provider_id': 'test'}
        if '/events?' in path:
            return {'events': [
                {'type': 'tool_wire_projection', 'toolNames': [s['function']['name'] for s in tool_schemas()],
                 'executableToolCount': 2, 'schemaFingerprint': 'fixed'},
                {'type': 'custom_tool_call', 'toolName': 'custom__search',
                 'callId': 'search-1', 'arguments': {'query': 'SQLite JSONB'}},
                {'type': 'custom_tool_call', 'toolName': 'custom__fetch',
                 'callId': 'fetch-1', 'arguments': {'url': evidence()[0]['reference_url']}}],
                'next_cursor': 14, 'caught_up': True, 'done': True}
        if path.endswith('/tool_result'):
            return {'resolved': not self.refuse_callback, 'taskId': 'owned-task', 'callId': body['call_id']}
        if path.endswith('/abort'):
            return {}
        if path.endswith('/owned-task'):
            return {'id': 'owned-task', 'status': 'done', 'finishReason': 'stop',
                    'content': json.dumps(evidence()[2]), 'apiRounds': [
                        {'usage': {'_dispatch': {'model': 'model', 'provider_id': self.provider}}}]}
        raise AssertionError(path)


@pytest.mark.parametrize('provider,refuse_callback,passed',
                         [('test', False, True), ('changed', False, False), ('test', True, False)])
def test_native_handoff_records_failures_and_actual_provider(tmp_path, provider, refuse_callback, passed):
    from evaluations.programmatic_pilot.native_api import HTTPRuntime
    class Worker:
        def call(self, arm, operation, args, **kwargs):
            return copy.deepcopy(evidence()[1][0 if operation == 'search' else 1]['output'])
    api = ReplayAPI(provider, refuse_callback)
    runtime = HTTPRuntime(api, {'creator_id': 'test', 'model_id': 'model'}, 'test')
    result = asyncio.run(run_one(runtime, Worker(), evidence()[0], 'tofu', 0, tmp_path / 'run', 30))
    assert result['passed'] is passed
    assert runtime.in_flight == 0
    runtime.close()
    admission = api.calls[0][1]
    assert admission['config']['_customToolsMode'] == 'exclusive'
    assert len(admission['tools']) == 2
    assert (tmp_path / 'run' / 'record.json').is_file()
    assert sum(path == '/api/v4/agent/run' for path, _ in api.calls) == 1


def test_worker_cleanup_failure_remains_blocking_after_model_task_settles(tmp_path):
    from evaluations.programmatic_pilot.native_api import HTTPRuntime
    from lib.project_mod.process_group import ProcessGroupRetirementError
    class Worker:
        def call(self, *args, **kwargs):
            raise ProcessGroupRetirementError('Synthetic unjoined worker')
    runtime = HTTPRuntime(ReplayAPI(), {'creator_id': 'test', 'model_id': 'model'}, 'test')
    result = asyncio.run(run_one(runtime, Worker(), evidence()[0], 'tofu', 0, tmp_path / 'run', 30))
    assert runtime.in_flight == 0
    assert result['cleanup_failure'] == 'worker_settlement_unproven'
    assert not result['passed']
    runtime.close()


def test_source_mismatch_stops_before_any_application_call(tmp_path, monkeypatch):
    class API:
        def call(self, *args, **kwargs):
            raise AssertionError('No application call before source identity matches')
    class Worker:
        def call(self, *args, **kwargs):
            return {'identity': {'tofu_source_sha256': 'wrong-source'}}
    monkeypatch.setattr('evaluations.search_quality.runner.JsonAPI', lambda *a, **kw: API())
    args = SimpleNamespace(output=tmp_path / 'cohort', origin='http://127.0.0.1:15000',
                           api_token_env=None, provider='test', trials=1, timeout=30)
    registration = manifest(ROOT, {'creator_id': 'test', 'model_id': 'model'}, 'test', 1, 30)
    with pytest.raises(ValueError, match='identity preflight'):
        asyncio.run(live(args, ROOT, registration, Worker()))
    assert (args.output / 'preflight_failure.json').is_file()
    assert not (args.output / 'manifest.json').exists()


def test_registered_task_pack_is_snapshotted_and_drives_actual_execution(tmp_path, monkeypatch):
    from evaluations.search_quality import runner

    selected = [evidence()[0]]
    registration = manifest(ROOT, {'creator_id': 'test', 'model_id': 'model'}, 'test', 1, 30,
                            task_pack=selected)
    selected[0]['question'] = 'Changed by caller after registration'
    assert registration['tasks'][0]['question'] != selected[0]['question']
    seen, closed = [], []

    class Worker:
        proxy = ''
        def call(self, arm, operation, *args, **kwargs):
            assert operation == 'identity'
            return {'identity': {'tofu_source_sha256': registration['tofu_source_sha256'],
                                 'ddgs': DDGS_VERSION}}

    runtime = SimpleNamespace(in_flight=0, close=lambda: closed.append(True))
    monkeypatch.setattr(runner, 'HTTPRuntime', lambda *_a: runtime)
    monkeypatch.setattr(runner, 'JsonAPI', lambda *_a, **_kw: SimpleNamespace(call=lambda *_a: {}))

    async def execute(_runtime, _workers, task, arm, trial, _output, _timeout):
        seen.append((task['id'], arm))
        return {'task': task['id'], 'arm': arm, 'trial': trial, 'passed': True,
                'provenance_valid': True, 'schema_fingerprints': ['shared'],
                'seconds': 0, 'failure': None, 'cleanup_failure': None}

    monkeypatch.setattr(runner, 'run_one', execute)
    args = SimpleNamespace(output=tmp_path / 'cohort', origin='http://127.0.0.1:15000',
                           api_token_env=None, provider='test', trials=1, timeout=30)
    asyncio.run(live(args, ROOT, registration, Worker()))
    assert seen == [(registration['tasks'][0]['id'], arm) for arm in ARMS]
    assert closed == [True]
    assert json.loads((args.output / 'summary.json').read_text())['complete']


@pytest.mark.parametrize('fault', ['empty', 'duplicate', 'path_escape'])
def test_task_pack_refuses_ambiguous_or_unsafe_artifact_identities(fault):
    selected = [evidence()[0]]
    if fault == 'empty':
        selected = []
    elif fault == 'duplicate':
        selected *= 2
    else:
        selected[0]['id'] = '../escape'
    with pytest.raises(ValueError, match='task pack'):
        manifest(ROOT, {'creator_id': 'test', 'model_id': 'model'}, 'test', 1, 30,
                 task_pack=selected)


def test_changed_registered_tasks_stop_before_worker_or_application_creation(tmp_path, monkeypatch):
    from evaluations.search_quality import runner

    def forbidden(*_a, **_kw):
        pytest.fail('Changed task pack must not dispatch')

    registration = manifest(ROOT, {'creator_id': 'test', 'model_id': 'model'}, 'test', 1, 30)
    registration['tasks'][0]['question'] = 'Changed after registration'
    monkeypatch.setattr(runner, 'JsonAPI', forbidden)
    args = SimpleNamespace(output=tmp_path / 'cohort')
    with pytest.raises(ValueError, match='task pack'):
        asyncio.run(live(args, ROOT, registration, SimpleNamespace(call=forbidden)))


def test_ddgs_version_mismatch_stops_before_application_call(tmp_path, monkeypatch):
    from evaluations.search_quality import runner
    registration = manifest(ROOT, {'creator_id': 'test', 'model_id': 'model'}, 'test', 1, 30)

    class API:
        def call(self, *_a, **_kw):
            pytest.fail('Mismatched retrieval version must not reach application')

    class Worker:
        def call(self, *_a, **_kw):
            return {'identity': {'tofu_source_sha256': registration['tofu_source_sha256'],
                                 'ddgs': 'wrong-version'}}

    monkeypatch.setattr(runner, 'JsonAPI', lambda *_a, **_kw: API())
    args = SimpleNamespace(output=tmp_path / 'cohort', origin='http://127.0.0.1:15000',
                           api_token_env=None, provider='test', trials=1, timeout=30)
    with pytest.raises(ValueError, match='identity preflight'):
        asyncio.run(live(args, ROOT, registration, Worker()))
    assert (args.output / 'preflight_failure.json').is_file()


def test_unsettled_worker_stops_the_cohort_and_preserves_incomplete_summary(tmp_path, monkeypatch):
    from lib.project_mod.process_group import ProcessGroupRetirementError
    class API(ReplayAPI):
        def call(self, path, body=None):
            if path.endswith('/capabilities'):
                return {'version': 'test'}
            return super().call(path, body)
    registration = manifest(ROOT, {'creator_id': 'test', 'model_id': 'model'}, 'test', 1, 30)
    class Worker:
        proxy = ''
        def call(self, arm, operation, *args, **kwargs):
            if operation == 'identity':
                return {'identity': {'tofu_source_sha256': registration['tofu_source_sha256'],
                                     'ddgs': DDGS_VERSION}}
            raise ProcessGroupRetirementError('Synthetic worker still alive')
    api = API()
    monkeypatch.setattr('evaluations.search_quality.runner.JsonAPI', lambda *a, **kw: api)
    args = SimpleNamespace(output=tmp_path / 'cohort', origin='http://127.0.0.1:15000',
                           api_token_env=None, provider='test', trials=1, timeout=30)
    with pytest.raises(RuntimeError, match='Owned work not settled'):
        asyncio.run(live(args, ROOT, registration, Worker()))
    summary = json.loads((args.output / 'summary.json').read_text())
    assert not summary['complete']
    assert summary['source_unchanged']
    assert sum(path == '/api/v4/agent/run' for path, _ in api.calls) == 1


def test_client_callback_budget_includes_time_queued_behind_another_tool(tmp_path):
    from urllib.error import HTTPError
    from evaluations.programmatic_pilot.native_api import HTTPRuntime
    class API(ReplayAPI):
        elapsed = 0
        callback_timeout = 0
        def call(self, path, body=None):
            if path == '/api/v4/agent/run':
                self.callback_timeout = body['tools'][0]['execution']['timeout_s']
            if path.endswith('/tool_result') and self.elapsed > self.callback_timeout:
                raise HTTPError(path, 409, 'Client callback expired', {}, None)
            return super().call(path, body)
    api = API()
    class Worker:
        def call(self, arm, operation, args, **kwargs):
            # Two server handoffs are published together. Each operation stays
            # below 45s and both finish inside the 120s task budget.
            api.elapsed += 30
            assert 30 < kwargs['seconds']
            return copy.deepcopy(evidence()[1][0 if operation == 'search' else 1]['output'])
    runtime = HTTPRuntime(api, {'creator_id': 'test', 'model_id': 'model'}, 'test')
    result = asyncio.run(run_one(runtime, Worker(), evidence()[0], 'tofu', 0, tmp_path / 'run', 120))
    assert result['passed']
    assert all(call['accepted'] for call in result['calls'])
    assert runtime.in_flight == 0
    runtime.close()


def test_oversized_native_evidence_aborts_before_any_tool_operation(tmp_path, monkeypatch):
    from evaluations.programmatic_pilot.native_api import HTTPRuntime
    class API(ReplayAPI):
        def call(self, path, body=None):
            result = super().call(path, body)
            if '/events?' in path:
                result['events'].insert(0, {'type': 'delta', 'text': 'x' * 3000})
            return result
    class Worker:
        def call(self, *args, **kwargs):
            raise AssertionError('Evidence limit must stop tool dispatch')
    monkeypatch.setattr('evaluations.search_quality.runner.ARTIFACT_BYTES', 2048)
    runtime = HTTPRuntime(API(), {'creator_id': 'test', 'model_id': 'model'}, 'test')
    result = asyncio.run(run_one(runtime, Worker(), evidence()[0], 'tofu', 0, tmp_path / 'run', 30))
    assert result['failure'] == 'ValueError'
    assert result['calls'] == []
    assert not result['passed']
    assert runtime.in_flight == 0
    runtime.close()
