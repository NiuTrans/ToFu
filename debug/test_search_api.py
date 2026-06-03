"""Smoke test for /api/v1/agents/search wiring.

Imports the agents blueprint, builds a mock app, exercises the route
parser branches WITHOUT hitting real engines. The actual
``perform_web_search`` call is monkey-patched so the test runs offline.

Run: python3 debug/test_search_api.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    # Install Flask→Quart shim BEFORE importing routes (mirrors server.py).
    import quart
    sys.modules['flask'] = quart

    from quart import Quart

    from routes.api_v1 import agents as agents_mod
    from routes.api_v1.auth import require_scope  # noqa: F401 — sanity import

    # Stub out the orchestrator so the test never hits the network.
    def fake_perform_web_search(query, max_results=None, user_question='',
                                freshness='', *, fetch_pages=True,
                                filter_pages=True, rerank=True,
                                engines=None, max_chars_per_page=None):
        from lib.search.orchestrator import SearchResultList
        results = SearchResultList([
            {'title': 'Hello', 'url': 'https://example.com/a',
             'source': 'DDG-HTML', 'snippet': 'snippet a',
             'full_content': ('xxx ' * 200) if fetch_pages else ''},
            {'title': 'World', 'url': 'https://example.com/b',
             'source': 'Brave', 'snippet': 'snippet b',
             'full_content': ('yyy ' * 200) if fetch_pages else ''},
        ])
        results._engine_breakdown = {
            'DDG-HTML': [{'url': 'https://example.com/a', 'title': 'Hello'}],
            'Brave':    [{'url': 'https://example.com/b', 'title': 'World'}],
        }
        results._search_diag = None
        return results

    # Patch both the import in lib.search and the local import in agents.
    import lib.search as ls
    ls.perform_web_search = fake_perform_web_search
    sys.modules['lib.search'].perform_web_search = fake_perform_web_search

    app = Quart(__name__)

    # Bypass auth: install a permissive before_request that grants admin.
    @app.before_request
    async def _grant_admin():
        from quart import g
        from lib.api_keys import AuthContext
        g.auth_ctx = AuthContext(
            key_id='test', name='test',
            scopes=frozenset({'admin'}), via_tunnel_token=False,
        )
        g.rate_decision = None

    app.register_blueprint(agents_mod.api_v1_agents_bp)

    client = app.test_client()

    import asyncio

    async def run():
        # 1. Missing query → 400
        resp = await client.post('/api/v1/agents/search', json={})
        assert resp.status_code == 400, f'expected 400, got {resp.status_code}'
        body = await resp.get_json()
        assert body and body.get('ok') is False
        print(f'[1] missing query → 400 OK ({body.get("error")!r})')

        # 2. Sync, default toggles (full pipeline, mocked)
        resp = await client.post('/api/v1/agents/search',
                                  json={'query': 'tofu architecture'})
        assert resp.status_code == 200, f'expected 200, got {resp.status_code}'
        body = await resp.get_json()
        assert body['ok'] is True
        assert body['count'] == 2
        assert body['query'] == 'tofu architecture'
        assert body['results'][0]['fetch_failed'] is False
        assert 'pipeline' in body and 'engines' in body['pipeline']
        print(f"[2] sync default → 200 OK count={body['count']} "
              f"engines={body['pipeline']['engines']}")

        # 3. Sync, fetch_pages=False → fetch_failed=True everywhere
        resp = await client.post('/api/v1/agents/search', json={
            'query': 'cheap mode', 'fetch_pages': False,
            'filter': False, 'rerank': False,
        })
        assert resp.status_code == 200
        body = await resp.get_json()
        assert all(r['fetch_failed'] for r in body['results'])
        print(f"[3] sync cheap-mode → 200 OK results={len(body['results'])}, "
              f"all fetch_failed=True")

        # 4. Engine allowlist
        resp = await client.post('/api/v1/agents/search', json={
            'query': 'one engine', 'engines': ['DDG-HTML'],
        })
        assert resp.status_code == 200
        print('[4] sync engines=DDG-HTML → 200 OK')

        # 5. Bad engine name in array → still validated by orchestrator;
        #    request itself is accepted.
        resp = await client.post('/api/v1/agents/search', json={
            'query': 'unknown', 'engines': ['Yahoo'],
        })
        assert resp.status_code == 200
        print('[5] sync engines=[Yahoo] → 200 OK (orchestrator falls back)')

        # 6. Async start
        resp = await client.post('/api/v1/agents/search/async',
                                  json={'query': 'async test'})
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body['ok'] is True
        assert 'task_id' in body
        assert body['kind'] == 'search'
        assert body['poll_url'].startswith('/api/v1/tasks/')
        task_id = body['task_id']
        print(f'[6] async start → task_id={task_id[:8]}…')

        # Wait briefly for worker to finish (mock is instant).
        await asyncio.sleep(0.3)
        st = agents_mod._search_runtime.get(task_id)
        assert st is not None
        assert st['status'] in ('done', 'running'), st['status']
        print(f"[7] async runtime task status={st['status']}")

        # Drain events via runtime.poll()
        for _ in range(30):
            poll = agents_mod._search_runtime.poll(task_id, cursor=0)
            if poll['done']:
                break
            await asyncio.sleep(0.1)
        assert poll['done'], 'task did not complete'
        assert poll['status'] == 'done', poll
        assert poll.get('result', {}).get('count') == 2
        print(f"[8] async poll → done count={poll['result']['count']} "
              f"events={len(poll['events'])}")

        # 9. Validation: max_results out of range
        resp = await client.post('/api/v1/agents/search',
                                  json={'query': 'oob', 'max_results': 999})
        assert resp.status_code == 400
        print('[9] max_results=999 → 400 OK')

    asyncio.run(run())
    print('\nALL OK — search v1 routes wired correctly.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
