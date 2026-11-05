"""Search HTTP and detached workers share explicit owner/device binding."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from quart import Quart, g, request

from lib.api_keys import AuthContext
from lib.identity import PrincipalContext

pytestmark = pytest.mark.api


@pytest.fixture
def search_application(monkeypatch):
    from lib.browser import queue
    from lib.search import search_bridge, search_runtime
    from routes.api_v1 import agents

    devices = {
        str(owner): [dict(client_id=f'browser-{owner}', profile=f'Owner {owner}',
                          last_poll=owner, capabilities=[])]
        for owner in (101, 202)
    }
    lookups = []

    def connected_clients(*, owner_user_id):
        lookups.append(str(owner_user_id))
        return devices.get(str(owner_user_id), [])

    monkeypatch.setattr(queue, 'get_connected_clients', connected_clients)
    seen = []
    tasks = []
    workers = []
    events = []
    finishes = []

    def probe(value, **_kwargs):
        provider = search_bridge._ChatuiBrowserProvider().bind()
        site_provider = search_bridge._ChatuiSiteSearchProvider().bind()
        assert (provider.user_id, provider.client_id, provider.profile) == (
            site_provider.user_id, site_provider.client_id, site_provider.profile)
        seen.append((value, provider.user_id, provider.client_id, provider.profile))
        if 'fail' in value:
            raise RuntimeError('search fixture failure')
        return []

    def create(*, user_id, meta):
        task = dict(id=f'search-{len(tasks)}', _userId=user_id, meta=meta)
        tasks.append(task)
        return task

    runtime = SimpleNamespace(
        create=create,
        spawn=lambda task_id, worker: workers.append((task_id, worker)),
        append_event=lambda task_id, event: events.append((task_id, event)),
        finish=lambda task_id, **result: finishes.append((task_id, result)),
    )
    monkeypatch.setattr(agents, '_search_runtime', runtime)
    monkeypatch.setattr(search_runtime, 'ensure_search_runtime', lambda: SimpleNamespace(
        perform_web_search=probe, fetch_page_content=probe))
    app = Quart(__name__, static_folder=None)
    app.config['TESTING'] = True

    @app.before_request
    def authenticate_fixture():
        if owner := request.headers.get('X-Fixture-Owner'):
            owner_id = int(owner)
            scopes = frozenset({'agents:search', 'agents:browser'})
            g.auth_ctx = AuthContext(key_id='fixture', owner_user_id=owner_id, scopes=scopes)
            g.principal_context = PrincipalContext.user(
                subject_id='fixture', owner_user_id=owner_id, scopes=scopes)

    app.register_blueprint(agents.api_v1_agents_bp)
    return SimpleNamespace(app=app, seen=seen, lookups=lookups, tasks=tasks, workers=workers,
                           events=events, finishes=finishes)


@pytest.mark.parametrize('kind', ['sync', 'async', 'browser'])
@pytest.mark.parametrize('fails', [False, True])
def test_search_keeps_owner_after_request_exit_and_cleans_up(search_application, kind, fails):
    state = search_application
    paths = {'sync': '/api/v1/agents/search', 'async': '/api/v1/agents/search/async',
             'browser': '/api/v1/agents/browser/fetch'}
    value = 'fail' if fails else 'query'
    body = {'url': f'https://example.test/{value}'} if kind == 'browser' else {'query': value}

    async def requests():
        client = state.app.test_client()
        for owner in (101, 202):
            response = await client.post(paths[kind], json=body,
                                         headers={'X-Fixture-Owner': str(owner)})
            assert response.status_code == (500 if fails and kind != 'async' else 200)

    asyncio.run(requests())
    if kind == 'async':
        # The callback starts after both HTTP contexts have ended. A fresh
        # thread also excludes incidental context propagation by the test.
        def run_worker(worker):
            from lib.search.search_bridge import _ChatuiBrowserProvider
            worker()
            assert _ChatuiBrowserProvider().bind().user_id == ''

        with ThreadPoolExecutor() as executor:
            futures = [executor.submit(run_worker, worker) for _task_id, worker in state.workers]
            for future in futures:
                future.result()
        assert [task['_userId'] for task in state.tasks] == [101, 202]
        assert len(state.finishes) == 2
        assert all(('error' in result) == fails for _task, result in state.finishes)
        assert sum(event['phase'] == 'finished' for _task, event in state.events) == (0 if fails else 2)

    assert sorted(row[1:] for row in state.seen) == [
        ('101', 'browser-101', 'Owner 101'), ('202', 'browser-202', 'Owner 202')]
    assert sorted(state.lookups) == ['101', '202']
    from lib.search.search_bridge import _ChatuiBrowserProvider
    assert _ChatuiBrowserProvider().bind().user_id == ''


def test_an_http_request_does_not_grant_an_unbound_search_provider(search_application):
    from lib.search.search_bridge import _ChatuiBrowserProvider, _ChatuiSiteSearchProvider

    async def probe():
        async with search_application.app.test_request_context('/'):
            g.auth_ctx = AuthContext(key_id='fixture', owner_user_id=202)
            for factory in (_ChatuiBrowserProvider, _ChatuiSiteSearchProvider):
                provider = factory().bind()
                assert (provider.user_id, provider.client_id) == ('', '')

    asyncio.run(probe())
    assert search_application.lookups == []


def test_search_refuses_an_unauthenticated_request_before_provider_use(search_application):
    async def request_without_owner():
        response = await search_application.app.test_client().post(
            '/api/v1/agents/search', json={'query': 'query'})
        assert response.status_code == 401

    asyncio.run(request_without_owner())
    assert search_application.seen == []
    assert search_application.tasks == []


def test_parallel_binding_restores_each_owners_context_on_failure(search_application):
    from lib.search.search_bridge import _ChatuiBrowserProvider, bind_search_browser

    async def run_owner(owner):
        with bind_search_browser(user_id=owner):
            with pytest.raises(RuntimeError, match='binding fixture failure'):
                with bind_search_browser(user_id=303):
                    await asyncio.sleep(0)
                    assert _ChatuiBrowserProvider().bind().user_id == '303'
                    raise RuntimeError('binding fixture failure')
            provider = _ChatuiBrowserProvider().bind()
            assert (provider.user_id, provider.client_id) == (str(owner), f'browser-{owner}')
        assert _ChatuiBrowserProvider().bind().user_id == ''

    async def parallel_calls():
        await asyncio.gather(run_owner(101), run_owner(202))

    asyncio.run(parallel_calls())
