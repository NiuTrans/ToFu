#!/usr/bin/env python3
"""The persisted research artifacts must be REACHABLE — over HTTP, by direction.

THE DEFECT THIS PINS (epic pt_a40dbd9569194b52, second half).
The first half gave research artifacts a durable home in ``paper_reports``.
But durability with no read path is a warehouse with no door: the artifacts
sat on disk while the product had no way to ask for them.

  * ``GET /api/v1/tasks/<id>`` resolves against the IN-MEMORY task registry,
    so the moment ``cleanup_stale()`` evicts the task (TTL) or the process
    restarts, it 404s — precisely the window the persistence exists for.
  * The persisted row is keyed by DIRECTION hash and carries no task id, so
    even a remembered task id could not address it.

★ THE RECURRING FAILURE SHAPE THIS FILE EXISTS TO BREAK
Twice now this capability shipped a function that was written, exported, and
never called: first ``survey_lang_key`` / ``ideate_lang_key`` (0 callers while
the sibling ``insight_lang_key`` had 19), then ``load_research_artifacts``.
A guard asserting "the symbol exists" would have passed in BOTH cases.

So every assertion here drives the REAL Quart app through the REAL HTTP stack
and asserts what a CLIENT receives. The decisive test
(:func:`test_direction_lookup_works_with_no_live_task_at_all`) empties the task
registry first, so it can only pass if the response was served from durable
storage. A mocked engine or a bare symbol import cannot satisfy it.

Run:  pytest tests/research/test_research_lookup_route.py -m unit
"""
from __future__ import annotations

from pathlib import Path

import asyncio
import os
import sys

import pytest


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1


