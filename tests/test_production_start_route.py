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
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
         'params': ('lang', 'n_ideas', 'seed_arxiv_ids')})

    app = _client()

    async def go():
        r = await app.test_client().post(
            '/api/v1/tasks/start',
            json={'kind': 'research',
                  'direction': 'long-context KV cache compression',
                  'lang': 'en', 'n_ideas': 4})
        return r.status_code, await r.get_json()

    code, body = _run(go())
    assert code == 200, f'start failed: {body}'
    assert body['taskId'] == 'research_fake123'
    assert body['deduped'] is False
    assert seen['direction'] == 'long-context KV cache compression', (
        "the route dropped or mangled the user's direction")
    assert seen['kw'].get('lang') == 'en'
    assert seen['kw'].get('n_ideas') == 4


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
