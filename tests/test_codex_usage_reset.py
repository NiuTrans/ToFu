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
    cache_path = tmp_path / "oauth" / "codex_usage_reset_cache.json"
    monkeypatch.setattr(codex_usage, "_cache_path", lambda: str(cache_path))
    codex_usage._reset_codex_usage_state_for_tests()
    yield cache_path
    codex_usage._reset_codex_usage_state_for_tests()


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
         mock.patch("lib.http_client.http_get",
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
             "lib.http_client.http_get",
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
             "lib.http_client.http_get",
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


def test_cache_rows_and_entry_count_are_hard_bounded(_private_cache):
    entries = {}
    for index in range(30):
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
    assert len(persisted["entries"]) == 16
    assert len(codex_usage._read_entries()) == 16


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


def test_refresh_lock_sidecars_are_bounded_across_account_churn():
    paths = set()
    for index in range(100):
        identity = codex_usage._identity(
            _token(f"account-{index}"), f"owner-{index}")
        assert identity is not None
        paths.add(codex_usage._refresh_lock_path(identity["cache_key"]))

    assert len(paths) <= 16


def test_daemon_capacity_is_bounded_per_owner_account(monkeypatch):
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
    assert codex_usage.trigger_codex_usage_reset_refresh(user_id="owner-3") is False
    release.set()
    deadline = time.monotonic() + 5
    identities = [
        codex_usage._identity(_token(), owner)
        for owner in ("owner", "owner-2")
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


def test_capacity_deferral_returns_a_bounded_retry_hint():
    with codex_usage._state_lock:
        codex_usage._refreshing_keys.update({"occupied-a", "occupied-b"})
    try:
        with mock.patch("lib.oauth.token_store.load_token",
                        return_value=_token()):
            status = codex_usage.codex_usage_reset_status(
                user_id="waiting-owner", now=1000)
    finally:
        codex_usage._reset_codex_usage_state_for_tests()

    assert status["state"] == "unknown"
    assert status["refreshing"] is False
    assert status["retry_after_seconds"] == 5


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
    detector.assert_called_once_with(user_id="owner-42")


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
