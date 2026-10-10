"""Exploration loop: dual-axis frontier, frame-change promotion, consumer lens."""
from __future__ import annotations

pytest_plugins = ('tests._credential_sidecar',)

import pytest

pytestmark = pytest.mark.unit

USER = 12


@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def _obs(obs_id, *, kind='premise_objection', subject=None, statement,
         actor='executor'):
    return {
        'schema_version': 1,
        'observation_id': obs_id,
        'kind': kind,
        'statement': statement,
        'subject': subject or {'type': 'tool', 'id': 'grep_search'},
        'lineage': ['e1'],
        'source': {'actor': actor},
        'status': 'open',
        'created_at': 1,
        'updated_at': 1,
    }


def test_friction_and_objection_clusters_become_probes(store):
    from lib import evolution
    evolution.record_observation(
        _obs('obs-f1', kind='friction',
             statement='tool grep_search truncated then identical retry'),
        user_id=USER, now=5)
    evolution.record_observation(
        _obs('obs-o1', statement='grep recovery premise does not hold'),
        user_id=USER, now=6)
    admitted = evolution.propose_probes(user_id=USER, now=100)
    markers = {p['statement'].split(']')[0] + ']' for p in admitted}
    assert '[probe:friction]' in markers
    assert '[probe:objection]' in markers
    assert all(p['exploratory'] for p in admitted)  # no derivable criteria


def test_surprise_axis_outranks_a_quiet_hopeless_domain(store):
    from lib import evolution
    from lib.evolution import curriculum
    digest = {'domain_rates': {'hopeless': {'samples': 8,
                                            'failure_rate': 1.0}}}
    quiet = curriculum._frontier_score('hopeless', digest, user_id=USER,
                                       cluster_count=0)
    protested = curriculum._frontier_score('hopeless', digest, user_id=USER,
                                           cluster_count=4)
    assert quiet == 0.0
    assert protested == 1.0  # a wall with protest beats walking away


def test_promote_creates_one_draft_goal_for_the_whole_cluster(store):
    from lib import evolution
    evolution.record_observation(_obs('obs-1', statement='premise is wrong'),
                                 user_id=USER)
    evolution.record_observation(
        _obs('obs-2', statement='premise is wrong, again'), user_id=USER)
    evolution.record_observation(
        _obs('obs-3', subject={'type': 'contract', 'id': 'other'},
             statement='unrelated noticing'), user_id=USER)
    result = evolution.promote_observation('obs-1', user_id=USER, now=100)
    goal = result['goal']
    assert goal['goal_id'].startswith('frame-')
    assert goal['statement'].startswith('[frame-change] premise is wrong')
    assert goal['exploratory'] is True
    assert sorted(result['promoted']) == ['obs-1', 'obs-2']
    assert evolution.get_goal(goal['goal_id'], user_id=USER) is not None
    # unrelated noticing stays open; the promoted cluster stops clustering
    assert [c['count'] for c in evolution.cluster_observations(
        user_id=USER)] == [1]
    again = evolution.promote_observation('obs-1', user_id=USER, now=200)
    assert again['already_promoted'] is True
    assert again['goal']['goal_id'] == goal['goal_id']


def test_frame_proposals_require_corroboration(store):
    from lib import evolution
    evolution.record_observation(_obs('obs-lone', statement='lonely doubt'),
                                 user_id=USER)
    assert evolution.propose_frame_changes(user_id=USER, now=100) == []
    evolution.record_observation(
        _obs('obs-c1', subject={'type': 'tool', 'id': 'web_fetch'},
             statement='fetch loses bytes'), user_id=USER)
    evolution.record_observation(
        _obs('obs-c2', subject={'type': 'tool', 'id': 'web_fetch'},
             statement='fetch loses bytes again'), user_id=USER)
    proposed = evolution.propose_frame_changes(user_id=USER, now=100)
    assert len(proposed) == 1
    assert proposed[0].startswith('frame-')
    assert evolution.propose_frame_changes(user_id=USER, now=101) == []


def test_awareness_surfaces_observation_clusters(store):
    from lib import evolution
    evolution.record_observation(_obs('obs-1', statement='premise is wrong'),
                                 user_id=USER)
    snapshot = evolution.system_snapshot(user_id=USER)
    assert snapshot['open_observations'] == 1
    assert snapshot['observation_clusters'][0]['count'] == 1
    assert snapshot['observation_clusters'][0]['kind'] == 'premise_objection'


def _review_task(content):
    return {'id': 'review-task-1', '_userId': USER, 'content': content}


def _review_json(**overrides):
    import json
    review = {
        'criterion_results': [
            {'criterion_id': key, 'outcome': 'pass',
             'evidence_ref': f'{key} ok'}
            for key in ('objective', 'evidence', 'delivery')],
    }
    review.update(overrides)
    return json.dumps(review)


