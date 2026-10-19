"""Admitted open-mode work has no per-peer request budget or store lifecycle."""
from types import SimpleNamespace
import asyncio
import pytest
from quart import Quart

pytestmark = pytest.mark.unit

@pytest.mark.parametrize('remote', ['0', '1'])
@pytest.mark.parametrize('legacy_rpm', ['', '1', '120', 'not-a-number'])
def test_open_requests_do_not_create_counters(monkeypatch, remote, legacy_rpm):
    from lib.rate_limit.rate_limit_api import check_open_mode_request, check_request
    import lib.rate_limit.rate_limit_store as store
    monkeypatch.setenv('TOFU_OPEN_MODE_ALLOW_REMOTE', remote)
    monkeypatch.setenv('TOFU_OPEN_MODE_RPM', legacy_rpm)
    monkeypatch.setenv('TOFU_OPEN_MODE_EXEMPT_PATHS', '/custom')
    monkeypatch.setattr(store, 'get_store', lambda: pytest.fail('peer counter created'))
    monkeypatch.setattr(store, 'get_token_bucket_store', lambda: pytest.fail('credential store created'))
    context = SimpleNamespace(via_open_mode=True)
    for _ in range(4097):
        decision = check_open_mode_request(client_ip='203.0.113.7', request_cost=256)
        assert decision.allowed and decision.rpm_limit == 0
        assert check_request(context, request_cost=256).allowed

@pytest.mark.parametrize('path', ['/api/v4/tasks/t1/poll', '/api/v4/chat/completions',
                                  '/api/v4/search', '/api/v4/browser/commands', '/custom'])
def test_all_admitted_paths_share_count_free_behavior(monkeypatch, path):
    from lib.rate_limit.rate_limit_api import check_open_mode_request
    monkeypatch.setenv('TOFU_OPEN_MODE_RPM', '1')
    app = Quart(__name__)
    async def run():
        async with app.test_request_context(path):
            assert all(check_open_mode_request().allowed for _ in range(129))
    asyncio.run(run())
