"""Structured Codex earned-reset detection and lifecycle specifications."""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import unittest.mock as mock

import pytest

import lib.oauth.codex_usage as codex_usage

pytestmark = pytest.mark.unit


class _Response:
    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, payload):
        self.payload = payload
        self.content = json.dumps(payload).encode("utf-8")

    def json(self):
        return self.payload


def _token(account_id: str = "account-1") -> dict:
    return {
        "access_token": "stored-token",
        "refresh_token": "refresh-token",
        "account_id": account_id,
        "expire": time.time() + 3600,
    }


@pytest.fixture(autouse=True)
def _private_cache(tmp_path, monkeypatch):
    from lib.subscription_quota import (
        _reset_subscription_quota_cache_for_tests)
    cache_path = tmp_path / "oauth" / "codex_usage_reset_cache.json"
    monkeypatch.setattr(codex_usage, "_cache_path", lambda: str(cache_path))
    codex_usage._reset_codex_usage_state_for_tests()
    _reset_subscription_quota_cache_for_tests()
    yield cache_path
    codex_usage._reset_codex_usage_state_for_tests()
    _reset_subscription_quota_cache_for_tests()


def test_reset_credit_is_never_inferred_from_window_reset_timestamps():
    assert codex_usage._parse_usage_available_count({
        "rate_limit": {"resets_at": 1_900_000_000},
        "resets_at": 1_900_000_000,
    }) is None
    assert codex_usage._parse_usage_available_count({
        "rate_limit_reset_credits": {"available_count": 0},
    }) == 0
    assert codex_usage._parse_usage_available_count({
        "rate_limit_reset_credits": {"available_count": 1},
    }) == 1
    for invalid in (True, "1", -1, 101):
        assert codex_usage._parse_usage_available_count({
            "rate_limit_reset_credits": {"available_count": invalid},
        }) is None


def test_refresh_reads_authenticated_usage_and_details_into_private_cache(
        _private_cache):
    usage = _Response({
        "plan_type": "pro",
        "rate_limit_reset_credits": {"available_count": 1},
    })
    details = _Response({
        "available_count": 1,
        "credits": [None, {
            "id": "credit-1",
            "reset_type": "codex_rate_limits",
            "status": "available",
            "granted_at": "2030-01-01T00:00:00Z",
            "expires_at": "2030-02-01T00:00:00Z",
            "title": "Full reset (Weekly + 5 hr)",
            "description": "Ready to redeem",
        }],
    })
    with mock.patch("lib.oauth.token_store.load_token", return_value=_token()), \
         mock.patch(
             "lib.oauth.outbound.resolve_oauth_request",
             return_value=("live-token", {
                 "originator": "codex-tui",
                 "chatgpt-account-id": "account-1",
             }, {}),
         ) as resolve, \
         mock.patch("lib.desktop.egress.route_request", return_value="direct"), \
         mock.patch("lib.http_transport.client.http_get",
                    side_effect=[usage, details]) as get:
        status = codex_usage.refresh_codex_usage_reset(
            user_id="owner-7", force=True, now=1_800_000_000)

    assert status["state"] == "available"
    assert status["available_count"] == 1
    assert status["notification_key"]
    assert status["title"] == "Full reset (Weekly + 5 hr)"
    assert status["description"] == "Ready to redeem"
    assert status["expires_at"] == 1_896_134_400
    assert status["stale"] is False
    assert [call.args[0] for call in get.call_args_list] == [
        "https://chatgpt.com/backend-api/wham/usage",
        "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits",
    ]
    assert resolve.call_count == 2
    assert {call.kwargs["user_id"] for call in resolve.call_args_list} \
        == {"owner-7"}
    affinity_bodies = [call.args[1] for call in resolve.call_args_list]
    assert affinity_bodies[0] == affinity_bodies[1]
    assert affinity_bodies[0]["_conv_id"].startswith("codex-usage-reset:")
    assert get.call_args_list[0].kwargs["headers"]["Authorization"] \
        == "Bearer live-token"

    assert stat.S_IMODE(os.stat(_private_cache).st_mode) == 0o600
    stored_text = _private_cache.read_text(encoding="utf-8")
    assert "stored-token" not in stored_text
    assert "live-token" not in stored_text
    assert "account-1" not in stored_text
    persisted = json.loads(stored_text)
    assert len(persisted["entries"]) == 1
    assert persisted["entries"][0]["account_fingerprint"]


def test_zero_is_explicit_none_and_skips_detail_request():
    with mock.patch("lib.oauth.token_store.load_token", return_value=_token()), \
         mock.patch(
             "lib.oauth.outbound.resolve_oauth_request",
             return_value=("live-token", {}, {}),
         ), \
         mock.patch("lib.desktop.egress.route_request", return_value="direct"), \
         mock.patch(
             "lib.http_transport.client.http_get",
             return_value=_Response({
                 "rate_limit_reset_credits": {"available_count": 0},
             }),
         ) as get:
        status = codex_usage.refresh_codex_usage_reset(
            user_id="1", force=True, now=1000)

    assert status["state"] == "none"
    assert status["available_count"] == 0
    assert "notification_key" not in status
    assert get.call_count == 1


def test_successful_usage_without_reset_field_stays_unknown_not_zero():
    with mock.patch("lib.oauth.token_store.load_token", return_value=_token()), \
         mock.patch(
             "lib.oauth.outbound.resolve_oauth_request",
             return_value=("live-token", {}, {}),
         ), \
         mock.patch("lib.desktop.egress.route_request", return_value="direct"), \
         mock.patch(
             "lib.http_transport.client.http_get",
             return_value=_Response({
                 "primary_window": {"resets_at": 1_900_000_000},
             }),
         ):
        status = codex_usage.refresh_codex_usage_reset(
            user_id="1", force=True, now=1000)

    assert status["state"] == "unknown"
    assert status["available_count"] is None
    assert status["reason"] == "not_reported"
    assert status["retry_after_seconds"] \
        == codex_usage.CODEX_USAGE_RESET_TTL_S