def test_consumer_assessment_never_changes_the_verdict(store):
    from lib.evolution import runs
    assessment = {'meaningful_to_consumer': 'no',
                  'note': 'offset is meaningless to the model'}
    review = runs._parse_review(
        _review_task(_review_json(consumer_assessment=assessment)),
        run={'run_id': 'r1'}, user_id=USER)
    assert review['outcome'] == 'success'  # all pass, assessment aside
    from lib import evolution
    notes = evolution.list_observations(user_id=USER,
                                        kind='consumer_friction')
    assert len(notes) == 1
    assert 'offset is meaningless to the model' in notes[0]['statement']
    assert notes[0]['source']['actor'] == 'reviewer'


def test_consumer_assessment_yes_records_nothing(store):
    from lib.evolution import runs
    review = runs._parse_review(
        _review_task(_review_json(
            consumer_assessment={'meaningful_to_consumer': 'yes'})),
        run={'run_id': 'r1'}, user_id=USER)
    assert review['outcome'] == 'success'
    from lib import evolution
    assert evolution.list_observations(user_id=USER) == []


def test_review_without_assessment_still_validates(store):
    from lib.evolution import runs
    review = runs._parse_review(_review_task(_review_json()))
    assert review['outcome'] == 'success'
    assert 'consumer_assessment' not in review


def test_assessment_observation_failure_never_breaks_parsing(store,
                                                             monkeypatch):
    from lib.evolution import runs
    monkeypatch.setattr('lib.evolution.observations.record_observation',
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError('store down')))
    assessment = {'meaningful_to_consumer': 'no', 'note': 'x'}
    review = runs._parse_review(
        _review_task(_review_json(consumer_assessment=assessment)),
        run={'run_id': 'r1'}, user_id=USER)
    assert review['outcome'] == 'success'


# ── HTTP surface ─────────────────────────────────────────────────────

OWNER = 12_012


def _run(coro):
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def client(monkeypatch):
    from quart import Quart
    app = Quart(__name__, static_folder=None)
    app.config.setdefault('PROVIDE_AUTOMATIC_OPTIONS', True)
    app.config['TESTING'] = True
    from routes.api_v1.auth import (
        attach_rate_headers, bearer_auth_before_request,
    )
    app.before_request(bearer_auth_before_request)
    app.after_request(attach_rate_headers)
    from routes.api_v1.evolution import api_v1_evolution_bp
    app.register_blueprint(api_v1_evolution_bp)
    from tests.support.evolution_store import install_store
    store = install_store(monkeypatch)
    from lib.api_keys import create_key
    _row, token = create_key(owner_user_id=OWNER, name='evo-explore',
                             scopes=['tasks'])
    return app.test_client(), token, store


def test_observation_http_lifecycle(client):
    test_client, token, _store = client
    headers = {'Authorization': f'Bearer {token}'}

    async def flow():
        created = await test_client.post('/api/v4/evolution/observations',
                                         headers=headers, json={
                                             'kind': 'premise_objection',
                                             'statement': 'grep recovery '
                                                          'premise fails',
                                             'subject': {'type': 'tool',
                                                         'id': 'grep_search'},
                                         })
        assert created.status_code == 201
        body = await created.get_json()
        obs_id = body['observation']['observation_id']
        assert body['observation']['source'] == {'actor': 'owner'}
        # deterministic id: the same noticing re-posted is the same record
        again = await test_client.post('/api/v4/evolution/observations',
                                       headers=headers, json={
                                           'kind': 'premise_objection',
                                           'statement': 'grep recovery '
                                                        'premise fails',
                                           'subject': {'type': 'tool',
                                                       'id': 'grep_search'},
                                       })
        assert (await again.get_json())['observation'][
            'observation_id'] == obs_id
        listed = await test_client.get('/api/v4/evolution/observations',
                                       headers=headers)
        payload = await listed.get_json()
        assert len(payload['observations']) == 1
        assert payload['clusters'][0]['count'] == 1
        promoted = await test_client.post(
            f'/api/v4/evolution/observations/{obs_id}/promote',
            headers=headers)
        assert promoted.status_code == 201
        goal = (await promoted.get_json())['goal']
        assert goal['statement'].startswith('[frame-change]')
        dismissed = await test_client.post(
            '/api/v4/evolution/observations/obs-unknown/dismiss',
            headers=headers)
        assert dismissed.status_code in (404, 409)

    _run(flow())


def test_observation_routes_require_tasks_scope(client):
    from lib.api_keys import create_key
    test_client, _token, _store = client
    _row, weak = create_key(owner_user_id=OWNER, name='weak-explore',
                            scopes=['providers'])

    async def flow():
        response = await test_client.get(
            '/api/v4/evolution/observations',
            headers={'Authorization': f'Bearer {weak}'})
        assert response.status_code == 403

    _run(flow())

