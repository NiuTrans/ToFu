"""A UI must be able to START a production job — not just watch one.

Context (epic pt_493b861038fb4040). ``produce_research`` and ``produce_report``
existed ONLY as LLM tools. That makes them unreachable from a button for two
independent reasons, either of which alone is disqualifying:

  1. Starting depends on the model CHOOSING to call the tool. A UI control
     whose effect is "maybe, if the model feels like it" is not a control.
  2. ``lib/tools/registry/_build.py::_build_produce`` is SEARCH-GATED — with
     web search off, all three produce tools vanish from the toolset. So the
     button's availability would silently track an unrelated setting.

Poll/abort already ride the generic ``/api/v1/tasks/*`` surface. START was the
missing verb, and it must be just as capability-agnostic: one route driven by
the same kind→runtime registry, so the NEXT capability inherits it for free
(charter: "衡量扩展性看它让下一个新能力少写多少代码").

These are BEHAVIOUR guards — they assert what the API DOES (a job appears and
is pollable), never that some constant equals some value.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = pytest.mark.unit


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _client():
    """A Quart test client for the tasks blueprint with an admin auth context."""
    from quart import Quart, g

    from lib.api_keys import local_admin_context
    from routes.api_v1.tasks import api_v1_tasks_bp

    app = Quart(__name__)
    app.config['TESTING'] = True

    @app.before_request
    async def _grant():
        g.auth_ctx = local_admin_context()
        g.rate_decision = None

    app.register_blueprint(api_v1_tasks_bp)
    return app


# ── The registry of startable capabilities ────────────────────────

def test_start_registry_is_capability_agnostic():
    """Both production capabilities are startable through ONE table.

    The point of the epic: research and longform must not each need their own
    route. Asserting the registry (not a hand-listed route set) is what makes
    the next capability free.
    """
    from routes.api_v1.tasks import _starters

    reg = _starters()
    assert 'research' in reg, (
        'research is not startable over HTTP — the only way to launch it '
        'would still be hoping the model calls produce_research')
    assert 'longform-report' in reg, (
        'longform has the same shape and must ride the same route')
    for kind, spec in reg.items():
        assert callable(spec['start']), f'{kind}: start must be callable'
        assert isinstance(spec['input'], str) and spec['input'], (
            f'{kind}: must name its primary input field so the route can stay '
            'capability-agnostic instead of hardcoding "direction"')


def test_kinds_are_startable_and_pollable_under_the_same_name():
    """A kind you can start is a kind you can poll — same identifier.

    Guards the silent-desync failure: if start used 'research' while the
    runtime registered 'research-job', the UI would start a job it could
    never find again.
    """
    from routes.api_v1.tasks import _registries, _starters

    pollable = set(_registries())
    for kind in _starters():
        assert kind in pollable, (
            f'{kind!r} is startable but not pollable — a UI could launch a job '
            f'and then never locate it. Pollable kinds: {sorted(pollable)}')


# ── Starting a job over HTTP, with no LLM and no search setting ───

def test_research_job_starts_over_http_without_the_llm_tool_path(monkeypatch):
    """★ The epic's headline: a direction in, a live task id out.

    Deliberately does NOT touch the toolset, the model, or the search mode —
    that independence IS the requirement. The engine's spawn is stubbed so the
    test stays offline; what is asserted is that the ROUTE reaches it with the
    user's input intact.
    """
    import routes.api_v1.tasks as tasks_mod

    seen = {}

    def _fake_start(direction, **kw):
        seen['direction'] = direction
        seen['kw'] = kw
        return {'task_id': 'research_fake123', 'deduped': False}

    monkeypatch.setitem(
        tasks_mod._STARTERS, 'research',
        {'start': _fake_start, 'input': 'direction',
         'params': ('lang', 'n_ideas', 'seed_arxiv_ids', 'model')})

    app = _client()

    async def go():
        r = await app.test_client().post(
            '/api/v1/tasks/start',
            json={'kind': 'research',
                  'direction': 'long-context KV cache compression',
                  'lang': 'en', 'n_ideas': 4, 'model': 'kimi-k3'})
        return r.status_code, await r.get_json()

    code, body = _run(go())
    assert code == 200, f'start failed: {body}'
    assert body['taskId'] == 'research_fake123'
    assert body['deduped'] is False
    assert seen['direction'] == 'long-context KV cache compression', (
        "the route dropped or mangled the user's direction")
    assert seen['kw'].get('lang') == 'en'
    assert seen['kw'].get('n_ideas') == 4
    assert seen['kw'].get('model') == 'kimi-k3'


def test_start_route_projects_capability_validation_as_bad_request(monkeypatch):
    """A bounded capability rejection is a caller error, never an opaque 500.

    The stand-in starter runs the research request validation every research
    entry shares, so the route meets the capability's real rejection.
    """
    import routes.api_v1.tasks as tasks_mod
    from lib.research.contracts import (MAX_RESEARCH_SEED_PAPERS,
                                        normalize_research_request)

    def reject(direction, **kwargs):
        normalize_research_request(
            direction, seed_arxiv_ids=kwargs['seed_arxiv_ids'])
        raise AssertionError('an over-limit seed list reached job creation')

    monkeypatch.setitem(
        tasks_mod._STARTERS, 'research',
        {'start': reject, 'input': 'direction',
         'params': ('lang', 'n_ideas', 'seed_arxiv_ids')})
    app = _client()

    async def go():
        response = await app.test_client().post(
            '/api/v1/tasks/start',
            json={'kind': 'research', 'direction': 'bounded',
                  'seed_arxiv_ids': [f'2501.{i:05d}' for i in
                                     range(MAX_RESEARCH_SEED_PAPERS + 1)]})
        return response.status_code, await response.get_json()

    code, body = _run(go())
    assert code == 400
    assert f'at most {MAX_RESEARCH_SEED_PAPERS} papers' in str(body)


def test_longform_starts_through_the_same_route(monkeypatch):
    """Same route, different capability, zero research-specific parameters.

    NEUTER: hardcode 'direction' in the route and this reds while the research
    test above stays green — proving the route is generic, not research-shaped.
    """
    import routes.api_v1.tasks as tasks_mod

    seen = {}

    def _fake_start(topic, **kw):
        seen['topic'] = topic
        seen['kw'] = kw
        return {'task_id': 'longform_fake456', 'deduped': False}

    monkeypatch.setitem(
        tasks_mod._STARTERS, 'longform-report',
        {'start': _fake_start, 'input': 'topic', 'params': ('lang', 'depth')})

    app = _client()

    async def go():
        r = await app.test_client().post(
            '/api/v1/tasks/start',
            json={'kind': 'longform-report', 'topic': '可控核聚变',
                  'lang': 'zh', 'depth': 'brief'})
        return r.status_code, await r.get_json()

    code, body = _run(go())
    assert code == 200, f'start failed: {body}'
    assert body['taskId'] == 'longform_fake456'
    assert seen['topic'] == '可控核聚变'
    assert seen['kw'].get('depth') == 'brief'


def test_unknown_kind_is_rejected_not_crashed():
    app = _client()

    async def go():
        r = await app.test_client().post(
            '/api/v1/tasks/start', json={'kind': 'no-such-kind', 'direction': 'x'})
        return r.status_code, await r.get_json()

    code, body = _run(go())
    assert code == 400, f'expected a clean 400, got {code}: {body}'
    assert 'no-such-kind' in str(body), 'the error must name the bad kind'


def test_missing_input_is_rejected_with_the_field_name():
    """An empty direction must fail loudly at the boundary, not spawn a job."""
    app = _client()

    async def go():
        r = await app.test_client().post(
            '/api/v1/tasks/start', json={'kind': 'research', 'direction': '  '})
        return r.status_code, await r.get_json()

    code, body = _run(go())
    assert code == 400, f'expected 400 for a blank input, got {code}: {body}'
    assert 'direction' in str(body), (
        "the 400 must name the missing field so the UI can point at the input")


def test_start_does_not_depend_on_the_search_gated_tool_registry():
    """The route must not consult the LLM toolset to decide it can start.

    `_build_produce` returns [] when web search is off. If the route asked the
    tool registry, the button would die whenever a user toggled search — an
    unrelated setting silently disabling a UI control.
    """
    import inspect

    import routes.api_v1.tasks as tasks_mod

    src = inspect.getsource(tasks_mod.start_task)
    for forbidden in ('_build_produce', 'build_tools', 'search_mode',
                      'search_enabled', 'PRODUCE_RESEARCH_TOOL'):
        assert forbidden not in src, (
            f'the start route references {forbidden!r} — starting a job must '
            'not depend on whether the model happens to have the tool this turn')


@pytest.mark.parametrize('budget,expected_code', [({'dispatch_budget': 30}, 200),
                                                ({'dispatch_budget': True}, 400)])
def test_research_action_route_forwards_validated_cumulative_policy(monkeypatch, budget, expected_code):
    import routes.api_v1.tasks as tasks_mod
    from lib.research.action_budget import normalize_action_logical_budget, action_logical_budget_schema
    from jsonschema import Draft202012Validator
    assert Draft202012Validator(action_logical_budget_schema()).is_valid(budget) is (expected_code == 200)
    spec = tasks_mod._starters()['research-action']
    observed = []
    def start(direction, **kwargs):
        normalized = normalize_action_logical_budget(kwargs['logical_budget'])
        observed.append(normalized)
        return {'task_id':'research_action_budget_http','deduped':False}
    monkeypatch.setitem(tasks_mod._STARTERS, 'research-action', dict(spec, start=start))
    app = _client()
    async def go():
        response = await app.test_client().post('/api/v1/tasks/start', json={
            'kind':'research-action','direction':'Long scientific experiment',
            'action':'experiment','expected_revision':0,'logical_budget':budget})
        return response.status_code, await response.get_json()
    code, body = _run(go())
    assert code == expected_code, body
    assert observed == ([{'token_budget':0,'dispatch_budget':30}] if expected_code == 200 else [])


# ── Every registered starter answers caller faults with a typed 4xx ──
#
# The route maps BadRequest to 400, LifecycleNotFound to 404 and
# LifecycleConflict to 409; anything else is a redacted 500. These cases drive
# the REAL registered starters, so a starter that rejects caller input with a
# builtin exception reds here.

def _start_app():
    """The tasks blueprint on both wire majors: v1 envelope and v4 problem."""
    from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4
    from routes.api_v1.tasks import api_v1_tasks_bp

    app = _client()
    app.register_blueprint(attach_v4_envelope(
        copy_blueprint_to_v4(api_v1_tasks_bp, name='api_v1_tasks_v4')))
    return app


def _start(prefix, body):
    app = _start_app()

    async def go():
        response = await app.test_client().post(f'{prefix}/tasks/start', json=body)
        return response.status_code, await response.get_json(force=True)

    return _run(go())


@pytest.fixture
def job_creation(monkeypatch):
    """Ports a starter reaches only after admitting the request; all refuse."""
    created = []

    def refuse(*args, **kwargs):
        created.append(args[:1])
        raise AssertionError('a rejected start reached job creation')

    for port in ('lib.research.runtime._claim_research_task',
                 'lib.research.action_runtime._production.claim_task',
                 'lib.longform.runtime._claim_longform_task',
                 'lib.slides.runtime._claim_slides_task',
                 'lib.slides.readiness.ensure_slides_runtime_ready'):
        monkeypatch.setattr(port, refuse)
    return created


# (body, offending field) — at least one per registered starter.
_CLIENT_FAULTS = [
    pytest.param({'kind': 'research', 'direction': 'KV cache', 'lang': 'fr'},
                 'lang', id='research-lang'),
    pytest.param({'kind': 'research-action', 'direction': 'KV cache',
                  'action': 'experiment', 'expected_revision': 0},
                 'confirm_external_writes', id='research-action-authority'),
    pytest.param({'kind': 'longform-report', 'topic': 'fusion',
                  'depth': 'exhaustive'}, 'depth', id='longform-depth'),
    pytest.param({'kind': 'longform-report', 'topic': 'fusion',
                  'lang': ['zh']}, 'lang', id='longform-lang'),
    pytest.param({'kind': 'slides-deck', 'topic': 'fusion',
                  'creative_mode': 'chaotic'}, 'creative_mode',
                 id='slides-creative-mode'),
    pytest.param({'kind': 'slides-deck', 'topic': 'fusion',
                  'size': '4000x3000'}, 'size', id='slides-size'),
    pytest.param({'kind': 'slides-deck', 'topic': 'fusion',
                  'style': 'x' * 2001}, 'style', id='slides-style'),
    pytest.param({'kind': 'slides-deck', 'topic': 'fusion',
                  'lang': {'zh': True}}, 'lang', id='slides-lang'),
]


def test_every_registered_starter_has_a_client_fault_case():
    """A new starter in the dispatch table must join the cases below."""
    from routes.api_v1.tasks import _starters

    assert {case.values[0]['kind'] for case in _CLIENT_FAULTS} == set(_starters())


@pytest.mark.parametrize('prefix', ['/api/v1', '/api/v4'])
@pytest.mark.parametrize('body,field', _CLIENT_FAULTS)
def test_starter_client_fault_is_a_bad_request_naming_the_field(
        prefix, body, field, job_creation):
    status, payload = _start(prefix, body)

    assert status == 400, payload
    if prefix == '/api/v1':
        assert payload['ok'] is False
        assert payload['field'] == field
        assert field in payload['error']
    else:
        assert (payload['status'], payload['code']) == (400, 'bad_request')
        assert field in payload['detail']
    assert job_creation == []


@pytest.mark.parametrize('prefix', ['/api/v1', '/api/v4'])
def test_stale_research_workspace_is_a_typed_conflict(prefix, job_creation, monkeypatch):
    """A valid request against a newer workspace revision is a 409, not a 500."""
    monkeypatch.setattr('lib.research.workspace.load_workspace',
                        lambda direction, lang, *, user_id: {'revision': 4})

    status, payload = _start(prefix, {
        'kind': 'research-action', 'direction': 'KV cache',
        'action': 'manuscript', 'expected_revision': 3})

    assert status == 409, payload
    if prefix == '/api/v1':
        assert payload['ok'] is False
        assert payload['error']['kind'] == 'research_workspace_stale'
        assert 'revision 4' in payload['error']['message']
    else:
        assert (payload['status'], payload['code']) == (409, 'research_workspace_stale')
        assert 'revision 4' in payload['detail']
    assert job_creation == []


@pytest.mark.parametrize('prefix', ['/api/v1', '/api/v4'])
def test_missing_research_source_is_a_typed_not_found(prefix, job_creation, monkeypatch):
    """An experiment whose selected idea source no longer exists is a 404, not a 500."""
    monkeypatch.setattr('lib.research.workspace.load_workspace',
                        lambda direction, lang, *, user_id: {
                            'revision': 3, 'direction': direction, 'lang': lang,
                            'selected_idea_id': 'idea-1',
                            'selected_idea_source_task_id': 'deleted-source'})
    monkeypatch.setattr('lib.research.records.load_record',
                        lambda task_id, *, user_id, internal=False: None)

    status, payload = _start(prefix, {
        'kind': 'research-action', 'direction': 'KV cache',
        'action': 'experiment', 'expected_revision': 3,
        'confirm_external_writes': True})

    assert status == 404, payload
    if prefix == '/api/v1':
        assert payload['ok'] is False
        assert 'research source not found' in payload['error']
    else:
        assert (payload['status'], payload['code']) == (404, 'not_found')
        assert 'research source not found' in payload['detail']
    assert job_creation == []


def test_starter_server_fault_stays_a_redacted_500(job_creation, monkeypatch):
    """A builtin exception inside a starter is never reported as a caller fault."""
    def corrupt(*args, **kwargs):
        raise ValueError('stored workspace row is corrupt: secret-detail')

    monkeypatch.setattr('lib.research.workspace.load_workspace', corrupt)

    status, payload = _start('/api/v1', {
        'kind': 'research-action', 'direction': 'KV cache',
        'action': 'manuscript', 'expected_revision': 3})

    assert status == 500, payload
    assert 'secret-detail' not in json.dumps(payload)
    assert job_creation == []
