"""Codex success headers become durable, honestly-labelled quota facts."""

from __future__ import annotations

import pytest

from lib.subscription_quota import (
    _reset_subscription_quota_cache_for_tests,
    latest_subscription_quota,
    parse_codex_quota_headers,
    parse_codex_usage_payload,
    record_codex_quota,
    record_codex_usage_snapshot,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_cache():
    _reset_subscription_quota_cache_for_tests()
    yield
    _reset_subscription_quota_cache_for_tests()


def test_parse_real_codex_header_shape_case_insensitively():
    snapshot = parse_codex_quota_headers({
        'X-Codex-Primary-Used-Percent': '4',
        'x-codex-primary-window-minutes': '10080',
        'X-Codex-Primary-Reset-At': '1704069000',
        'X-Codex-Secondary-Used-Percent': '12.5',
        'x-codex-secondary-window-minutes': '300',
    }, now=1000)

    assert snapshot == {
        'provider': 'codex',
        'source': 'response_headers',
        'captured_at': 1000,
        'primary': {
            'used_percent': 4.0,
            'remaining_percent': 96.0,
            'window_minutes': 10080,
            'resets_at': 1704069000,
        },
        'secondary': {
            'used_percent': 12.5,
            'remaining_percent': 87.5,
            'window_minutes': 300,
        },
    }


def test_non_codex_headers_do_not_manufacture_quota():
    usage = {'prompt_tokens': 10}
    assert parse_codex_quota_headers({'x-ratelimit-remaining': '3'}) is None
    assert record_codex_quota({'x-ratelimit-remaining': '3'}, usage) is usage
    assert '_subscription_quota' not in usage


def test_adjacent_snapshots_record_observed_not_claimed_exact_delta():
    first_usage = {'prompt_tokens': 100}
    record_codex_quota({
        'x-codex-primary-used-percent': '3',
        'x-codex-primary-window-minutes': '10080',
    }, first_usage, now=1000)
    first = first_usage['_subscription_quota']['primary']
    assert 'observed_delta_percent' not in first

    second_usage = {'prompt_tokens': 200}
    record_codex_quota({
        'x-codex-primary-used-percent': '4',
        'x-codex-primary-window-minutes': '10080',
    }, second_usage, now=1010)
    second = second_usage['_subscription_quota']['primary']
    assert second['observed_delta_percent'] == 1.0
    assert second['has_previous_snapshot'] is True

    latest = latest_subscription_quota(now=1025)
    assert latest['primary']['remaining_percent'] == 96.0
    assert latest['age_seconds'] == 15


def test_window_reset_never_becomes_negative_consumption():
    record_codex_quota({
        'x-codex-primary-used-percent': '99',
        'x-codex-primary-window-minutes': '300',
    }, {}, now=1000)
    usage = {}
    record_codex_quota({
        'x-codex-primary-used-percent': '1',
        'x-codex-primary-window-minutes': '300',
    }, usage, now=2000)
    assert 'observed_delta_percent' not in usage['_subscription_quota']['primary']


def test_direct_oauth_and_adapter_snapshots_do_not_cross_accounts():
    record_codex_quota({
        'x-codex-primary-used-percent': '80',
    }, {}, now=1000, cache_key='adapter:agent-1')
    record_codex_quota({
        'x-codex-primary-used-percent': '7',
    }, {}, now=1001, cache_key='oauth_codex')

    direct = latest_subscription_quota(cache_key='oauth_codex', now=1010)
    adapter = latest_subscription_quota(cache_key='adapter:agent-1', now=1010)
    assert direct['primary']['remaining_percent'] == 93.0
    assert adapter['primary']['remaining_percent'] == 20.0


def test_quota_identity_cache_is_bounded_and_long_keys_are_stable():
    from lib.subscription_quota import (
        _MAX_QUOTA_IDENTITIES,
        _latest_snapshots,
    )

    long_key = 'adapter:' + ('x' * 400)
    for index in range(_MAX_QUOTA_IDENTITIES + 5):
        record_codex_quota({
            'x-codex-primary-used-percent': str(index % 100),
        }, {}, now=index, cache_key=f'adapter:{index}')
    record_codex_quota({
        'x-codex-primary-used-percent': '7',
    }, {}, now=999, cache_key=long_key)

    assert len(_latest_snapshots) == _MAX_QUOTA_IDENTITIES
    assert latest_subscription_quota(
        cache_key=long_key, now=1000)['age_seconds'] == 1


def test_header_dimensions_are_bounded():
    snapshot = parse_codex_quota_headers({
        'x-codex-primary-used-percent': '2',
        'x-codex-primary-window-minutes': '1e300',
        'x-codex-primary-resets-at': '1e300',
        'x-codex-plan-type': 'p' * 1000,
    }, now=1)

    assert snapshot['primary'] == {
        'used_percent': 2.0,
        'remaining_percent': 98.0,
    }
    assert len(snapshot['plan_type']) == 64


def test_oauth_status_exposes_only_authenticated_direct_snapshot():
    record_codex_quota({
        'x-codex-primary-used-percent': '9',
        'x-codex-primary-window-minutes': '10080',
    }, {}, now=1000, cache_key='oauth_codex')
    from routes.api_v1.oauth import _with_quota_state

    connected = _with_quota_state({'authenticated': True}, 'codex')
    disconnected = _with_quota_state({'authenticated': False}, 'codex')
    claude = _with_quota_state({'authenticated': True}, 'claude')
    assert connected['quota']['primary']['remaining_percent'] == 91.0
    assert 'quota' not in disconnected
    assert 'quota' not in claude


def test_malformed_values_are_ignored_and_percent_is_clamped():
    assert parse_codex_quota_headers({
        'x-codex-primary-used-percent': 'not-a-number',
    }) is None
    snapshot = parse_codex_quota_headers({
        'x-codex-primary-used-percent': '120',
        'x-codex-primary-window-minutes': '-5',
    }, now=1)
    assert snapshot['primary'] == {
        'used_percent': 100.0,
        'remaining_percent': 0.0,
    }


def test_sync_stream_carries_success_headers_into_round_usage(monkeypatch):
    import lib.llm.stream as stream

    class FakeResponse:
        status_code = 200
        encoding = 'utf-8'
        headers = {
            'x-codex-primary-used-percent': '8',
            'x-codex-primary-window-minutes': '300',
        }

        def iter_lines(self, decode_unicode=True):
            yield ('data: {"choices":[{"delta":{"content":"ok"},'
                   '"finish_reason":"stop"}]}')
            yield 'data: [DONE]'

        def close(self):
            pass

    class FakeSession:
        post = staticmethod(lambda *args, **kwargs: FakeResponse())

    monkeypatch.setattr(stream, 'get_sync_session', lambda: FakeSession())
    monkeypatch.setattr('lib.desktop.egress.route_request',
                        lambda *args, **kwargs: 'direct')
    monkeypatch.setattr('lib.proxy.subscription_route_candidates',
                        lambda *args, **kwargs: [])

    _msg, _finish, usage = stream.stream_chat({
        'model': 'codex-test',
        'messages': [{'role': 'user', 'content': 'hi'}],
        'stream': True,
    }, api_key='token', base_url='https://example.test/v1')

    quota = usage['_subscription_quota']['primary']
    assert quota['used_percent'] == 8.0
    assert quota['remaining_percent'] == 92.0
    assert quota['window_minutes'] == 300


def _usage_payload(**overrides):
    payload = {
        'plan_type': 'plus',
        'rate_limit': {
            'allowed': True,
            'limit_reached': False,
            'primary_window': {
                'used_percent': 12,
                'limit_window_seconds': 18000,
                'reset_after_seconds': 3600,
                'reset_at': 1704069000,
            },
            'secondary_window': {
                'used_percent': 4.5,
                'limit_window_seconds': 604800,
                'reset_after_seconds': 86400,
                'reset_at': 1704155400,
            },
        },
        'rate_limit_reset_credits': {'available_count': 1},
    }
    payload.update(overrides)
    return payload


def test_usage_payload_windows_parse_like_codex_status():
    snapshot = parse_codex_usage_payload(_usage_payload(), now=1000)

    assert snapshot == {
        'provider': 'codex',
        'source': 'usage_api',
        'captured_at': 1000,
        'plan_type': 'plus',
        'primary': {
            'used_percent': 12.0,
            'remaining_percent': 88.0,
            'window_minutes': 300,
            'resets_at': 1704069000,
        },
        'secondary': {
            'used_percent': 4.5,
            'remaining_percent': 95.5,
            'window_minutes': 10080,
            'resets_at': 1704155400,
        },
    }


def test_usage_payload_reset_after_seconds_is_the_reset_at_fallback():
    payload = _usage_payload()
    payload['rate_limit'] = {
        'primary_window': {
            'used_percent': 50,
            'limit_window_seconds': 18000,
            'reset_after_seconds': 900,
        },
    }
    snapshot = parse_codex_usage_payload(payload, now=1000)
    assert snapshot['primary'] == {
        'used_percent': 50.0,
        'remaining_percent': 50.0,
        'window_minutes': 300,
        'resets_at': 1900,
    }
    assert snapshot['secondary'] is None


def test_usage_payload_shape_drift_never_manufactures_quota():
    assert parse_codex_usage_payload(None) is None
    assert parse_codex_usage_payload({}) is None
    assert parse_codex_usage_payload({'rate_limit': None}) is None
    assert parse_codex_usage_payload(
        {'rate_limit': {'primary_window': {'limit_window_seconds': 18000}}}
    ) is None
    assert parse_codex_usage_payload(
        {'rate_limit': {'primary_window': {'used_percent': True}}}
    ) is None
    assert parse_codex_usage_payload(
        {'rate_limit': {'resets_at': 1_900_000_000}}
    ) is None
    snapshot = parse_codex_usage_payload({
        'rate_limit': {
            'primary_window': {
                'used_percent': 250,
                'limit_window_seconds': 10**15,
                'reset_at': 10**15,
            },
        },
    }, now=1)
    assert snapshot['primary'] == {
        'used_percent': 100.0,
        'remaining_percent': 0.0,
    }


def test_usage_snapshot_records_cache_and_cross_source_delta():
    record_codex_quota({
        'x-codex-primary-used-percent': '10',
        'x-codex-primary-window-minutes': '300',
    }, {}, now=1000, cache_key='oauth_codex')

    snapshot = record_codex_usage_snapshot(
        _usage_payload(), now=1010, cache_key='oauth_codex')
    assert snapshot['primary']['observed_delta_percent'] == 2.0
    assert snapshot['primary']['has_previous_snapshot'] is True

    latest = latest_subscription_quota(cache_key='oauth_codex', now=1070)
    assert latest['source'] == 'usage_api'
    assert latest['primary']['remaining_percent'] == 88.0
    assert latest['age_seconds'] == 60


def test_usage_payload_without_windows_keeps_previous_snapshot():
    record_codex_quota({
        'x-codex-primary-used-percent': '10',
    }, {}, now=1000, cache_key='oauth_codex')

    assert record_codex_usage_snapshot(
        {'rate_limit_reset_credits': {'available_count': 0}}, now=1010,
        cache_key='oauth_codex') is None
    latest = latest_subscription_quota(cache_key='oauth_codex', now=1020)
    assert latest['source'] == 'response_headers'


def test_oauth_status_marks_quota_refresh_due_only_when_stale(monkeypatch):
    import lib.oauth.codex_usage as codex_usage
    from routes.api_v1.oauth import _with_quota_state

    seen = {}

    def _spy(*, user_id='', quota_refresh_due=False, **_kwargs):
        seen['quota_refresh_due'] = quota_refresh_due
        return {'state': 'unknown', 'refreshing': False}

    monkeypatch.setattr(codex_usage, 'codex_usage_reset_status', _spy)

    record_codex_quota({
        'x-codex-primary-used-percent': '9',
    }, {}, cache_key='oauth_codex')  # captured at real now: fresh
    _with_quota_state({'authenticated': True}, 'codex', user_id='1')
    assert seen['quota_refresh_due'] is False

    record_codex_quota({
        'x-codex-primary-used-percent': '9',
    }, {}, now=1000, cache_key='oauth_codex')  # ancient capture: stale
    _with_quota_state({'authenticated': True}, 'codex', user_id='1')
    assert seen['quota_refresh_due'] is True

    _reset_subscription_quota_cache_for_tests()  # nothing cached: due
    _with_quota_state({'authenticated': True}, 'codex', user_id='1')
    assert seen['quota_refresh_due'] is True