def test_refresh_failure_preserves_last_good_as_immediately_stale():
    available_usage = _Response({
        "rate_limit_reset_credits": {"available_count": 1},
    })
    with mock.patch("lib.oauth.token_store.load_token", return_value=_token()), \
         mock.patch.object(
             codex_usage, "_authenticated_get",
             side_effect=[available_usage.payload, RuntimeError("details down")],
         ):
        first = codex_usage.refresh_codex_usage_reset(
            user_id="1", force=True, now=1000)

    with mock.patch("lib.oauth.token_store.load_token", return_value=_token()), \
         mock.patch.object(
             codex_usage, "_authenticated_get",
             side_effect=RuntimeError("usage down"),
         ):
        failed = codex_usage.refresh_codex_usage_reset(
            user_id="1", force=True, now=1010)

    assert first["state"] == "available"
    assert failed["state"] == "available"
    assert failed["available_count"] == 1
    assert failed["notification_key"] == first["notification_key"]
    assert failed["captured_at"] == 1000
    assert failed["stale"] is True
    assert failed["retry_after_seconds"] \
        == codex_usage.CODEX_USAGE_RESET_FAILURE_RETRY_S


def test_account_switch_during_refresh_cannot_publish_the_old_observation():
    with mock.patch(
            "lib.oauth.token_store.load_token",
            side_effect=[_token("account-old"), _token("account-new")]), \
         mock.patch.object(
             codex_usage, "_authenticated_get",
             return_value={
                 "rate_limit_reset_credits": {"available_count": 0},
             }):
        status = codex_usage.refresh_codex_usage_reset(
            user_id="owner", force=True, now=1000)

    assert status["state"] == "unknown"
    assert status["reason"] == "account_changed"
    assert status["available_count"] is None


def test_owner_and_account_scopes_cannot_reuse_another_cache_row():
    identity = codex_usage._identity(_token("account-a"), "owner-a")
    assert identity is not None
    codex_usage._write_entries({
        identity["cache_key"]: {
            **identity,
            "state": "available",
            "available_count": 1,
            "captured_at": 1000,
            "notification_key": "a" * 24,
        },
    })

    with mock.patch("lib.oauth.token_store.load_token",
                    return_value=_token("account-a")):
        same = codex_usage.codex_usage_reset_status(
            user_id="owner-a", refresh_if_stale=False, now=1001)
        other_owner = codex_usage.codex_usage_reset_status(
            user_id="owner-b", refresh_if_stale=False, now=1001)
    with mock.patch("lib.oauth.token_store.load_token",
                    return_value=_token("account-b")):
        other_account = codex_usage.codex_usage_reset_status(
            user_id="owner-a", refresh_if_stale=False, now=1001)

    assert same["state"] == "available"
    assert other_owner["state"] == "unknown"
    assert other_account["state"] == "unknown"


def test_count_only_to_detailed_transition_does_not_duplicate_notice():
    identity = codex_usage._identity(_token(), "owner")
    assert identity is not None
    count_only = codex_usage._available_entry(
        identity, 1, [], None, 1000)
    with_details = codex_usage._available_entry(
        identity, 1, [{"id": "credit-1"}], count_only, 1010)
    same_details = codex_usage._available_entry(
        identity, 1, [{"id": "credit-1"}], with_details, 1020)
    new_credit = codex_usage._available_entry(
        identity, 1, [{"id": "credit-2"}], same_details, 1030)

    assert with_details["notification_key"] == count_only["notification_key"]
    assert same_details["notification_key"] == count_only["notification_key"]
    assert new_credit["notification_key"] != count_only["notification_key"]


def test_cache_loader_rejects_non_integer_zero_and_shape_drifted_text():
    identity = codex_usage._identity(_token(), "owner")
    assert identity is not None
    base = {
        **identity,
        "state": "none",
        "captured_at": 1000,
    }
    assert codex_usage._normalise_entry({
        **base, "available_count": False}) is None
    assert codex_usage._normalise_entry({
        **base, "available_count": 0.0}) is None
    assert codex_usage._bounded_text({"unexpected": "object"}, 20) == ""


def test_credit_expiry_requires_an_explicit_timezone():
    assert codex_usage._parse_iso_timestamp("2030-02-01T00:00:00") is None
    assert codex_usage._parse_iso_timestamp(
        "2030-02-01T00:00:00Z") == 1_896_134_400
    assert codex_usage._parse_iso_timestamp(
        "2030-02-01T08:00:00+08:00") == 1_896_134_400


def test_cache_retains_all_valid_identity_rows_without_count_truncation(_private_cache):
    entries = {}
    for index in range(1200):
        identity = codex_usage._identity(
            _token(f"account-{index}"), f"owner-{index}")
        assert identity is not None
        entries[identity["cache_key"]] = {
            **identity,
            "state": "none",
            "available_count": 0,
            "captured_at": 1000 + index,
        }
    codex_usage._write_entries(entries)

    persisted = json.loads(_private_cache.read_text(encoding="utf-8"))
    assert len(persisted["entries"]) == 1200
    assert len(codex_usage._read_entries()) == 1200


def test_different_owner_refreshes_do_not_share_a_network_lock_or_lose_rows():
    barrier = threading.Barrier(2)

    def fetch(_url, **_kwargs):
        barrier.wait(timeout=5)
        return {"rate_limit_reset_credits": {"available_count": 0}}

    with mock.patch("lib.oauth.token_store.load_token", return_value=_token()), \
         mock.patch.object(codex_usage, "_authenticated_get", side_effect=fetch):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    codex_usage.refresh_codex_usage_reset,
                    user_id=owner, force=True, now=1000,
                )
                for owner in ("owner-a", "owner-b")
            ]
            statuses = [future.result(timeout=10) for future in futures]

    assert [status["state"] for status in statuses] == ["none", "none"]
    assert len(codex_usage._read_entries()) == 2


def test_refresh_identity_lock_artifacts_retire_after_account_churn(_private_cache):
    from lib.oauth.identity_flight import identity_flight
    for index in range(100):
        identity = codex_usage._identity(_token(f'account-{index}'), f'owner-{index}')
        with identity_flight(str(_private_cache), identity['cache_key']):
            pass
    assert not list(_private_cache.parent.glob('*.flight.active.*'))
    assert len(list(_private_cache.parent.glob('*.flight.guard'))) == 1


