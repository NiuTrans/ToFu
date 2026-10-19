"""Persistent matrix 429s recommend disable; retry success stays healthy."""
from copy import deepcopy

import pytest

from lib.provider import provider_probe as probe

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('detail', [
    'HTTP 429 每分钟请求次数超过限制',
    'HTTP 429 每小时请求次数超过限制',
    'HTTP 429 requests per minute exceeded',
    'HTTP 429 per-hour limit exceeded',
    'HTTP 429 quota exceeded',
])
def test_all_final_rate_limits_recommend_disable(detail):
    assert probe._recommend_disable('rate_limited', detail)


def test_saved_rate_limit_recommendation_is_upgraded_without_mutation():
    snapshot = {
        'probe_schema_version': probe.PROBE_SCHEMA_VERSION,
        'status': 'done',
        'cells': {'0::model': {
            'status': 'rate_limited', 'detail': 'HTTP 429 per minute',
            'recommend_disable': False,
        }},
        'summary': {'disable': 0},
    }
    original = deepcopy(snapshot)
    normalized = probe.normalize_probe_snapshot(snapshot)
    assert normalized['cells']['0::model']['recommend_disable'] is True
    assert normalized['summary']['disable'] == 1
    assert snapshot == original


def test_successful_retry_is_not_recommended_for_disable(monkeypatch):
    results = iter([
        ('rate_limited', 'HTTP 429 per minute'), ('ok', 'HTTP 200'),
    ])
    monkeypatch.setattr(probe, 'probe_one_cell', lambda *a, **kw: next(results))
    status, detail = probe.probe_cell_multi(
        'https://example.test/v1', 'test-key', 'model', {}, 2,
        attempts=3, retry_delay=0,
    )
    assert status == 'ok'
    assert probe._recommend_disable(status, detail) is False
