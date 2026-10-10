#!/usr/bin/env python3
"""The protocol draft must be grounded, bounded and never self-persisted.

THE UX DEFECT THIS PINS. The last step of Auto Research presented the owner
with a blank falsification contract (metric, baseline, dataset, falsifier,
seeds, environment, evaluation, stops, resources) even though the pipeline
had already paid for a survey, an open-gap map and accepted ideas. Filling
it from memory is both slow and lower quality than the evidence allows.

The fix is ``lib.research.draft.draft_protocol``: ONE bounded, tool-less
model call that turns the persisted evidence into a complete protocol draft.
The draft is a proposal — authority stays with the versioned workspace PUT,
so these tests also pin that the module never touches persistence writes.

Run:  pytest tests/research/test_research_draft_protocol.py -m unit
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.support.envelope import v1_get_json

pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1
_DIRECTION = 'long-context KV-cache compression'

_ARTIFACTS = {
    'found': True,
    'survey_md': '# Survey\n\nneedle recall degrades beyond 2x compression.',
    'open_gaps': {
        'open_gaps': [{'id': 'gap_1', 'gap': 'no method preserves needle recall',
                       'why_open': 'only perplexity is reported'}],
    },
    'accepted': [{
        'id': 'idea_1', 'title': 'Per-layer learnable compression rate',
        'kind': 'methodology', 'linked_gap_id': 'gap_1',
        'core_mechanism': 'attention entropy varies per layer',
        'novelty_claim': 'unlike 2305.11111 the rate is learned',
        'falsifiable_prediction': 'needle recall drops <2% at 4x',
    }],
}

_MODEL_REPLY = {
    'hypothesis': 'Per-layer learned rates preserve needle recall at 4x',
    'primary_metric': 'needle-in-haystack recall @ 4x',
    'baseline': 'uniform 4x KV eviction',
    'dataset': 'LongBench needle suite',
    'falsifier': 'recall drops >=2% vs uncompressed',
    'evaluation_protocol': 'three seeds, paired bootstrap CI',
    'environment': '1xA100 80G, torch 2.4',
    'random_seeds': [13, 42, 7],
    'stop_conditions': ['recall CI crosses 2%', 'budget exceeds 6h'],
    'resources': 'one A100-day, 3 model runs, 12k prompts',
}


def _fake_dispatch(payload: dict):
    """A provider-shaped stub: streams the JSON reply, then finishes."""
    def _dispatch(messages, **kwargs):
        on_content = kwargs.get('on_content')
        text = json.dumps(payload)
        if callable(on_content):
            on_content(text[: len(text) // 2])
            on_content(text[len(text) // 2:])
        # Legacy 3-tuple with an explicit finish reason → verified complete.
        return {'role': 'assistant', 'content': text}, 'stop', {
            'prompt_tokens': 100, 'completion_tokens': 50}
    return _dispatch


@pytest.fixture()
def patched(monkeypatch):
    import lib.research.draft as draft
    calls = []

    def _dispatch(messages, **kwargs):
        calls.append({'messages': messages, 'kwargs': kwargs})
        return _fake_dispatch(_MODEL_REPLY)(messages, **kwargs)

    monkeypatch.setattr(draft, 'load_research_artifacts',
                        lambda *a, **k: dict(_ARTIFACTS))
    monkeypatch.setattr(draft, 'dispatch_stream', _dispatch)
    return draft, calls


# ── 1. The happy path: grounded, normalized, bounded ──────────────────────

def test_draft_returns_normalized_protocol_grounded_in_evidence(patched):
    draft, calls = patched
    out = draft.draft_protocol(
        _DIRECTION, 'en', 'idea_1', user_id=TEST_OWNER_USER_ID)

    assert out['idea_id'] == 'idea_1'
    assert out['hypothesis'] == _MODEL_REPLY['hypothesis']
    protocol = out['protocol']
    assert protocol['primary_metric'] == _MODEL_REPLY['primary_metric']
    assert protocol['baseline'] == _MODEL_REPLY['baseline']
    assert protocol['dataset'] == _MODEL_REPLY['dataset']
    assert protocol['falsifier'] == _MODEL_REPLY['falsifier']
    assert protocol['random_seeds'] == [13, 42, 7]
    assert protocol['stop_conditions'] == [
        'recall CI crosses 2%', 'budget exceeds 6h']

    (call,) = calls
    # Grounding: survey excerpt, idea packet and linked gap all reach the model.
    user_prompt = call['messages'][1]['content']
    assert 'needle recall degrades' in user_prompt
    assert 'attention entropy varies per layer' in user_prompt
    assert 'no method preserves needle recall' in user_prompt
    # The call is bounded and deterministic.
    assert call['kwargs']['temperature'] == 0.0
    assert call['kwargs']['max_tokens'] <= 4000
    assert call['kwargs']['thinking_enabled'] is False


def test_draft_matches_the_frontend_positional_idea_id(patched):
    """The workspace stores ``idea-<n>`` when the ideate stage saved no id."""
    draft, _ = patched
    row = dict(_ARTIFACTS['accepted'][0])
    row.pop('id')
    artifacts = dict(_ARTIFACTS, accepted=[row])
    patched_load = lambda *a, **k: dict(artifacts)  # noqa: E731
    import lib.research.draft as draft_mod
    draft_mod.load_research_artifacts = patched_load

    out = draft.draft_protocol(
        _DIRECTION, 'en', 'idea-1', user_id=TEST_OWNER_USER_ID)
    assert out['idea_id'] == 'idea-1'
    assert out['protocol']['falsifier'] == _MODEL_REPLY['falsifier']


def test_draft_falls_back_to_the_idea_prediction_for_hypothesis(patched):
    draft, _ = patched
    reply = dict(_MODEL_REPLY, hypothesis='')
    import lib.research.draft as draft_mod
    draft_mod.dispatch_stream = _fake_dispatch(reply)

    out = draft.draft_protocol(
        _DIRECTION, 'en', 'idea_1', user_id=TEST_OWNER_USER_ID)
    assert out['hypothesis'] == 'needle recall drops <2% at 4x'


# ── 2. Fail-closed cases ───────────────────────────────────────────────────

def test_unknown_direction_or_idea_is_rejected(patched):
    draft, _ = patched
    import lib.research.draft as draft_mod

    draft_mod.load_research_artifacts = lambda *a, **k: {'found': False}
    with pytest.raises(ValueError, match='no persisted research artifacts'):
        draft.draft_protocol(
            'never researched', 'en', 'idea_1', user_id=TEST_OWNER_USER_ID)

    draft_mod.load_research_artifacts = lambda *a, **k: dict(_ARTIFACTS)
    with pytest.raises(ValueError, match='unknown idea_id'):
        draft.draft_protocol(
            _DIRECTION, 'en', 'idea_999', user_id=TEST_OWNER_USER_ID)
    with pytest.raises(ValueError, match='required'):
        draft.draft_protocol('', 'en', 'idea_1', user_id=TEST_OWNER_USER_ID)


def test_unparseable_or_empty_model_reply_is_rejected(patched):
    draft, _ = patched
    import lib.research.draft as draft_mod

    def _garbage(messages, **kwargs):
        on_content = kwargs.get('on_content')
        if callable(on_content):
            on_content('not json at all')
        return {'role': 'assistant', 'content': 'not json at all'}, 'stop', {}

    draft_mod.dispatch_stream = _garbage
    with pytest.raises(ValueError, match='no valid JSON'):
        draft.draft_protocol(
            _DIRECTION, 'en', 'idea_1', user_id=TEST_OWNER_USER_ID)

    # With no model text AND no idea fallback, the draft is unusable.
    draft_mod.load_research_artifacts = lambda *a, **k: dict(
        _ARTIFACTS, accepted=[{'id': 'idea_1', 'title': 'bare'}])
    draft_mod.dispatch_stream = _fake_dispatch({})
    with pytest.raises(ValueError, match='empty protocol'):
        draft.draft_protocol(
            _DIRECTION, 'en', 'idea_1', user_id=TEST_OWNER_USER_ID)


def test_drafting_never_persists_anything(patched, monkeypatch):
    """The workspace PUT is the only write authority — a draft that saved
    itself would silently overwrite reviewed fields."""
    import lib.research.draft as draft
    import lib.research.persistence as persistence

    def _forbidden(*a, **k):  # pragma: no cover - tripwire
        raise AssertionError('draft module must not write persistence')

    for name in dir(persistence):
        if name.startswith(('persist_', 'save_')):
            monkeypatch.setattr(persistence, name, _forbidden)
    draft.draft_protocol(
        _DIRECTION, 'en', 'idea_1', user_id=TEST_OWNER_USER_ID)


# ── 3. The HTTP boundary, driven through the real app ─────────────────────

@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    import lib.research.persistence as persistence
    from lib.storage import StorageSupervisor

    supervisor = StorageSupervisor(
        project_root=tmp_path / 'sidecar', backend='sqlite', startup_timeout=60)
    supervisor.start()
    monkeypatch.setattr(
        persistence, '_storage', lambda **_kwargs: supervisor.client)
    try:
        yield supervisor
    finally:
        supervisor.stop()


@pytest.fixture()
def client(fresh_db):
    import server
    app = server.app
    app.config['TESTING'] = True
    return app.test_client()


@pytest.fixture()
def seeded(fresh_db):
    from lib.research.persistence import persist_ideate, persist_survey
    persist_survey(
        _DIRECTION, 'en', _ARTIFACTS['survey_md'],
        _ARTIFACTS['open_gaps'], model='m1', user_id=TEST_OWNER_USER_ID)
    persist_ideate(
        _DIRECTION, 'en', {
            'accepted': _ARTIFACTS['accepted'], 'rejected': [],
            'threshold': 4.0, 'gate_reached': 'accepted'},
        model='m1', user_id=TEST_OWNER_USER_ID)


def _new_loop_run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _post_json(client, path, payload):
    resp = _new_loop_run(client.post(path, json=payload))
    return resp.status_code, _new_loop_run(v1_get_json(resp))


def test_draft_protocol_route_serves_a_grounded_draft(client, seeded, monkeypatch):
    """The whole point: the owner picks a candidate, clicks once, and the
    frozen contract arrives pre-filled from evidence already paid for."""
    import lib.research.draft as draft
    monkeypatch.setattr(draft, 'dispatch_stream', _fake_dispatch(_MODEL_REPLY))

    status, body = _post_json(
        client, '/api/v4/research/workspace/draft-protocol',
        {'direction': _DIRECTION, 'lang': 'en', 'idea_id': 'idea_1'})

    assert status == 200, f'draft route failed: {status} {body}'
    payload = body.get('draft') or {}
    assert payload.get('idea_id') == 'idea_1'
    assert payload.get('hypothesis') == _MODEL_REPLY['hypothesis']
    protocol = payload.get('protocol') or {}
    assert protocol.get('falsifier') == _MODEL_REPLY['falsifier']
    assert protocol.get('random_seeds') == [13, 42, 7], (
        'the route must return the protocol normalized through the same '
        'contract the workspace PUT enforces')


def test_draft_protocol_route_validates_required_fields(client):
    status, body = _post_json(
        client, '/api/v4/research/workspace/draft-protocol', {'lang': 'en'})
    assert status == 400, f'missing direction/idea must fail closed: {body}'


def test_draft_protocol_route_rejects_unknown_idea(client, seeded):
    status, body = _post_json(
        client, '/api/v4/research/workspace/draft-protocol',
        {'direction': _DIRECTION, 'lang': 'en', 'idea_id': 'idea_999'})
    assert status == 400, f'an unknown idea must not reach the model: {body}'


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-x', '-q', '-m', 'unit']))


def test_draft_context_exposes_canonical_numeric_rule_without_guessing_defaults(patched):
    from lib.research.observation_audit import decision_rule_contract
    draft,calls=patched
    result=draft.draft_protocol(_DIRECTION,'en','idea_1',user_id=TEST_OWNER_USER_ID)
    packet=json.loads(calls[0]['messages'][1]['content'])
    assert packet['scientific_decision_rule_contract']==decision_rule_contract()
    assert 'decision_rule' not in result['protocol']
    assert len(calls)==1


def test_complete_rule_proposal_is_retained_but_not_executed(patched,monkeypatch):
    from unittest.mock import Mock
    draft,calls=patched
    rule={'direction':'higher','minimum_effect':-.02,'uncertainty_multiplier':2}
    dispatch=Mock(side_effect=_fake_dispatch(dict(_MODEL_REPLY,decision_rule=rule)))
    monkeypatch.setattr(draft,'dispatch_stream',dispatch)
    result=draft.draft_protocol(_DIRECTION,'en','idea_1',user_id=TEST_OWNER_USER_ID)
    assert result['protocol']['decision_rule']==rule
    assert dispatch.call_count==1


def test_incomplete_rule_proposal_is_rejected_instead_of_silently_omitted(patched,monkeypatch):
    draft,calls=patched
    monkeypatch.setattr(draft,'dispatch_stream',_fake_dispatch(dict(_MODEL_REPLY,decision_rule={'direction':'higher'})))
    with pytest.raises(ValueError,match='scientific decision rule'):
        draft.draft_protocol(_DIRECTION,'en','idea_1',user_id=TEST_OWNER_USER_ID)


@pytest.mark.parametrize('owner', [12, 23])
def test_draft_keeps_material_and_provider_owner_identical(patched, monkeypatch, owner):
    draft, calls = patched
    material_owners = []
    def load(*args, user_id, **kwargs):
        material_owners.append(user_id)
        return dict(_ARTIFACTS)
    monkeypatch.setattr(draft, 'load_research_artifacts', load)
    draft.draft_protocol(_DIRECTION, 'en', 'idea_1', user_id=owner)
    assert material_owners == [owner]
    assert len(calls) == 1
    assert calls[0]['kwargs']['user_id'] == owner


@pytest.mark.parametrize('owner', [True, 0, -1, None])
def test_invalid_draft_owner_cannot_read_material_or_select_provider(patched, monkeypatch, owner):
    draft, calls = patched
    monkeypatch.setattr(draft, 'load_research_artifacts',
                        lambda *args, **kwargs: pytest.fail('must not read material'))
    with pytest.raises((ValueError, TypeError)):
        draft.draft_protocol(_DIRECTION, 'en', 'idea_1', user_id=owner)
    assert calls == []


def test_draft_reads_original_task_instead_of_latest_direction(patched,monkeypatch):
    draft, calls = patched
    from lib.research import records
    source = {'task_id':'original-task','direction':_DIRECTION,'lang':'en','status':'done',
              'result':{**_ARTIFACTS, 'survey_md':'Original retained survey ONLY'}}
    reads = []
    def load(task_id,*,user_id,internal):
        reads.append((task_id,user_id,internal))
        return source
    monkeypatch.setattr(records,'load_record',load)
    monkeypatch.setattr(draft,'load_research_artifacts',lambda *a,**k:pytest.fail('must not use latest direction'))
    result = draft.draft_protocol(_DIRECTION,'en','idea_1',user_id=12,source_task_id='original-task')
    assert reads == [('original-task',12,True)]
    assert result['source_task_id'] == 'original-task'
    assert 'Original retained survey ONLY' in json.dumps(calls[0]['messages'])


@pytest.mark.parametrize('source',[None, {'status':'running'},
    {'task_id':'different-task','status':'done','direction':_DIRECTION,'lang':'en','result':_ARTIFACTS}, {'status':'done','direction':'foreign'},
    {'status':'done','direction':_DIRECTION,'lang':'zh'},
    {'status':'done','direction':_DIRECTION,'lang':'en','result':{'accepted':[]}}])
def test_invalid_original_source_cannot_fall_back_or_spend(patched,monkeypatch,source):
    draft, calls = patched
    from lib.research import records
    monkeypatch.setattr(records,'load_record',lambda *a,**k:source)
    monkeypatch.setattr(draft,'load_research_artifacts',lambda *a,**k:pytest.fail('no fallback'))
    with pytest.raises(ValueError):
        draft.draft_protocol(_DIRECTION,'en','idea_1',user_id=12,source_task_id='original-task')
    assert calls == []


def test_draft_route_keeps_explicit_original_source(client, monkeypatch):
    from lib.research import draft, records
    calls = []
    def load(task_id, *, user_id, internal):
        calls.append((task_id,user_id,internal))
        return {'task_id':task_id,'direction':_DIRECTION,'lang':'en','status':'done','result':_ARTIFACTS}
    monkeypatch.setattr(records,'load_record',load)
    monkeypatch.setattr(draft,'dispatch_stream',_fake_dispatch(_MODEL_REPLY))
    monkeypatch.setattr(draft,'load_research_artifacts',lambda *a,**k:pytest.fail('no latest-direction fallback'))
    status, body = _post_json(client,'/api/v4/research/workspace/draft-protocol',
        {'direction':_DIRECTION,'lang':'en','idea_id':'idea_1','source_task_id':'original-source'})
    assert status == 200
    assert body['draft']['source_task_id'] == 'original-source'
    assert calls == [('original-source',TEST_OWNER_USER_ID,True)]


@pytest.mark.parametrize('source',[None,True,0,''])
def test_draft_route_refuses_explicit_invalid_source_before_spend(client,monkeypatch,source):
    from lib.research import draft
    monkeypatch.setattr(draft,'dispatch_stream',lambda *a,**k:pytest.fail('must not dispatch'))
    status, _ = _post_json(client,'/api/v4/research/workspace/draft-protocol',
        {'direction':_DIRECTION,'idea_id':'idea_1','source_task_id':source})
    assert status == 400


def test_saved_original_source_remains_owner_scoped(fresh_db,monkeypatch):
    from lib.research import draft, records
    records.persist_record({'task_id':'owner12-original','user_id':12,'direction':_DIRECTION,
        'lang':'en','status':'done','result':_ARTIFACTS})
    calls = []
    def dispatch(messages,**kwargs):
        calls.append(kwargs['user_id'])
        return _fake_dispatch(_MODEL_REPLY)(messages,**kwargs)
    monkeypatch.setattr(draft,'dispatch_stream',dispatch)
    with pytest.raises(ValueError,match='source is unavailable'):
        draft.draft_protocol(_DIRECTION,'en','idea_1',user_id=13,source_task_id='owner12-original')
    assert calls == []
    output = draft.draft_protocol(_DIRECTION,'en','idea_1',user_id=12,source_task_id='owner12-original')
    assert output['source_task_id'] == 'owner12-original' and calls == [12]
