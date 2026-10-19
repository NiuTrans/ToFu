"""Provider-owned catalog feeds never imply another API supplies a model.

Covers exact wire identity, cache migration, bounded refresh/failure/backoff,
endpoint overrides, stale expiry, and non-destructive template projection.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import json
import time

import pytest

import lib.provider.provider_template_feed as feed

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_feed(monkeypatch, tmp_path):
    monkeypatch.setenv('TOFU_TEMPLATE_FEED', '1')
    monkeypatch.setattr(feed, 'PUBLIC_CATALOGS', {'openrouter': feed.PUBLIC_CATALOGS['openrouter']})
    monkeypatch.setattr(feed, '_CACHE_PATH', str(tmp_path / 'feed.json'))
    monkeypatch.setattr(feed, 'ensure_config_dir', lambda: None)
    monkeypatch.setattr(feed, '_last_attempt', 0.0)
    monkeypatch.setattr(feed, '_refresh_scheduled', False)


def row(model_id='deepseek/deepseek-v4-flash', **overrides):
    return {'id': model_id, 'context_length': 262144,
            'architecture': {'input_modalities': ['text'], 'output_modalities': ['text']},
            'supported_parameters': ['reasoning'],
            'pricing': {'prompt': '0.0000002', 'completion': '0.0000008'}, **overrides}


class Response:
    def __init__(self, rows=None, *, status=200, raw=None):
        self.status_code = status
        self.raw = raw if raw is not None else json.dumps({'data': rows}).encode()
        self.closed = False

    async def aiter_bytes(self, chunk_size):
        yield self.raw

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True


def template(key='openrouter'):
    return {'key': key, 'category': 'relay',
            'base_url': feed.PUBLIC_CATALOGS['openrouter']['base_url'],
            'offering_recipes': [{'model_id': 'old-model'}, {'model_id': row()['id'], 'rpm': 10}]}


def cache(rows=None, *, fetched_at=None):
    return {'version': 2, 'sources': {'openrouter': {
        'url': feed.PUBLIC_CATALOGS['openrouter']['url'],
        'fetched_at': fetched_at if fetched_at is not None else time.time(),
    }}, 'templates': {'openrouter': rows or feed.recipes_from_catalog([row()])['openrouter']}}


def store(value):
    with open(feed._CACHE_PATH, 'w') as handle:
        json.dump(value, handle)


def test_exact_names_are_only_added_to_the_source_provider():
    projected = feed.recipes_from_catalog([row(), row('openai/gpt-x-2026-09-01')])
    assert set(projected) == {'openrouter'}
    assert {r['model_id'] for r in projected['openrouter']} == {
        'deepseek/deepseek-v4-flash', 'openai/gpt-x-2026-09-01'}
    item = projected['openrouter'][0]
    assert item['context_window'] == 262144
    assert item['pricing']['input'] == 0.2
    assert item['pricing']['output'] == 0.8
    assert 'thinking' in item['capabilities']
    assert feed.recipes_from_catalog([row()], template_key='deepseek') == {}


@pytest.mark.parametrize('value', ['nan', 'inf', '-1', True, None])
def test_invalid_prices_never_become_billable(value):
    projected = feed.recipes_from_catalog([row(pricing={'prompt': value, 'completion': '0.1'})])
    assert 'pricing' not in projected['openrouter'][0]


def test_billing_redirect_and_non_chat_rows_are_excluded():
    projected = feed.recipes_from_catalog([
        row('openai/gpt-x:free'), row('~alias'), row('bad\nid'),
        row('embed', architecture={'output_modalities': ['embeddings']}), row(),
    ])
    assert len(projected['openrouter']) == 1


def test_billing_channels_cannot_break_full_template_compilation(monkeypatch):
    import lib.provider.provider_template_recipes as recipes
    standard = 'meta/muse-spark-test-1.3'
    projected = feed.recipes_from_catalog([
        row(standard + '-contributor', created=3),
        row(standard, created=2, pricing={'prompt': '.000002', 'completion': '.000007'}),
        row(standard + '-preview', created=1),
        row(standard + '-20260901', created=1),
    ])['openrouter']
    assert {r['model_id'] for r in projected} == {
        standard, standard + '-preview', standard + '-20260901'}
    template_row = recipes.normalize_provider_template({
        **template(), 'name': 'Relay', 'offering_recipes': projected})
    monkeypatch.setattr(recipes, 'load_provider_templates', lambda: [template_row])
    bundle = recipes.compile_provider_template_bundle('openrouter')
    assert len(bundle['offerings']) == len(bundle['deployments']) == 3
    assert {r['wire_model_id'] for r in bundle['deployments']} == {r['model_id'] for r in projected}
    standard_offering = next(r for r in bundle['offerings'] if r['model']['model_id'] == 'muse-spark-test-1.3')
    assert standard_offering['actual_pricing']['input'] == 2
    assert standard_offering['actual_pricing']['output'] == 7
    assert all('list_pricing' not in r for r in bundle['models'])


def test_existing_cache_deduplicates_logical_billing_channels():
    store(cache([{'model_id': 'meta/muse-spark-test-contributor'},
                 {'model_id': 'meta/muse-spark-test'}]))
    assert [r['model_id'] for r in feed.load_cached_feed()['templates']['openrouter']] == ['meta/muse-spark-test']


def test_row_budget_favors_recent_models():
    rows = [row(f'provider/m{i}', created=i + 1) for i in range(500)]
    projected = feed.recipes_from_catalog(rows)['openrouter']
    assert len(projected) == feed._MAX_FEED_ROWS_PER_TEMPLATE
    assert projected[0]['model_id'] == 'provider/m499'


def test_cache_version_one_cross_provider_data_is_not_reused():
    store({'version': 1, 'templates': {'deepseek': [{'model_id': 'invented'}]}})
    assert feed.load_cached_feed() is None


def test_cache_revalidates_rows_and_source_identity():
    raw = cache([{'model_id': 'valid'}, {'model_id': ''}, 'garbage'])
    store(raw)
    assert [r['model_id'] for r in feed.load_cached_feed()['templates']['openrouter']] == ['valid']
    raw['sources']['openrouter']['url'] = 'https://untrusted.example/models'
    store(raw)
    assert feed.load_cached_feed() is None


def test_refresh_fetches_once_with_bounds_and_closes_response():
    response = Response([row()])
    calls = []
    def get(method, url, **kw):
        assert method == 'GET'
        calls.append((url, kw))
        return response
    stats = feed.refresh_now(get)
    assert stats['ok'] and stats['templates'] == 1 and stats['models'] == 1
    assert len(calls) == 1
    assert calls[0][1]['follow_redirects'] is False
    assert calls[0][1]['timeout'] == 8
    assert response.closed
    assert feed.load_cached_feed()['templates']['openrouter'][0]['model_id'] == row()['id']


@pytest.mark.parametrize('response', [Response(status=503), Response(raw=b'not json'),
                                    Response([]), Response(raw=b'x' * (feed._MAX_BYTES + 1))])
def test_failures_preserve_last_good_cache(response):
    store(cache())
    before = feed.load_cached_feed()
    assert not feed.refresh_now(lambda *a, **kw: response)['ok']
    assert feed.load_cached_feed() == before
    assert response.closed


def test_partial_source_failure_isolated(monkeypatch):
    monkeypatch.setattr(feed, 'PUBLIC_CATALOGS', {
        **feed.PUBLIC_CATALOGS, 'other': {'url': 'https://example.test/models',
                                         'base_url': 'https://example.test/v1'},
    })
    def get(method, url, **kw):
        if 'example.test' in url:
            raise ConnectionError('down')
        return Response([row()])
    stats = feed.refresh_now(get)
    assert stats['sources'] == {'openrouter': 'ok', 'other': 'unavailable'}
    assert stats['ok'] is False
    assert feed.load_cached_feed()['templates']['openrouter']


def test_refresh_is_single_flight_and_opt_out_skips_network(monkeypatch):
    feed._refresh_lock.acquire()
    try:
        assert feed.refresh_now()['status'] == 'in_progress'
    finally:
        feed._refresh_lock.release()
    monkeypatch.setenv('TOFU_TEMPLATE_FEED', '0')
    assert feed.refresh_now(lambda *a, **kw: pytest.fail('network')) == {'ok': False, 'disabled': True}


def test_background_one_flight_failure_backoff_and_fresh_cache(monkeypatch):
    spawned = []
    now = time.time()
    assert feed.maybe_refresh_async(None, now=now, _spawn=spawned.append)
    assert not feed.maybe_refresh_async(None, now=now, _spawn=spawned.append)
    monkeypatch.setattr(feed, 'refresh_now', lambda: {'ok': False})
    spawned[0]()
    assert not feed.maybe_refresh_async(None, now=now + 1, _spawn=spawned.append)
    assert not feed.maybe_refresh_async(cache(fetched_at=now), now=now, _spawn=spawned.append)
    assert feed.maybe_refresh_async(None, now=now + 301, _spawn=spawned.append)
    spawned[-1]()


def test_template_overlay_removes_absent_seeds_but_never_changes_other_provider():
    original = template()
    official = template('deepseek')
    before = json.dumps(original, sort_keys=True)
    updated, unchanged = feed.merge_feed_recipes([original, official], cache())
    assert [r['model_id'] for r in updated['offering_recipes']] == [row()['id']]
    assert updated['offering_recipes'][0]['rpm'] == 10
    assert updated['catalog_feed']['stale'] is False
    assert json.dumps(original, sort_keys=True) == before
    assert unchanged is official


def test_deployment_override_is_authoritative_even_at_the_public_endpoint():
    original = template()
    before = json.dumps(original, sort_keys=True)
    merged = feed.merge_feed_recipes([original], cache(), excluded_keys={'openrouter'})
    assert merged[0] is original
    assert json.dumps(original, sort_keys=True) == before


def test_wrong_endpoint_override_and_expired_cache_do_not_merge():
    original = template()
    original['base_url'] = 'https://private-gateway.example/v1'
    assert feed.merge_feed_recipes([original], cache()) == [original]
    original = template()
    expired = cache(fetched_at=time.time() - 8 * 86400)
    assert feed.merge_feed_recipes([original], expired) == [original]
    assert feed.merge_feed_recipes([original], cache(fetched_at=time.time() + 3600)) == [original]


def test_retired_and_disabled_templates_never_gain_rows(monkeypatch):
    original = template()
    original['retired_model_ids'] = [row()['id']]
    assert feed.merge_feed_recipes([original], cache())[0]['offering_recipes'] == []
    monkeypatch.setenv('TOFU_TEMPLATE_FEED', '0')
    assert feed.merge_feed_recipes([original], cache()) == [original]


def test_live_wire_pool_and_absent_price_context_never_fall_back_to_old_seeds():
    original = template()
    original['offering_recipes'][1].update({
        'request_ids': ['removed-wire'], 'aliases': ['old-alias'],
        'pricing': {'input': 9, 'output': 10}, 'context_window': 500000,
    })
    unknown = feed.recipes_from_catalog([row(context_length=None,
        pricing={'prompt': '0', 'completion': '0'})])['openrouter']
    refreshed = feed.merge_feed_recipes([original], cache(unknown))[0]['offering_recipes'][0]
    assert refreshed['request_ids'] == [row()['id']]
    assert not {'aliases', 'pricing', 'context_window'} & refreshed.keys()
    assert refreshed['rpm'] == 10


def test_slow_trickle_cannot_hold_the_refresh_deadline():
    closed = []
    class Trickle:
        status_code = 200
        async def aiter_bytes(self, chunk_size):
            while True:
                await asyncio.sleep(.005)
                yield b' '
    @asynccontextmanager
    async def stream(*args, **kwargs):
        try:
            yield Trickle()
        finally:
            closed.append(True)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        asyncio.run(feed._fetch_rows(stream, feed.PUBLIC_CATALOGS['openrouter'], started + .04))
    assert time.monotonic() - started < .5
    assert closed == [True]


def test_provider_dialects_use_declared_metadata_and_exclude_non_chat(monkeypatch):
    monkeypatch.setattr(feed, 'PUBLIC_CATALOGS', {
        key: {} for key in ('cerebras', 'sambanova', 'ovhcloud')})
    cerebras = feed.recipes_from_catalog([{
        'id': 'gemma-4-31b', 'capabilities': {'vision': True, 'reasoning': True},
        'limits': {'max_context_length': 131072},
        'pricing': {'prompt': '0.00000099', 'completion': '0.00000149'},
    }, {'id': 'old', 'deprecated': True}], template_key='cerebras')['cerebras']
    assert len(cerebras) == 1
    assert set(cerebras[0]['capabilities']) == {'text', 'vision', 'thinking'}
    assert cerebras[0]['context_window'] == 131072
    assert cerebras[0]['pricing']['input'] == .99
    models = [row('embed', max_completion_tokens=0), row('audio', context_length=0),
              row('guard', owned_by='qwen3guard', max_completion_tokens=100),
              row('chat', max_completion_tokens=100,
                  pricing={'prompt': '0.01', 'completion': '0.01', 'currency_unit': 'EUR'})]
    projected = feed.recipes_from_catalog(models, template_key='ovhcloud')['ovhcloud']
    assert [r['model_id'] for r in projected] == ['chat']
    assert 'pricing' not in projected[0]


def test_loader_uses_local_feed_without_fetch_in_request(monkeypatch):
    from lib.provider.provider_template_recipes import load_provider_templates
    store(cache())
    scheduled = []
    monkeypatch.setattr(feed, 'maybe_refresh_async', lambda value: scheduled.append(value))
    rows = {r['key']: r for r in load_provider_templates()}
    assert rows['openrouter']['offering_recipes'][0]['model_id'] == row()['id']
    assert all(r['model_id'] != row()['id'] for r in rows['deepseek']['offering_recipes'])
    assert scheduled
