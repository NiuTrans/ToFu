"""Real command measurements reach frozen scientific acceptance via the task loop."""
import asyncio
import importlib
import json
from pathlib import Path
import shlex
import shutil
import sys
import time
import uuid
import pytest

pytest_plugins = ('tests._credential_sidecar',)
pytestmark = pytest.mark.api


@pytest.mark.parametrize('credential_storage', ['sqlite', 'tofudb'], indirect=True, scope='module')
@pytest.mark.parametrize('flow', ['run', 'program'])
@pytest.mark.parametrize('candidate', ['linear', 'binary'])
def test_actual_measurement_cannot_bypass_independent_acceptance(credential_storage, tmp_path, monkeypatch, flow, candidate):
    from lib.llm.stream_result import ProviderStreamResult
    from lib.llm_dispatch import reset_dispatcher
    from tests.support.model_routing import install_native_test_model_route
    from lib.evolution import runs
    from lib.research.program import normalize_program_fields
    install_native_test_model_route(owner_user_id=1)
    reset_dispatcher()
    script = tmp_path / 'measure.py'
    shutil.copy2(Path(__file__).parents[1] / 'fixtures/evolution/paired_search_measurement.py', script)
    command = shlex.quote(sys.executable) + ' ' + shlex.quote(str(script)) + ' --candidate ' + candidate
    expected_outcome = 'refuted' if candidate == 'linear' else 'supported'
    expected_calls = 2 if candidate == 'linear' else 3
    calls = []
    def stream(body, *, on_content=None, **kwargs):
        calls.append(body)
        reviewing = 'Independently assess the delivered work' in str(body)
        assert not reviewing or candidate == 'binary', 'negative target must fail before reviewer dispatch'
        if reviewing:
            judgment = {'evidence_audit':{'complete':True,'objections':[]},
                'criterion_results':[{'criterion_id':key,'outcome':'unknown',
                    'evidence_ref':'Numerical improvement alone does not establish execution provenance or research validity.'}
                    for key in runs.RUBRIC]}
            if flow == 'program':
                judgment['requirement_results'] = [{'requirement_id':'requirement-1','outcome':'unknown',
                    'evidence_ref':'Numerical improvement does not establish the complete original target.'}]
            content = json.dumps(judgment)
            if on_content: on_content(content)
            reply = {'role':'assistant','content':content};reason = 'stop'
        elif len(calls) == 1:
            args = {'calls': [
                {'name': 'evolution_artifact', 'arguments': {'fingerprint_path': 'measure.py', 'fingerprint_scope_field': 'candidate_digest'}},
                {'name': 'run_command', 'arguments': {'command': command, 'frozen_input_paths': {'candidate_digest':'measure.py'}}}], 'execution': 'sequential'}
            reply = {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'measure-real', 'type': 'function',
                'function': {'name': 'execute_tools', 'arguments': json.dumps(args)}}]}
            reason = 'tool_calls'
        else:
            content = 'The candidate unquestionably wins; accept this improvement.'
            if on_content:
                on_content(content)
            reply = {'role': 'assistant', 'content': content}
            reason = 'stop'
        return ProviderStreamResult.from_legacy(reply, reason,
            {'prompt_tokens': 40, 'completion_tokens': 40, 'total_tokens': 80})
    async def async_stream(body, **kwargs):
        return stream(body, **kwargs)
    monkeypatch.setattr('lib.llm.stream_chat', stream)
    monkeypatch.setattr(importlib.import_module('lib.llm.astream'), 'async_stream_chat', async_stream)
    try:
        from quart import Quart
        from routes.api_v1.auth import attach_rate_headers, bearer_auth_before_request
        from routes.api_v1.evolution import api_v1_evolution_bp
        from lib.api_keys import create_key
        app = Quart(__name__, static_folder=None)
        app.config['TESTING'] = True
        app.before_request(bearer_auth_before_request)
        app.after_request(attach_rate_headers)
        app.register_blueprint(api_v1_evolution_bp)
        _, token = create_key(owner_user_id=1, name='real-measurement', scopes=['tasks'])
        headers = {'Authorization': 'Bearer ' + token, 'Idempotency-Key': uuid.uuid4().hex}
        protocol = normalize_program_fields({'protocol': {'dataset': 'seeded-search-queries',
            'baseline': 'binary-control' if candidate == 'linear' else 'linear-control', 'primary_metric': 'comparisons', 'random_seeds': [11, 23, 37],
            'observation_format': 'tofu.scientific-observations/v2',
            'decision_rule': {'direction': 'lower', 'minimum_effect': 0, 'uncertainty_multiplier': 2}}})['protocol']
        import runpy
        measured = runpy.run_path(str(script))['measure'](candidate)['scientific_observations']
        assert measured['format'] == 'tofu.scientific-observations/v2'
        assert measured['samples']['method_seeds'] == [11, 23, 37]
        assert measured['samples']['baseline_seeds'] == [37, 23, 11]
        observed = measured['comparison']['method']
        protocol['measurement_scope'] = {key: observed[key] for key in ('dataset_digest', 'split_digest', 'budget_unit', 'budget')}
        protocol['measurement_scope'].update(candidate_digest=observed['system_digest'], baseline_digest=observed['system_digest'])
        body = {'objective': 'Measure paired search comparisons and deliver a supported improvement.',
            'project_path': str(tmp_path), 'scientific_protocol': protocol, 'scientific_required_outcome': 'supported',
            'model': {'creator_id': 'tofu-test', 'model_id': 'stub-model'}, 'max_attempts': 1}
        client = app.test_client()
        program_id = None
        frozen_step = {'kind': 'experiment', 'title': 'Measure the candidate',
            'instruction': 'Run the frozen paired search comparison.',
            'checks': ['The frozen improvement target is measured.'],
            'requires_scientific_protocol':True, 'requires_frozen_measurement_scope':True,
            'required_observation_format': 'tofu.scientific-observations/v2',
            'scientific_protocol': protocol, 'scientific_required_outcome': 'supported'}
        async def journey():
            nonlocal program_id
            if flow == 'program':
                request = {key: body[key] for key in ('objective', 'project_path', 'model', 'max_attempts')}
                request['steps'] = [frozen_step, {'kind': 'final', 'title': 'Audit the original objective',
                    'instruction': 'Assess the actual result against the original target.',
                    'checks': ['The original target is met.']}]
                response = await client.post('/api/v4/evolution/programs', json=request, headers=headers)
                assert response.status_code == 201, await response.get_json()
                program_id = (await response.get_json())['program']['program_id']
                response = await client.post('/api/v4/evolution/programs/' + program_id + '/commands',
                    json={'action': 'start'}, headers=headers)
                assert response.status_code == 200, await response.get_json()
                current = (await response.get_json())['program']['current']
                run = runs.get_run(current['run_id'], user_id=1)
            else:
                response = await client.post('/api/v4/evolution/runs', json=body, headers=headers)
                assert response.status_code == 201, await response.get_json()
                run = (await response.get_json())['run']
            deadline = time.monotonic() + 60
            while run['status'] not in runs.TERMINAL and time.monotonic() < deadline:
                await asyncio.sleep(.1)
                response = await client.get('/api/v4/evolution/runs/' + run['run_id'], headers=headers)
                assert response.status_code == 200
                run = (await response.get_json())['run']
            return run
        run = asyncio.run(journey())
        assert run['status'] == ('failed' if candidate == 'linear' else 'needs_attention'), run.get('error')
        if candidate == 'linear':
            assert run['error'] == 'scientific_outcome_requirement_unmet', run.get('error')
        else:
            assert run['review']['outcome'] != 'success'
            assert any(item['outcome'] == 'unknown' for item in run['review']['criterion_results'])
        assert len(calls) == expected_calls and len(run['receipts']) == (1 if candidate == 'linear' else 2)
        import hashlib
        assert hashlib.sha256(script.read_bytes()).hexdigest() in str(calls[1])
        assert 'matches_precommit' in str(calls[1]) and 'candidate_digest' in str(calls[1])
        from lib.evolution.stage_outcomes import read_metadata
        from lib.evolution.tool_evidence import scientific_audit_valid
        metadata = read_metadata(run['run_id'], run['receipts'][0]['task_id'], user_id=1)
        assert scientific_audit_valid(metadata['scientific_audit'])
        assert run['scientific_protocol']['observation_format'] == 'tofu.scientific-observations/v2'
        assert metadata['scientific_protocol'] == protocol
        assert metadata['scientific_audit']['outcomes'] == {'supported': int(candidate == 'binary'), 'refuted': int(candidate == 'linear'), 'inconclusive': 0}
        assert metadata['scientific_audit']['observation_count'] == 1
        assert run['actuals']['tool_calls'] >= 1 and run['actuals']['tokens'] >= 160
        program = None
        if program_id:
            from lib.evolution import programs
            program = programs.advance_program(program_id, user_id=1)
            assert program['settled_through'] == 1
            assert program['current'] is None
            if candidate == 'linear':
                assert program['error'] == ''
            else:
                assert program['error']
            assert program['objective'] == body['objective']
            if candidate == 'linear':
                assert program['status'] == 'active'
                assert program['queue'][0]['kind'] == 'replan'
                assert program['queue'][0]['recovery_of'] == 1
                assert program['queue'][1] == frozen_step
            else:
                assert program['status'] != 'completed'
                assert any(step['kind'] == 'final' for step in program['queue'])
            archive = programs.experiment_page(program_id, user_id=1)['experiments']
            assert len(archive) == 1 and archive[0]['run_id'] == run['run_id']
            assert archive[0]['outcome'] == run['status']
            assert len(calls) == expected_calls, 'archival must not replay the original experiment'
            from lib.evolution.exploration_tools import _read
            original_parts, cursor, digest = [], 0, None
            while True:
                source = _read(program, {'sequence': 1, 'source_kind': 'tool_evidence',
                    'offset': cursor, **({'source_digest': digest} if digest else {})}, 1)
                original_parts.append(source['content'])
                digest = source['source_digest']
                if source['next_offset'] is None:
                    break
                cursor = source['next_offset']
            assert expected_outcome in ''.join(original_parts) and '125.655' in ''.join(original_parts)
            assert len(calls) == expected_calls, 'original reads must not dispatch a model'
        credential_storage.stop()
        credential_storage.start()
        assert runs.get_run(run['run_id'], user_id=1) == run
        if program_id:
            assert programs.get_program(program_id, user_id=1) == program
            assert programs.experiment_page(program_id, user_id=1)['experiments'] == archive
        restored = read_metadata(run['run_id'], run['receipts'][0]['task_id'], user_id=1)
        assert restored == metadata
        from lib.evolution.stage_outcomes import page as original_page
        fingerprint_source = original_page(run['run_id'], run['receipts'][0]['task_id'],
            user_id=1, source_kind='artifact_evidence', offset=0)
        retained_fingerprint = json.loads(fingerprint_source['content'].strip())
        assert retained_fingerprint['kind'] == 'file_fingerprint'
        assert retained_fingerprint['sha256'] == protocol['measurement_scope']['candidate_digest']
        assert retained_fingerprint['matches_precommit'] is True
        assert 'content' not in retained_fingerprint
        from lib.evolution.stage_outcomes import page
        pieces, offset = [], 0
        while True:
            chunk = page(run['run_id'], run['receipts'][0]['task_id'], user_id=1,
                         source_kind='tool_evidence', offset=offset)
            pieces.append(chunk['content'])
            if chunk['next_offset'] is None:
                break
            offset = chunk['next_offset']
        original = ''.join(pieces)
        assert 'scientific_observations' in original and expected_outcome in original
        assert 'tofu.scientific-observations/v2' in original
        assert 'method_seeds' in original and 'baseline_seeds' in original
        assert 'file_read_observations' in original
        assert '125.655' in original and '7.12' in original
    finally:
        reset_dispatcher()