def test_refresh_deduplicates_same_identity_and_accepts_independent_owners(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def refresh(**_kwargs):
        started.set()
        assert release.wait(timeout=5)
        return {"state": "none"}

    monkeypatch.setattr(codex_usage, "refresh_codex_usage_reset", refresh)
    monkeypatch.setattr("lib.oauth.token_store.load_token", lambda _p: _token())

    assert codex_usage.trigger_codex_usage_reset_refresh(user_id="owner") is True
    assert started.wait(timeout=5)
    assert codex_usage.trigger_codex_usage_reset_refresh(user_id="owner") is False
    assert codex_usage.trigger_codex_usage_reset_refresh(user_id="owner-2") is True
    assert codex_usage.trigger_codex_usage_reset_refresh(user_id="owner-3") is True
    release.set()
    deadline = time.monotonic() + 5
    identities = [
        codex_usage._identity(_token(), owner)
        for owner in ("owner", "owner-2", "owner-3")
    ]
    assert all(identity is not None for identity in identities)
    while any(codex_usage._is_refreshing(identity["cache_key"])
              for identity in identities if identity is not None):
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_daemon_pushes_one_owner_scoped_completion_projection(monkeypatch):
    published = []
    delivered = threading.Event()
    reset_offer = {
        "state": "available",
        "available_count": 1,
        "captured_at": 1000,
        "stale": False,
        "refreshing": False,
        "notification_key": "a" * 24,
    }

    monkeypatch.setattr(
        codex_usage,
        "refresh_codex_usage_reset",
        lambda **_kwargs: reset_offer,
    )
    monkeypatch.setattr("lib.oauth.token_store.load_token", lambda _p: _token())

    def publish(channel, task_id, payload, *, user_id):
        published.append((channel, task_id, payload, user_id))
        delivered.set()

    monkeypatch.setattr("lib.agent_core.push.push_event", publish)

    assert codex_usage.trigger_codex_usage_reset_refresh(user_id="owner-42") is True
    assert delivered.wait(timeout=5)
    identity = codex_usage._identity(_token(), "owner-42")
    assert identity is not None
    deadline = time.monotonic() + 5
    while codex_usage._is_refreshing(identity["cache_key"]):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert published == [(
        codex_usage.CODEX_USAGE_RESET_PUSH_CHANNEL,
        codex_usage.CODEX_USAGE_RESET_PUSH_TASK_ID,
        {
            "type": codex_usage.CODEX_USAGE_RESET_PUSH_EVENT_TYPE,
            "provider": "codex",
            "reset_offer": reset_offer,
        },
        "owner-42",
    )]
    assert codex_usage._is_refreshing(identity["cache_key"]) is False


def test_completion_push_failure_cannot_strand_refresh_state(monkeypatch):
    delivered = threading.Event()
    monkeypatch.setattr(
        codex_usage,
        "refresh_codex_usage_reset",
        lambda **_kwargs: {"state": "none"},
    )
    monkeypatch.setattr("lib.oauth.token_store.load_token", lambda _p: _token())

    def fail_publish(*_args, **_kwargs):
        delivered.set()
        raise RuntimeError("injected push outage")

    monkeypatch.setattr("lib.agent_core.push.push_event", fail_publish)
    assert codex_usage.trigger_codex_usage_reset_refresh(user_id="owner-42") is True
    assert delivered.wait(timeout=5)
    identity = codex_usage._identity(_token(), "owner-42")
    assert identity is not None
    deadline = time.monotonic() + 5
    while codex_usage._is_refreshing(identity["cache_key"]):
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_status_starts_refresh_even_with_two_independent_queries_active(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def refresh(**kwargs):
        entered.set()
        assert release.wait(5)
        return {'state':'none'}
    monkeypatch.setattr(codex_usage, 'refresh_codex_usage_reset', refresh)
    monkeypatch.setattr('lib.oauth.token_store.load_token', lambda p:_token())
    with codex_usage._state_lock:
        codex_usage._refreshing_keys.update({'occupied-a','occupied-b'})
    try:
        status = codex_usage.codex_usage_reset_status(user_id='waiting-owner',now=1000)
        assert entered.wait(2)
        assert status['refreshing']
        assert 'retry_after_seconds' not in status
    finally:
        release.set()
        identity = codex_usage._identity(_token(),'waiting-owner')
        deadline = time.monotonic()+3
        while codex_usage._is_refreshing(identity['cache_key']):
            assert time.monotonic()<deadline
            time.sleep(0.005)


def test_status_read_discards_a_projection_when_account_changes_mid_read():
    old = _token("account-old")
    identity = codex_usage._identity(old, "owner")
    assert identity is not None
    codex_usage._write_entries({
        identity["cache_key"]: {
            **identity,
            "state": "available",
            "available_count": 1,
            "captured_at": 1000,
            "notification_key": "a" * 24,
        },
    })
    with mock.patch(
            "lib.oauth.token_store.load_token",
            side_effect=[old, _token("account-new")]):
        status = codex_usage.codex_usage_reset_status(
            user_id="owner", refresh_if_stale=False, now=1001)

    assert status["state"] == "unknown"
    assert status["reason"] == "account_changed"


def test_authenticated_get_uses_the_selected_desktop_egress_agent():
    response = _Response({
        "rate_limit_reset_credits": {"available_count": 0},
    })
    with mock.patch(
            "lib.oauth.outbound.resolve_oauth_request",
            return_value=("live-token", {}, {})) as resolve, \
         mock.patch("lib.desktop.egress.route_request",
                    return_value="agent-1"), \
         mock.patch("lib.desktop.egress.egress_http",
                    return_value=response) as egress:
        payload = codex_usage._authenticated_get(
            "https://chatgpt.com/backend-api/wham/usage",
            timeout=5, user_id="owner", affinity_key="f" * 24)

    assert payload["rate_limit_reset_credits"]["available_count"] == 0
    assert resolve.call_args.args[1] == {
        "_conv_id": "codex-usage-reset:" + "f" * 24
    }
    assert egress.call_args.kwargs["method"] == "GET"
    assert egress.call_args.kwargs["agent_id"] == "agent-1"
    assert egress.call_args.kwargs["user_id"] == "owner"


def test_cache_clear_requires_an_explicit_scope_or_clear_all():
    entries = {}
    for account, owner in (("account-a", "owner-a"),
                           ("account-b", "owner-b")):
        identity = codex_usage._identity(_token(account), owner)
        assert identity is not None
        entries[identity["cache_key"]] = {
            **identity,
            "state": "none",
            "available_count": 0,
            "captured_at": 1000,
        }
    codex_usage._write_entries(entries)

    assert codex_usage.clear_codex_usage_reset_cache() == 0
    assert len(codex_usage._read_entries()) == 2
    assert codex_usage.clear_codex_usage_reset_cache(
        owner_user_id="owner-a") == 1
    assert len(codex_usage._read_entries()) == 1
    assert codex_usage.clear_codex_usage_reset_cache(clear_all=True) == 1
    assert codex_usage._read_entries() == {}


def test_status_projection_uses_authenticated_owner_for_detector():
    from routes.api_v1.oauth import _with_quota_state

    reset = {
        "state": "available",
        "available_count": 1,
        "source": "codex_usage_api",
        "captured_at": 1000,
        "stale": False,
        "refreshing": False,
        "notification_key": "b" * 24,
    }
    with mock.patch(
        "lib.oauth.codex_usage.codex_usage_reset_status",
        return_value=reset,
    ) as detector:
        projected = _with_quota_state(
            {"authenticated": True}, "codex", "owner-42")

    assert projected["reset_offer"] == reset
    # No cached quota snapshot here, so the read is due for a quota refresh.
    detector.assert_called_once_with(user_id="owner-42",
                                     quota_refresh_due=True)


def test_logout_with_missing_account_identity_does_not_clear_reset_cache():
    from lib.oauth.manager import _exchange

    token = _token("")
    with mock.patch("lib.oauth.token_store.load_token", return_value=token), \
         mock.patch("lib.oauth.token_store.delete_token", return_value=True), \
         mock.patch("lib.oauth.outbound.deprovision_oauth_provider"), \
         mock.patch("lib.oauth.manager._device.stop_device_flow"), \
         mock.patch("lib.subscription_quota.clear_subscription_quota"), \
         mock.patch(
             "lib.oauth.codex_usage.clear_codex_usage_reset_cache"
         ) as clear_reset:
        result = _exchange.logout_oauth("codex")

    assert result["ok"] is True
    clear_reset.assert_not_called()


def test_failed_logout_preserves_all_derived_cache_state():
    from lib.oauth.manager import _exchange

    with mock.patch("lib.oauth.token_store.load_token",
                    return_value=_token("account-to-keep")), \
         mock.patch("lib.oauth.token_store.delete_token", return_value=False), \
         mock.patch("lib.oauth.outbound.deprovision_oauth_provider"), \
         mock.patch("lib.oauth.manager._device.stop_device_flow"), \
         mock.patch(
             "lib.subscription_quota.clear_subscription_quota"
         ) as clear_quota, \
         mock.patch(
             "lib.oauth.codex_usage.clear_codex_usage_reset_cache"
         ) as clear_reset:
        result = _exchange.logout_oauth("codex")

    assert result == {
        "ok": False,
        "provider": "codex",
        "error": "credential_delete_failed",
    }
    clear_quota.assert_not_called()
    clear_reset.assert_not_called()


def test_reset_cleanup_survives_quota_cleanup_failure():
    from lib.oauth.manager import _exchange

    with mock.patch("lib.oauth.token_store.load_token",
                    return_value=_token("account-to-clear")), \
         mock.patch("lib.oauth.token_store.delete_token", return_value=True), \
         mock.patch("lib.oauth.outbound.deprovision_oauth_provider"), \
         mock.patch("lib.oauth.manager._device.stop_device_flow"), \
         mock.patch(
             "lib.subscription_quota.clear_subscription_quota",
             side_effect=RuntimeError("injected quota cleanup failure"),
         ), \
         mock.patch(
             "lib.oauth.codex_usage.clear_codex_usage_reset_cache"
         ) as clear_reset:
        result = _exchange.logout_oauth("codex")

    assert result["ok"] is True
    clear_reset.assert_called_once_with(account_id="account-to-clear")


def test_logout_authority_clears_both_quota_and_reset_cache():
    from lib.oauth.manager import _exchange

    token = _token("account-to-clear")
    with mock.patch("lib.oauth.token_store.load_token", return_value=token), \
         mock.patch("lib.oauth.token_store.delete_token", return_value=True), \
         mock.patch("lib.oauth.outbound.deprovision_oauth_provider"), \
         mock.patch("lib.oauth.manager._device.stop_device_flow"), \
         mock.patch(
             "lib.subscription_quota.clear_subscription_quota"
         ) as clear_quota, \
         mock.patch(
             "lib.oauth.codex_usage.clear_codex_usage_reset_cache"
         ) as clear_reset:
        result = _exchange.logout_oauth("codex")

    assert result["ok"] is True
    clear_quota.assert_called_once_with("codex", cache_key="oauth_codex")
    clear_reset.assert_called_once_with(account_id="account-to-clear")


# ── Explicit consume command (the "Reset now" spend) ────────────────


def _details_payload(*credit_ids: str) -> dict:
    return {
        "available_count": len(credit_ids),
        "credits": [
            {
                "id": credit_id,
                "reset_type": "codex_rate_limits",
                "status": "available",
                "granted_at": "2030-01-01T00:00:00Z",
                "expires_at": f"2030-02-{index + 1:02d}T00:00:00Z",
            }
            for index, credit_id in enumerate(credit_ids)
        ],
    }


def _consume_mocks(details, post_response=None, post_side_effect=None,
                   projection=None):
    """Patch every network/settle seam of the consume command."""
    projection = (
        {"state": "none", "available_count": 0, "captured_at": 2000,
         "stale": False, "refreshing": False}
        if projection is None else projection)
    if post_side_effect is not None:
        post = mock.patch.object(
            codex_usage, "_authenticated_post", side_effect=post_side_effect)
    else:
        post = mock.patch.object(
            codex_usage, "_authenticated_post", return_value=post_response)
    return (
        mock.patch("lib.oauth.token_store.load_token", return_value=_token()),
        mock.patch.object(
            codex_usage, "_authenticated_get", return_value=details),
        post,
        mock.patch.object(
            codex_usage, "refresh_codex_usage_reset", return_value=projection),
        mock.patch.object(codex_usage, "_publish_codex_usage_reset_update"),
    )


def test_consume_spends_soonest_expiring_credit_and_settles_projection():
    projection = {"state": "none", "available_count": 0, "captured_at": 2000,
                  "stale": False, "refreshing": False}
    published = []
    patches = _consume_mocks(
        _details_payload("credit-1", "credit-2"),
        post_response=_Response({
            "code": "reset", "windows_reset": 2,
            "credit": {"redeemed_at": "2030-01-02T00:00:00Z"},
        }),
        projection=projection)
    with patches[0], patches[1] as get, patches[2] as post, \
         patches[3] as refresh, patches[4] as publish:
        publish.side_effect = lambda **kwargs: published.append(kwargs)
        result = codex_usage.consume_codex_usage_reset(
            user_id="owner-7", now=1_800_000_000)

    assert result["ok"] is True
    assert result["windows_reset"] == 2
    assert result["redeemed_at"] == "2030-01-02T00:00:00Z"
    assert result["reset_offer"] == projection
    assert get.call_args.args[0] == (
        "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits")
    assert post.call_args.args[0] == (
        "https://chatgpt.com/backend-api/wham/"
        "rate-limit-reset-credits/consume")
    body = post.call_args.args[1]
    assert body["credit_id"] == "credit-1"
    assert isinstance(body["redeem_request_id"], str)
    assert len(body["redeem_request_id"]) == 36
    assert post.call_args.kwargs["user_id"] == "owner-7"
    refresh.assert_called_once_with(user_id="owner-7", force=True)
    assert published == [
        {"user_id": "owner-7", "reset_offer": projection}]
    identity = codex_usage._identity(_token(), "owner-7")
    assert identity is not None
    entry = codex_usage._read_entries()[identity["cache_key"]]
    assert "pending_redeem" not in entry


def test_consume_retry_after_ambiguous_outcome_reuses_persisted_request_id():
    request_ids = []

    def flaky_post(_url, payload, **_kwargs):
        request_ids.append(payload["redeem_request_id"])
        if len(request_ids) == 1:
            raise RuntimeError("injected transport outage")
        return _Response({"code": "reset", "windows_reset": 1})

    patches = _consume_mocks(
        _details_payload("credit-1"), post_side_effect=flaky_post)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        first = codex_usage.consume_codex_usage_reset(user_id="o", now=1000)
        second = codex_usage.consume_codex_usage_reset(user_id="o", now=1010)

    assert first["ok"] is False
    assert first["error"] == "unresolved"
    assert second["ok"] is True
    assert len(request_ids) == 2
    assert request_ids[0] == request_ids[1]


def test_consume_ambiguous_retry_against_another_credit_mints_a_fresh_key():
    request_ids = []
    responses = [RuntimeError("injected transport outage"),
                 _Response({"code": "reset"})]
    details_sequence = [_details_payload("credit-1"),
                        _details_payload("credit-2")]

    def post(_url, payload, **_kwargs):
        request_ids.append(payload["redeem_request_id"])
        outcome = responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    patches = _consume_mocks(None)
    with patches[0], \
         mock.patch.object(codex_usage, "_authenticated_get",
                           side_effect=details_sequence), \
         mock.patch.object(codex_usage, "_authenticated_post",
                           side_effect=post), \
         patches[3], patches[4]:
        first = codex_usage.consume_codex_usage_reset(user_id="o", now=1000)
        second = codex_usage.consume_codex_usage_reset(user_id="o", now=1010)

    assert first["error"] == "unresolved"
    assert second["ok"] is True
    assert len(request_ids) == 2
    assert request_ids[0] != request_ids[1]


@pytest.mark.parametrize("code", ["nothing_to_reset", "already_redeemed"])
def test_consume_definitive_no_spend_codes_clear_pending(code):
    patches = _consume_mocks(
        _details_payload("credit-1"),
        post_response=_Response({"code": code}))
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = codex_usage.consume_codex_usage_reset(user_id="o", now=1000)

    assert result["ok"] is False
    assert result["error"] == code
    identity = codex_usage._identity(_token(), "o")
    assert identity is not None
    entry = codex_usage._read_entries()[identity["cache_key"]]
    assert "pending_redeem" not in entry


def test_consume_definitive_4xx_rejection_clears_pending():
    rejected = _Response({"error": "forbidden"})
    rejected.status_code = 403
    patches = _consume_mocks(
        _details_payload("credit-1"), post_response=rejected)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = codex_usage.consume_codex_usage_reset(user_id="o", now=1000)

    assert result["ok"] is False
    assert result["error"] == "upstream_rejected"
    assert result["status"] == 403
    identity = codex_usage._identity(_token(), "o")
    assert identity is not None
    entry = codex_usage._read_entries()[identity["cache_key"]]
    assert "pending_redeem" not in entry


def test_consume_5xx_keeps_pending_record_for_idempotent_retry():
    failed = _Response({"error": "boom"})
    failed.status_code = 500
    patches = _consume_mocks(
        _details_payload("credit-1"), post_response=failed)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = codex_usage.consume_codex_usage_reset(user_id="o", now=1000)

    assert result["ok"] is False
    assert result["error"] == "unresolved"
    identity = codex_usage._identity(_token(), "o")
    assert identity is not None
    pending = codex_usage._read_entries()[
        identity["cache_key"]]["pending_redeem"]
    assert pending["credit_id"] == "credit-1"
    assert pending["redeem_request_id"]
    assert pending["created_at"] == 1000


def test_consume_without_available_credit_never_posts():
    patches = _consume_mocks(
        {"available_count": 0,
         "credits": [{"id": "credit-1", "status": "redeemed"}]})
    with patches[0], patches[1], patches[2] as post, patches[3], patches[4]:
        result = codex_usage.consume_codex_usage_reset(user_id="o", now=1000)

    assert result["ok"] is False
    assert result["error"] == "none_available"
    post.assert_not_called()


def test_consume_details_outage_is_upstream_unavailable_and_never_posts():
    patches = _consume_mocks(None)
    with patches[0], \
         mock.patch.object(codex_usage, "_authenticated_get",
                           side_effect=RuntimeError("details down")), \
         patches[2] as post, patches[3], patches[4]:
        result = codex_usage.consume_codex_usage_reset(user_id="o", now=1000)

    assert result["ok"] is False
    assert result["error"] == "upstream_unavailable"
    post.assert_not_called()


def test_consume_requires_authentication_and_skips_settle():
    with mock.patch("lib.oauth.token_store.load_token", return_value={}), \
         mock.patch.object(
             codex_usage, "refresh_codex_usage_reset") as refresh, \
         mock.patch.object(
             codex_usage, "_publish_codex_usage_reset_update") as publish:
        result = codex_usage.consume_codex_usage_reset(user_id="o", now=1000)

    assert result == {"ok": False, "error": "not_authenticated"}
    refresh.assert_not_called()
    publish.assert_not_called()


def test_consume_aborts_before_any_network_when_account_changes():
    with mock.patch(
            "lib.oauth.token_store.load_token",
            side_effect=[_token("account-a"), _token("account-b")]), \
         mock.patch.object(codex_usage, "_authenticated_get") as get, \
         mock.patch.object(codex_usage, "_authenticated_post") as post, \
         mock.patch.object(codex_usage, "refresh_codex_usage_reset",
                           return_value={"state": "unknown"}), \
         mock.patch.object(codex_usage, "_publish_codex_usage_reset_update"):
        result = codex_usage.consume_codex_usage_reset(user_id="o", now=1000)

    assert result["ok"] is False
    assert result["error"] == "account_changed"
    get.assert_not_called()
    post.assert_not_called()


def test_consume_route_envelope_and_error_status_mapping():
    import asyncio
    import sys

    import quart as _quart
    sys.modules.setdefault("flask", _quart)
    from quart import Quart
    if "PROVIDE_AUTOMATIC_OPTIONS" not in Quart.default_config:
        Quart.default_config = {**Quart.default_config,
                                "PROVIDE_AUTOMATIC_OPTIONS": True}
    app = Quart(__name__)
    from routes.api_v1.oauth import api_v1_oauth_bp
    app.register_blueprint(api_v1_oauth_bp)

    @app.before_request
    def _fake_auth():
        from quart import g
        from lib.api_keys import local_admin_context
        g.auth_ctx = local_admin_context()

    async def _run():
        client = app.test_client()
        with mock.patch(
                "lib.oauth.codex_usage.consume_codex_usage_reset",
                return_value={"ok": True, "code": "reset", "windows_reset": 2,
                              "reset_offer": {"state": "none"}}) as consume, \
             mock.patch("lib.log.audit_log") as audit:
            resp = await client.post(
                "/api/v1/oauth/reset-offer/consume", json={})
            assert resp.status_code == 200
            body = json.loads(await resp.get_data(as_text=True))
            assert body["ok"] is True
            assert body["windows_reset"] == 2
            assert body["reset_offer"] == {"state": "none"}
            consume.assert_called_once_with(user_id="1")
            audit.assert_called_once()
            assert audit.call_args.args[0] == "oauth_codex_reset_consumed"

        for error, expected in (
                ("not_authenticated", 401),
                ("none_available", 409),
                ("already_redeemed", 409),
                ("unresolved", 502),
                ("upstream_unavailable", 502),
        ):
            with mock.patch(
                    "lib.oauth.codex_usage.consume_codex_usage_reset",
                    return_value={"ok": False, "error": error}), \
                 mock.patch("lib.log.audit_log") as audit:
                resp = await client.post(
                    "/api/v1/oauth/reset-offer/consume", json={})
                assert resp.status_code == expected, error
                body = json.loads(await resp.get_data(as_text=True))
                assert body["ok"] is False
                assert body["error"] == error
                audit.assert_not_called()

    asyncio.run(_run())


def test_refresh_also_publishes_usage_api_quota_snapshot():
    from lib.subscription_quota import latest_subscription_quota

    usage_payload = {
        "plan_type": "plus",
        "rate_limit": {
            "allowed": True,
            "limit_reached": False,
            "primary_window": {
                "used_percent": 12,
                "limit_window_seconds": 18000,
                "reset_after_seconds": 3600,
                "reset_at": 1_800_003_600,
            },
            "secondary_window": {
                "used_percent": 4,
                "limit_window_seconds": 604800,
                "reset_after_seconds": 86400,
                "reset_at": 1_800_086_400,
            },
        },
        "rate_limit_reset_credits": {"available_count": 0},
    }
    with mock.patch("lib.oauth.token_store.load_token", return_value=_token()), \
         mock.patch.object(codex_usage, "_authenticated_get",
                           return_value=usage_payload):
        status = codex_usage.refresh_codex_usage_reset(
            user_id="owner", force=True, now=1_800_000_000)

    assert status["state"] == "none"
    quota = latest_subscription_quota(
        cache_key="oauth_codex", now=1_800_000_050)
    assert quota["source"] == "usage_api"
    assert quota["captured_at"] == 1_800_000_000
    assert quota["plan_type"] == "plus"
    assert quota["primary"] == {
        "used_percent": 12.0,
        "remaining_percent": 88.0,
        "window_minutes": 300,
        "resets_at": 1_800_003_600,
    }
    assert quota["secondary"]["window_minutes"] == 10080
    assert quota["age_seconds"] == 50


def test_quota_snapshot_failure_never_breaks_reset_refresh():
    usage_payload = {"rate_limit_reset_credits": {"available_count": 0}}
    with mock.patch("lib.oauth.token_store.load_token", return_value=_token()), \
         mock.patch.object(codex_usage, "_authenticated_get",
                           return_value=usage_payload), \
         mock.patch("lib.subscription_quota.record_codex_usage_snapshot",
                    side_effect=RuntimeError("quota store down")):
        status = codex_usage.refresh_codex_usage_reset(
            user_id="owner", force=True, now=1000)

    assert status["state"] == "none"


def test_account_switch_during_refresh_cannot_publish_quota():
    from lib.subscription_quota import latest_subscription_quota

    usage_payload = {
        "rate_limit": {
            "primary_window": {"used_percent": 12,
                               "limit_window_seconds": 18000},
        },
        "rate_limit_reset_credits": {"available_count": 0},
    }
    with mock.patch(
            "lib.oauth.token_store.load_token",
            side_effect=[_token("account-old"), _token("account-new")]), \
         mock.patch.object(codex_usage, "_authenticated_get",
                           return_value=usage_payload), \
         mock.patch("lib.subscription_quota.record_codex_usage_snapshot") \
            as record:
        status = codex_usage.refresh_codex_usage_reset(
            user_id="owner", force=True, now=1000)

    assert status["reason"] == "account_changed"
    record.assert_not_called()
    assert latest_subscription_quota(cache_key="oauth_codex", now=1010) is None


def test_quota_refresh_due_allows_one_bounded_extra_refresh():
    payload = {"rate_limit_reset_credits": {"available_count": 0}}
    with mock.patch("lib.oauth.token_store.load_token", return_value=_token()), \
         mock.patch.object(codex_usage, "_authenticated_get",
                           return_value=payload):
        codex_usage.refresh_codex_usage_reset(user_id="owner", force=True,
                                              now=1000)

    with mock.patch("lib.oauth.token_store.load_token", return_value=_token()), \
         mock.patch.object(codex_usage,
                           "trigger_codex_usage_reset_refresh") as trigger:
        # The reset entry itself stays fresh for its 30-minute TTL.
        codex_usage.codex_usage_reset_status(user_id="owner", now=1030)
        assert trigger.call_count == 0

        # A due quota snapshot allows one extra refresh per freshness window.
        codex_usage.codex_usage_reset_status(user_id="owner", now=1030,
                                             quota_refresh_due=True)
        assert trigger.call_count == 0  # last attempt was 30s ago
        codex_usage.codex_usage_reset_status(user_id="owner", now=1060,
                                             quota_refresh_due=True)
        assert trigger.call_count == 1
        codex_usage.codex_usage_reset_status(user_id="owner", now=1061,
                                             quota_refresh_due=False)
        assert trigger.call_count == 1


def test_quota_due_trigger_forces_past_fresh_reset_entry(_private_cache):
    payload = {"rate_limit_reset_credits": {"available_count": 0}}
    with mock.patch("lib.oauth.token_store.load_token", return_value=_token()), \
         mock.patch.object(codex_usage, "_authenticated_get",
                           return_value=payload):
        codex_usage.refresh_codex_usage_reset(user_id="owner", force=True,
                                              now=1000)

    with mock.patch("lib.oauth.token_store.load_token", return_value=_token()), \
         mock.patch.object(codex_usage,
                           "trigger_codex_usage_reset_refresh") as trigger:
        codex_usage.codex_usage_reset_status(user_id="owner", now=1060,
                                             quota_refresh_due=True)
        # The daemon must bypass the fresh reset-credit entry: without force
        # its inner freshness check discards this window's one extra refresh
        # and the quota snapshot is never re-recorded after a restart.
        trigger.assert_called_once_with(user_id="owner", force=True)


def test_forced_refresh_despite_fresh_entry_records_quota_snapshot(
        _private_cache):
    from lib.subscription_quota import latest_subscription_quota

    payload = {
        "plan_type": "plus",
        "rate_limit": {
            "primary_window": {"used_percent": 30,
                               "limit_window_seconds": 18000},
        },
        "rate_limit_reset_credits": {"available_count": 0},
    }
    with mock.patch("lib.oauth.token_store.load_token", return_value=_token()), \
         mock.patch.object(codex_usage, "_authenticated_get",
                           return_value=payload) as get:
        codex_usage.refresh_codex_usage_reset(user_id="owner", force=True,
                                              now=1000)
        assert get.call_count == 1
        # Fresh entry: the unforced daemon path must not fetch.
        codex_usage.refresh_codex_usage_reset(user_id="owner", force=False,
                                              now=1060)
        assert get.call_count == 1
        # The quota-driven forced path fetches and re-records the snapshot.
        codex_usage.refresh_codex_usage_reset(user_id="owner", force=True,
                                              now=1060)
        assert get.call_count == 2

    quota = latest_subscription_quota(cache_key="oauth_codex", now=1100)
    assert quota is not None
    assert quota["captured_at"] == 1060
    assert quota["primary"]["used_percent"] == 30.0


def test_thirty_two_blocked_refreshes_do_not_refuse_healthy_thirty_third(monkeypatch):
    entered, release, healthy = threading.Condition(), threading.Event(), threading.Event()
    identities = set()
    threads = []
    start = threading.Thread.start
    def capture(thread):
        if thread.name.startswith('codex-usage-reset-'):
            threads.append(thread)
        return start(thread)
    monkeypatch.setattr(threading.Thread, 'start', capture)
    monkeypatch.setattr('lib.oauth.token_store.load_token', lambda p:_token())
    monkeypatch.setattr(codex_usage, '_publish_codex_usage_reset_update', lambda **kwargs:None)
    def refresh(*, user_id, **kwargs):
        if user_id=='healthy':healthy.set();return {'state':'none'}
        with entered:
            identities.add(user_id)
            entered.notify_all()
        assert release.wait(10)
        return {'state':'none'}
    monkeypatch.setattr(codex_usage, 'refresh_codex_usage_reset', refresh)
    try:
        for n in range(32):
            assert codex_usage.trigger_codex_usage_reset_refresh(user_id=f'owner-{n}')
        with entered:assert entered.wait_for(lambda:len(identities)==32,3)
        assert codex_usage.trigger_codex_usage_reset_refresh(user_id='healthy')
        assert healthy.wait(2)
        assert not release.is_set()
        assert not codex_usage.trigger_codex_usage_reset_refresh(user_id='owner-0')
    finally:
        release.set()
        for thread in threads:thread.join(3)
    assert not any(thread.is_alive() for thread in threads)
    assert not codex_usage._refreshing_keys


@pytest.mark.parametrize('partial', [False, True])
def test_failed_refresh_start_releases_identity_without_query_or_publication(monkeypatch, partial):
    start = threading.Thread.start
    threads, queries, pushes = [], [], []
    monkeypatch.setattr('lib.oauth.token_store.load_token', lambda p:_token())
    monkeypatch.setattr(codex_usage, 'refresh_codex_usage_reset', lambda **kw:queries.append(kw) or {'state':'none'})
    monkeypatch.setattr(codex_usage, '_publish_codex_usage_reset_update', lambda **kw:pushes.append(kw))
    def fail(thread):
        if thread.name.startswith('codex-usage-reset-'):
            threads.append(thread)
            if partial:start(thread)
            raise RuntimeError('injected refresh startup failure')
        return start(thread)
    monkeypatch.setattr(threading.Thread, 'start', fail)
    with pytest.raises(RuntimeError, match='startup'):
        codex_usage.trigger_codex_usage_reset_refresh(user_id='owner')
    for thread in threads:
        if thread.ident is not None:thread.join(2)
    assert queries==pushes==[]
    assert not codex_usage._refreshing_keys
    monkeypatch.setattr(threading.Thread, 'start', start)
    assert codex_usage.trigger_codex_usage_reset_refresh(user_id='owner')
    deadline=time.monotonic()+2
    while codex_usage._refreshing_keys:
        assert time.monotonic()<deadline
        time.sleep(0.005)
    assert len(queries)==len(pushes)==1


def test_ambiguous_redeem_key_survives_thousand_identity_churn():
    request_ids=[]
    def post(url,payload,**kwargs):
        request_ids.append(payload['redeem_request_id'])
        raise RuntimeError('injected ambiguous transport failure')
    patches=_consume_mocks(_details_payload('credit-1'),post_side_effect=post)
    with patches[0],patches[1],patches[2],patches[3],patches[4]:
        first=codex_usage.consume_codex_usage_reset(user_id='owner',now=1000)
        assert first['error']=='unresolved'
        entries=codex_usage._read_entries()
        for n in range(1200):
            identity=codex_usage._identity(_token(f'other-{n}'),f'owner-{n}')
            entries[identity['cache_key']]={**identity,'state':'none','available_count':0,'captured_at':1001+n}
        codex_usage._write_entries(entries,now=3000)
        assert len(codex_usage._read_entries())==1201
        second=codex_usage.consume_codex_usage_reset(user_id='owner',now=3010)
        assert second['error']=='unresolved'
    assert len(request_ids)==2
    assert request_ids[0]==request_ids[1]


def test_expired_observations_reclaim_without_dropping_pending_evidence():
    now=200_000
    rows={}
    for name,state in [('old-none','none'),('old-unknown','unknown'),('pending','unknown'),('available','available'),('expired-positive','available'),('fresh','none')]:
        identity=codex_usage._identity(_token(name),'owner')
        row={**identity,'state':state,'available_count':1 if state=='available' else (0 if state=='none' else None),'captured_at':1}
        if name=='pending':row['pending_redeem']={'credit_id':'credit','redeem_request_id':'preserved-key','created_at':1}
        if name=='expired-positive':row['expires_at']=now-1
        if name=='fresh':row['captured_at']=now-1
        rows[identity['cache_key']]=row
    codex_usage._write_entries(rows,now=now)
    restored=codex_usage._read_entries()
    retained={name:codex_usage._identity(_token(name),'owner')['cache_key'] for name in ('pending','available','fresh')}
    assert set(restored)==set(retained.values())
    assert restored[retained['pending']]['pending_redeem']['redeem_request_id']=='preserved-key'
    assert codex_usage.clear_codex_usage_reset_cache(owner_user_id='owner')==3
    assert codex_usage._read_entries()=={}


def test_real_forced_refresh_preserves_ambiguous_redemption_record(monkeypatch):
    ids=[]
    monkeypatch.setattr('lib.oauth.token_store.load_token',lambda provider:_token())
    monkeypatch.setattr(codex_usage,'_authenticated_get',lambda url,**kwargs:
        {'rate_limit_reset_credits':{'available_count':1}} if url==codex_usage._codex_usage_url() else _details_payload('credit-1'))
    def post(url,payload,**kwargs):
        ids.append(payload['redeem_request_id'])
        raise RuntimeError('injected ambiguous response')
    monkeypatch.setattr(codex_usage,'_authenticated_post',post)
    monkeypatch.setattr(codex_usage,'_publish_codex_usage_reset_update',lambda **kwargs:None)
    first=codex_usage.consume_codex_usage_reset(user_id='owner',now=1000)
    assert first['error']=='unresolved'
    identity=codex_usage._identity(_token(),'owner')
    assert codex_usage._read_entries()[identity['cache_key']]['pending_redeem']['redeem_request_id']==ids[0]
    second=codex_usage.consume_codex_usage_reset(user_id='owner',now=1010)
    assert second['error']=='unresolved'
    assert ids[0]==ids[1]
    assert codex_usage._read_entries()[identity['cache_key']]['pending_redeem']['redeem_request_id']==ids[0]


@pytest.mark.parametrize('corrupt', ['not JSON', '{"schema_version":99,"entries":[]}', '{"schema_version":1,"entries":{}}', '{"schema_version":1,"entries":["invalid-row"]}'])
def test_unreadable_existing_evidence_prevents_post_and_is_not_overwritten(_private_cache,corrupt):
    from lib.json_store import JsonStoreReadError
    _private_cache.parent.mkdir(parents=True,exist_ok=True)
    _private_cache.write_text(corrupt,encoding='utf-8')
    patches=_consume_mocks(_details_payload('credit-1'),post_response=_Response({'code':'reset'}))
    with patches[0],patches[1],patches[2] as post,patches[3],patches[4]:
        with pytest.raises(JsonStoreReadError):
            codex_usage.consume_codex_usage_reset(user_id='owner',now=1000)
        post.assert_not_called()
    assert _private_cache.read_text(encoding='utf-8')==corrupt
