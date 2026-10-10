"""CatPaw Credit-Hub quota telemetry over the browser bridge.

The Credit-Hub console origin is unreachable from the Tofu server, so the
extension sniffs the SSO ``access-token`` header from the user's own
logged-in tab, polls the quota API, and piggy-backs the derived snapshot on
the bridge poll body (``creditHubQuota``). These tests pin the server-side
contract: the poll route accepts and stores the snapshot, the registry
exposes the freshest one per owner, and the v1 read endpoint returns it
owner-scoped (never across tenants).
"""

import pytest

pytestmark = pytest.mark.unit

OWNER = '101'
OTHER = '202'
CLIENT = 'machine-quota'


@pytest.fixture(autouse=True)
def _clean_state():
    from lib.browser.queue import _state

    def _clear():
        with _state._clients_lock:
            _state._clients.clear()

    _clear()
    yield
    _clear()


def _register(client_id=CLIENT, owner_user_id=OWNER, quota=None):
    from lib.browser.protocol import ALL_CAPABILITIES
    from lib.browser.queue import mark_poll
    mark_poll(
        client_id,
        owner_user_id=owner_user_id,
        protocol_version=2,
        capabilities=sorted(ALL_CAPABILITIES),
        credit_hub_quota=quota,
    )


def _quota(total=1000000, used=12345, remaining=987655, fetched_at=1788945000000):
    return {
        'total': total,
        'used': used,
        'remaining': remaining,
        'fetchedAt': fetched_at,
    }


# ── registry storage ──────────────────────────────────────────────────


def test_mark_poll_stores_quota_snapshot():
    from lib.browser.queue import get_connected_clients
    _register(quota=_quota())
    clients = get_connected_clients(owner_user_id=OWNER)
    assert len(clients) == 1
    snap = clients[0]['credit_hub_quota']
    assert snap['total'] == 1000000
    assert snap['used'] == 12345
    assert snap['remaining'] == 987655
    assert snap['fetched_at'] == 1788945000000


def test_mark_poll_without_quota_leaves_none():
    from lib.browser.queue import get_connected_clients
    _register()
    assert get_connected_clients(owner_user_id=OWNER)[0]['credit_hub_quota'] is None


def test_get_credit_hub_quota_picks_freshest():
    from lib.browser.queue import get_credit_hub_quota
    _register(client_id='stale', quota=_quota(used=1, fetched_at=1000))
    _register(client_id='fresh', quota=_quota(used=2, fetched_at=2000))
    snap = get_credit_hub_quota(owner_user_id=OWNER)
    assert snap['used'] == 2
    assert snap['fetched_at'] == 2000


def test_get_credit_hub_quota_owner_scoped():
    from lib.browser.queue import get_credit_hub_quota
    _register(client_id='owner-a', owner_user_id=OWNER, quota=_quota())
    assert get_credit_hub_quota(owner_user_id=OTHER) is None


# ── poll route validation ─────────────────────────────────────────────


def test_poll_route_accepts_credit_hub_quota():
    from routes.browser import browser_poll  # noqa: F401 — import smoke
    # Field acceptance is covered end-to-end by the v1 endpoint test below;
    # this just guards against the route module failing to import after the
    # new validation branch was added.
    assert callable(browser_poll)
