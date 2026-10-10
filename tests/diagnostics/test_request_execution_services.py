"""Fault-injection coverage for non-task-backed application execution owners."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.unit


class _Admission:
    def __init__(self, lease="lease-1"):
        self.lease = lease
        self.released = []

    def acquire(self):
        return self.lease

    def release(self, lease):
        self.released.append(lease)
        return True


def test_log_compression_has_owner_deadline_retry_budget_and_exact_lease(
        monkeypatch):
    import lib.llm_dispatch as dispatch
    import lib.log_compression as service

    admission = _Admission()
    monkeypatch.setattr(service, "controller", admission)
    captured = {}

    def smart_chat(**kwargs):
        captured.update(kwargs)
        assert kwargs["abort_check"]() is False
        return "```text\ncompressed\n```", {"total_tokens": 3}

    monkeypatch.setattr(dispatch, "smart_chat", smart_chat)
    content, usage = service.compress_logs("verbose", owner_user_id=42)

    assert content == "compressed"
    assert usage == {"total_tokens": 3}
    assert captured["owner_user_id"] == 42
    assert captured["timeout"] == service.LOG_COMPRESSION_TIMEOUT_SECONDS
    assert captured["max_429_attempts"] > 0
    assert admission.released == ["lease-1"]


def test_log_compression_dispatch_failure_still_releases_exact_lease(monkeypatch):
    import lib.llm_dispatch as dispatch
    import lib.log_compression as service

    admission = _Admission()
    monkeypatch.setattr(service, "controller", admission)
    monkeypatch.setattr(
        dispatch, "smart_chat",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("provider down")),
    )

    with pytest.raises(RuntimeError, match="provider down"):
        service.compress_logs("verbose", owner_user_id=42)
    assert admission.released == ["lease-1"]


def test_embedding_execution_success_releases_route_and_exact_admission(
        monkeypatch):
    import lib.llm_dispatch as dispatch
    import lib.model_routing.embedding_execution as service

    admission = _Admission("embed-lease")
    monkeypatch.setattr(service, "controller", admission)
    route_group = SimpleNamespace(pin_id="pin-1")
    monkeypatch.setattr(
        service, "mint_capability_slot_group",
        lambda *_args, **_kwargs: ("embed-model", route_group),
    )
    disposed = []
    monkeypatch.setattr(
        service, "dispose_routed_slot_group", disposed.append)

    slot = SimpleNamespace(
        base_url="https://embedding.example/v1",
        extra_headers={}, api_key="secret", model="wire-model",
        record_error=lambda **_kwargs: pytest.fail("unexpected provider error"),
        record_success=lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        dispatch, "get_dispatcher",
        lambda: SimpleNamespace(pick_and_reserve=lambda **_kwargs: slot),
    )
    monkeypatch.setattr(
        service, "http_post",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=True, status_code=200, json=lambda: {"data": [{"embedding": [1]}]},
        ),
    )

    assert service.execute_embeddings(
        ["hello"], model="embed-model", owner_user_id=5, tenant_id=None,
    ) == {"data": [{"embedding": [1]}]}
    assert disposed == [route_group]
    assert admission.released == ["embed-lease"]


def test_embedding_transport_failure_releases_route_and_admission(monkeypatch):
    import lib.llm_dispatch as dispatch
    import lib.model_routing.embedding_execution as service

    admission = _Admission("embed-fail-lease")
    monkeypatch.setattr(service, "controller", admission)
    route_group = SimpleNamespace(pin_id="pin-fail")
    monkeypatch.setattr(
        service, "mint_capability_slot_group",
        lambda *_args, **_kwargs: ("embed-model", route_group),
    )
    disposed = []
    monkeypatch.setattr(
        service, "dispose_routed_slot_group", disposed.append)
    errors = []
    slot = SimpleNamespace(
        base_url="https://embedding.example/v1",
        extra_headers={}, api_key="", model="wire-model",
        record_error=lambda **_kwargs: errors.append(True),
        record_success=lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        dispatch, "get_dispatcher",
        lambda: SimpleNamespace(pick_and_reserve=lambda **_kwargs: slot),
    )
    monkeypatch.setattr(
        service, "http_post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("slow")),
    )

    with pytest.raises(TimeoutError, match="slow"):
        service.execute_embeddings(
            ["hello"], model="embed-model", owner_user_id=5, tenant_id=None,
        )
    assert errors == [True]
    assert disposed == [route_group]
    assert admission.released == ["embed-fail-lease"]


def test_embedding_input_budget_is_finite():
    from lib.model_routing.embedding_execution import (
        EMBEDDING_MAX_INPUT_ITEMS,
        validate_embedding_inputs,
    )

    with pytest.raises(ValueError, match="at most"):
        validate_embedding_inputs([""] * (EMBEDDING_MAX_INPUT_ITEMS + 1))


def test_billing_disable_after_reserve_releases_existing_hold(monkeypatch):
    import lib.billing as billing
    import lib.relay_config as relay_config
    from lib.billing.request_flow import settle_task

    released = []
    monkeypatch.setattr(relay_config, "billing_enabled", lambda: False)
    monkeypatch.setattr(
        billing, "reserve_release",
        lambda user_id, micro, **kwargs: released.append(
            (user_id, micro, kwargs["ref_id"])),
    )
    task = {
        "id": "billing-flip",
        "_billing_reservation_micro": 17,
        "usage": {"total_tokens": 3},
    }

    assert settle_task(task, user_id="account-1", model="model") is None
    assert released == [("account-1", 17, "billing-flip")]