def _new_loop_run(coro):
    """Drive one coroutine on a private loop.

    The project convention for async route tests (see
    tests/agent_core/test_agent_poll_routes.py): the suite runs with
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1, so pytest-asyncio is NOT loaded and a
    ``@pytest.mark.asyncio`` test would be silently skipped rather than run —
    a guard that never executes is worse than no guard.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

_DIRECTION = 'long-context KV-cache compression'

_OPEN_GAPS = {
    'schema_version': 1, 'direction': _DIRECTION, 'lang': 'en',
    'surveyed_count': 18,
    'open_gaps': [{'id': 'gap_1', 'gap': 'no method preserves needle recall',
                   'why_open': 'only perplexity is reported',
                   'evidence': ['2305.11111'], 'kind_hint': 'methodology'}],
}

_IDEATE = {
    'accepted': [{
        'id': 'idea_1', 'title': 'Per-layer learnable compression rate',
        'kind': 'methodology', 'linked_gap_id': 'gap_1',
        'core_mechanism': 'attention entropy varies per layer',
        'novelty_claim': 'unlike 2305.11111 the rate is learned',
        'falsifiable_prediction': 'needle recall drops <2% at 4x',
        'why_not_AB': 'derived from the spectrum, not bolted on',
        'scores': {'novelty': 4, 'falsifiability': 5,
                   'mechanism_depth': 4, 'value': 4},
        'overall': 4.25}],
    'rejected': [{
        'id': 'idea_2', 'title': 'KV compression + speculative decoding',
        'reject_stage': 'rubric', 'reject_reason': 'overall 2.75 < 4.0',
        'scores': {'novelty': 2, 'falsifiability': 3,
                   'mechanism_depth': 2, 'value': 4},
        'overall': 2.75}],
    'threshold': 4.0, 'gate_reached': 'accepted',
}


# ── Fixtures: the REAL app, the REAL DB ────────────────────────────────────

@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    import lib.research.persistence as persistence
    import lib.evolution.records as evolution_records
    from lib.storage import StorageSupervisor

    supervisor = StorageSupervisor(
        project_root=tmp_path / 'sidecar', backend='sqlite', startup_timeout=60)
    supervisor.start()
    monkeypatch.setattr(
        persistence, '_storage', lambda **_kwargs: supervisor.client)
    monkeypatch.setattr(evolution_records, '_storage', lambda **_kwargs: supervisor.client)
    try:
        yield supervisor
    finally:
        supervisor.stop()


@pytest.fixture()
def client(fresh_db):
    """A real test client over the real blueprint registration.

    Deliberately NOT a hand-built mini-app: registering the blueprint by hand
    here would let a route that is never mounted in production still pass.
    """
    import server
    app = server.app
    app.config['TESTING'] = True
    return app.test_client()


@pytest.fixture()
def seeded(fresh_db):
    from lib.research.persistence import persist_ideate, persist_survey
    persist_survey(
        _DIRECTION, 'en', '# Survey\n\nBody.', _OPEN_GAPS, model='m1',
        user_id=TEST_OWNER_USER_ID)
    persist_ideate(
        _DIRECTION, 'en', _IDEATE, model='m1',
        user_id=TEST_OWNER_USER_ID)


def _empty_the_task_registry():
    """Evict every research task — what cleanup_stale() does past TTL and
    what a process restart does absolutely."""
    from lib.research.runtime import _research_runtime
    with _research_runtime._lock:
        _research_runtime._tasks.clear()


def _get_json(client, path, **query):
    from tests.support.envelope import v1_shaped
    resp = _new_loop_run(client.get(path, query_string=query))
    return resp.status_code, v1_shaped(_new_loop_run(resp.get_json()))


# ── 1. ★ The decisive criterion ────────────────────────────────────────────

def test_durable_run_http_reopens_after_registry_loss(client, fresh_db, tmp_path):
    from lib.research.records import persist_record
    from lib.research.runtime import _new_research_task, _research_task_id, _research_runtime
    task_id = _research_task_id()
    task = _new_research_task(task_id, direction='Database run', workdir=str(tmp_path),
                              lang='en', user_id=TEST_OWNER_USER_ID)
    persist_record(task, event={'type': 'paper_done', 'result': {
        'arxivId': '2401.00001', 'title': 'Durable captured title', 'status': 'parsed'}})
    assert _research_runtime.remove_owned(task_id, user_id=TEST_OWNER_USER_ID)
    status, body = _get_json(client, '/api/v4/research/records/' + task_id)
    assert status == 200
    record = body['record']
    assert record['status'] == 'interrupted'
    assert record['papers'][0]['title'] == 'Durable captured title'
    assert record['resumable'] is True
    status, body = _get_json(client, '/api/v4/research/records')
    assert status == 200
    assert any(item['task_id'] == task_id for item in body['items'])


def test_direction_lookup_works_with_no_live_task_at_all(client, seeded):
    """A finished job must stay readable after its task is gone.

    This is the whole point of the persistence layer: the registry is empty
    (TTL swept / server restarted), so a response can ONLY come from disk.
    """
    _empty_the_task_registry()

    status, body = _get_json(
        client, '/api/v4/research/lookup', direction=_DIRECTION, lang='en')

    assert status == 200, f'lookup failed with no live task: {status} {body}'
    assert body.get('found') is True, (
        'the persisted research was NOT reachable once the in-memory task was '
        f'evicted — the durable read path does not work: {body}')
    assert body.get('survey_md', '').startswith('# Survey')
    assert body['open_gaps']['open_gaps'][0]['id'] == 'gap_1'
    acc = body.get('accepted') or []
    assert len(acc) == 1 and acc[0]['core_mechanism'], \
        'accepted ideas came back without their mechanism text'
    rej = body.get('rejected') or []
    assert rej and rej[0]['scores']['novelty'] == 2, (
        'the four-axis rubric scores did not survive the HTTP round-trip — '
        'they are the calibration data for IDEATE_GATE_THRESHOLD')
    assert body.get('threshold') == 4.0
    assert body.get('gate_reached') == 'accepted'


def test_lookup_is_case_and_whitespace_insensitive_over_http(client, seeded):
    """A human retypes the direction. It must still hit the stored row —
    a miss re-runs the whole pipeline at full cost."""
    _empty_the_task_registry()
    status, body = _get_json(
        client, '/api/v4/research/lookup',
        direction='  Long-Context  KV-Cache Compression  ', lang='en')
    assert status == 200 and body.get('found') is True, (
        f'a retyped direction missed the persisted row: {body}')


# ── 2. Honest empties + input validation ──────────────────────────────────

def test_unresearched_direction_is_an_honest_empty_not_a_404(client):
    """The re-attach path calls this on every open. A never-researched
    direction is a normal answer (found=false), not an error."""
    status, body = _get_json(
        client, '/api/v4/research/lookup', direction='never researched', lang='en')
    assert status == 200, f'expected 200 honest-empty, got {status}'
    assert body.get('found') is False
    assert body.get('accepted') == [] and body.get('survey_md') == ''


def test_missing_direction_is_rejected(client):
    status, _ = _get_json(client, '/api/v4/research/lookup', lang='en')
    assert status == 400, 'a lookup with no direction must be a 400'


def test_languages_are_separate_over_http(client, seeded):
    """`seeded` wrote only 'en'. Asking for 'zh' must not serve the English
    row — the composite key is per-language."""
    _empty_the_task_registry()
    _, en = _get_json(client, '/api/v4/research/lookup',
                      direction=_DIRECTION, lang='en')
    _, zh = _get_json(client, '/api/v4/research/lookup',
                      direction=_DIRECTION, lang='zh')
    assert en.get('found') is True
    assert zh.get('found') is False, 'the zh lookup served the en row'


# ── 3. Degraded must stay visible through the read path ───────────────────

def test_degraded_flag_survives_the_persist_and_the_lookup(client, fresh_db):
    """A degraded run keeps status='done' by design, so if the quality flag
    is dropped anywhere along persist→load→HTTP, a 100%% gate wipe reads as a
    clean success on re-open."""
    from lib.research.persistence import persist_ideate
    wiped = dict(_IDEATE, accepted=[], gate_reached='structural',
                 degraded=True,
                 degraded_reason='structural gate rejected ALL 6 idea(s)')
    persist_ideate(
        _DIRECTION, 'en', wiped, model='m1',
        user_id=TEST_OWNER_USER_ID)
    _empty_the_task_registry()

    status, body = _get_json(
        client, '/api/v4/research/lookup', direction=_DIRECTION, lang='en')
    assert status == 200
    assert body.get('degraded') is True, (
        'a degraded run reads back as a clean success — the quality axis was '
        'lost between the DB and the client')
    assert 'structural gate' in (body.get('degraded_reason') or '')


# ── 4. The wiring pins: a real caller, a real mount ───────────────────────

def test_route_is_mounted_via_normal_blueprint_registration(client):
    """The route must reach clients through the app's own registration, not
    a test-only mount."""
    import server
    rules = {str(r) for r in server.app.url_map.iter_rules()}
    assert '/api/v4/research/lookup' in rules, (
        'the lookup route is not on the real app url_map — it would 404 in '
        f'production. Mounted /api/v1/research/*: '
        f'{[r for r in rules if "research" in r]}')


def test_the_persistence_loader_has_a_real_caller():
    """★ The anti-orphan pin.

    `load_research_artifacts` was the SECOND function in this epic to be
    written, exported, and never called. Assert it is imported by a ROUTE
    module (production code, not tests) — the check that would have caught
    both instances.
    """
    from tests._source_scan import strip_comments  # shared scanner (charter #24)
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    src = strip_comments(
        open(os.path.join(root, 'routes', 'api_v1', 'research.py'),
             encoding='utf-8').read(), lang='python')
    assert 'load_research_artifacts' in src, (
        'no route calls load_research_artifacts — the persisted artifacts are '
        'unreachable from the product, which is the exact orphan shape this '
        'epic has already produced twice')


def test_frontend_client_exposes_the_lookup():
    """api.js is the single seam for backend calls (charter: no raw fetch
    outside it). A route with no client method is unreachable from the UI."""
    src = (Path(__file__).resolve().parents[2] / 'frontend/src/api/endpoint-registry/research.ts').read_text()
    assert '/api/v4/research/lookup' in src, (
        'Api has no method for the research lookup — the frontend cannot '
        'reach the persisted artifacts')


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-x', '-q', '-m', 'unit']))


def test_action_observations_are_reachable_without_a_live_task(client, fresh_db, monkeypatch):
    from lib.research.action_observations import ActionObservations, _validator
    from tests.research.test_research_action_observations import repository, capture
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: fresh_db.client)
    task_id = 'research_action_observed'
    for ordinal in (1, 2, 3):
        capture(repository(fresh_db.client), 'retained original-' + str(ordinal), ordinal=ordinal)
    # No action runtime task is ever created; reads use only acknowledged storage.
    status, body = _get_json(client, '/api/v4/research/actions/' + task_id + '/observations', limit=2)
    assert status == 200 and body['found'] is True
    _validator('recoveryPage').validate({key: value for key, value in body.items() if key != 'ok'})
    assert [row['ordinal'] for row in body['observations']] == [1, 2]
    assert body['binding']['revision'] == 7
    status, page = _get_json(client, '/api/v4/research/actions/' + task_id + '/observations',
                             limit=2, cursor=body['next_cursor'])
    assert status == 200 and [row['ordinal'] for row in page['observations']] == [3]
    assert page['next_cursor'] == ''


def test_action_observation_api_does_not_reveal_other_owner(client, fresh_db, monkeypatch):
    from tests.research.test_research_action_observations import repository, capture
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: fresh_db.client)
    capture(repository(fresh_db.client, user_id=2), 'other owner evidence')
    status, body = _get_json(client, '/api/v4/research/actions/research_action_observed/observations')
    body.pop('ok', None)
    assert status == 200 and body == {'found': False, 'binding': None, 'observations': [], 'next_cursor': ''}


@pytest.mark.parametrize('query', [{'limit': 0}, {'limit': 17}, {'limit': 'bad'}, {'cursor': 'foreign:1'}])
def test_action_observation_api_rejects_invalid_pagination(client, query):
    status, body = _get_json(client, '/api/v4/research/actions/research_action_observed/observations', **query)
    assert status == 400


def test_action_observation_api_storage_fault_cannot_be_empty_success(client, monkeypatch):
    from lib.storage.errors import StorageError
    def unavailable(*args, **kwargs):
        raise StorageError('database_unavailable', 'injected recovery storage refusal')
    monkeypatch.setattr('lib.research.action_observations.load_action_observation_page', unavailable)
    response = _new_loop_run(client.get('/api/v4/research/actions/research_action_observed/observations'))
    assert response.status_code >= 500


def test_observation_original_downloads_full_verified_bytes(client, fresh_db, monkeypatch):
    from tests.research.test_research_action_observations import repository, capture
    import hashlib
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: fresh_db.client)
    content = '汉字🧪 complete original <script>text</script>' * 2000
    capture(repository(fresh_db.client), content)
    response = _new_loop_run(client.get('/api/v4/research/actions/research_action_observed/observations/1/original'))
    assert response.status_code == 200
    assert _new_loop_run(response.get_data()) == content.encode('utf-8')
    assert response.headers['Content-Length'] == str(len(content.encode('utf-8')))
    assert response.headers['ETag'] == '"' + hashlib.sha256(content.encode()).hexdigest() + '"'
    assert 'attachment;' in response.headers['Content-Disposition']
    assert response.headers['X-Content-Type-Options'] == 'nosniff'


def test_observation_original_hides_other_owner_and_missing_ordinal(client, fresh_db, monkeypatch):
    from tests.research.test_research_action_observations import repository, capture
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: fresh_db.client)
    capture(repository(fresh_db.client, user_id=2), 'private original')
    response = _new_loop_run(client.get('/api/v4/research/actions/research_action_observed/observations/1/original'))
    assert response.status_code == 404
    capture(repository(fresh_db.client), 'own original')
    response = _new_loop_run(client.get('/api/v4/research/actions/research_action_observed/observations/2/original'))
    assert response.status_code == 404


def test_observation_original_corruption_fails_before_download_success(client, fresh_db, monkeypatch):
    from tests.research.test_research_action_observations import repository, capture
    from lib.research.action_observations import PART_NAMESPACE
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: fresh_db.client)
    repo = repository(fresh_db.client)
    manifest = capture(repo, 'original' * 10000)
    key = f"{repo.prefix}1:{manifest['result_digest']}:2"
    fresh_db.client.command('record.delete', {'namespace': PART_NAMESPACE, 'key': key}, 'delete-test-export-part')
    response = _new_loop_run(client.get('/api/v4/research/actions/research_action_observed/observations/1/original'))
    assert response.status_code >= 500
    assert 'Content-Disposition' not in response.headers


def test_effect_recovery_exposes_unknown_effect_without_granting_replay(client, fresh_db, monkeypatch):
    from tests.research.test_research_action_observations import intent_repository, capture
    from lib.research.action_observations import _validator
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: fresh_db.client)
    repo = intent_repository(fresh_db.client)
    repo.record_intent(ordinal=1, call_id='call-1', tool='run_command', arguments={'command': 'opaque command'})
    status, body = _get_json(client, '/api/v4/research/actions/research_action_observed/effects')
    assert status == 200 and body['found'] is True
    _validator('effectsPage').validate({key:value for key,value in body.items() if key != 'ok'})
    assert body['effects'][0]['state'] == 'unresolved_effect'
    assert body['effects'][0]['replay_allowed'] is False
    assert 'arguments' not in body['effects'][0]['intent']
    capture(repo, 'observed result')
    status, body = _get_json(client, '/api/v4/research/actions/research_action_observed/effects')
    assert status == 200 and body['effects'][0]['state'] == 'observation_published'
    assert body['effects'][0]['replay_allowed'] is False


def test_effect_recovery_hides_foreign_and_missing_tasks(client, fresh_db, monkeypatch):
    from tests.research.test_research_action_observations import intent_repository
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: fresh_db.client)
    repo = intent_repository(fresh_db.client)
    repo.record_intent(ordinal=1, call_id='call-1', tool='run_command', arguments={})
    monkeypatch.setattr('routes.api_v1.research.request_user_id', lambda: 2)
    status, body = _get_json(client, '/api/v4/research/actions/research_action_observed/effects')
    assert status == 200 and body['found'] is False and body['effects'] == []
    status, body = _get_json(client, '/api/v4/research/actions/missing-task/effects')
    assert status == 200 and body['found'] is False


@pytest.mark.parametrize('query', [{'limit': 0}, {'limit': 17}, {'limit': 'bad'}, {'cursor': 'foreign:1'}])
def test_effect_recovery_rejects_invalid_paging(client, query):
    status, _ = _get_json(client, '/api/v4/research/actions/missing-task/effects', **query)
    assert status == 400


def test_model_call_usage_recovery_works_without_live_task(client, fresh_db, monkeypatch):
    from tests.research.test_research_model_accounting import repository, admit, snapshot
    from lib.research.action_observations import _validator
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: fresh_db.client)
    repo = repository(fresh_db.client); intent = admit(repo)
    status, body = _get_json(client, '/api/v4/research/actions/research_action_observed/model-calls')
    assert status == 200 and body['calls'][0]['state'] == 'unresolved_usage'
    assert body['calls'][0]['replay_allowed'] is False
    repo.record_model_usage(intent, snapshot(), usage_mapping_received=False)
    status, body = _get_json(client, '/api/v4/research/actions/research_action_observed/model-calls')
    assert status == 200 and body['calls'][0]['state'] == 'usage_recorded'
    assert body['calls'][0]['usage']['usage_snapshot']['agent_dispatches'] == 1
    assert body['calls'][0]['usage']['usage_mapping_received'] is False
    _validator('modelCallsPage').validate({key:value for key,value in body.items() if key != 'ok'})
    monkeypatch.setattr('routes.api_v1.research.request_user_id', lambda: 2)
    status, body = _get_json(client, '/api/v4/research/actions/research_action_observed/model-calls')
    assert status == 200 and body['found'] is False and body['calls'] == []


@pytest.mark.parametrize('query', [{'limit': 0}, {'limit': 17}, {'limit': 'bad'}, {'cursor': 'foreign:1'}])
def test_model_calls_recovery_rejects_invalid_paging(client, query):
    status, _ = _get_json(client, '/api/v4/research/actions/missing-task/model-calls', **query)
    assert status == 400


def test_action_terminal_is_available_after_task_eviction_and_hides_foreign_owner(client, fresh_db, monkeypatch):
    from lib.agent_core.task_runtime import TaskRuntime
    from lib.research.action_terminal import persist_action_terminal
    from tests.research.test_research_action_terminal import create
    from lib.research.action_observations import _validator
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: fresh_db.client)
    runtime = TaskRuntime('terminal-api', terminal_persist=persist_action_terminal)
    task = create(runtime, task_id='terminal-api-fixture')
    runtime.finish(task['id'], result={'summary': 'committed'}, degraded=True, degraded_reason='test degradation')
    runtime.remove_owned(task['id'], user_id=1)
    status, body = _get_json(client, '/api/v4/research/actions/terminal-api-fixture/terminal')
    assert status == 200 and body['terminal']['status'] == 'done'
    assert body['terminal']['committed_workspace_revision'] == 4
    assert body['terminal']['artifact_quality']['degraded'] is True
    _validator('actionTerminalResponse').validate({key:value for key,value in body.items() if key != 'ok'})
    monkeypatch.setattr('routes.api_v1.research.request_user_id', lambda: 2)
    status, body = _get_json(client, '/api/v4/research/actions/terminal-api-fixture/terminal')
    assert status == 200 and body['found'] is False and body['terminal'] is None
