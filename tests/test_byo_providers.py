"""Executable contract for the Sidecar-backed BYO provider repository."""

from __future__ import annotations

from cryptography.fernet import Fernet
import pytest


pytest_plugins = ("tests._credential_sidecar",)

ALICE = 11_001
BOB = 11_002
QUOTA_OWNER = 11_003
TEST_OWNERS = (ALICE, BOB, QUOTA_OWNER)


@pytest.fixture(autouse=True)
def isolated_provider_domain(monkeypatch):
    """Give each test a fresh encryption key and remove only its owner rows."""
    from lib.secret_envelope import reset_secret_envelope_for_test

    monkeypatch.setenv(
        "TOFU_SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    reset_secret_envelope_for_test()
    yield
    from lib.byo_providers import delete_provider, list_providers

    for owner_user_id in TEST_OWNERS:
        for provider in list_providers(owner_user_id):
            delete_provider(provider["id"], owner_user_id)
    reset_secret_envelope_for_test()


def _create(
    owner_user_id: int = ALICE,
    *,
    name: str = "cluster-A",
    api_key: str = "sk-secret-AAAA",
    models: list[dict] | None = None,
    **fields,
) -> dict:
    from lib.byo_providers import create_provider

    return create_provider(
        owner_user_id=owner_user_id,
        name=name,
        base_url=fields.pop("base_url", "http://10.0.0.5:8080/v1"),
        api_key=api_key,
        models=(
            [{"model_id": "deepseek-v4-pro"}]
            if models is None
            else models
        ),
        **fields,
    )


def test_create_list_and_get_keep_plaintext_out_of_storage_projection():
    from lib.byo_providers import get_provider, list_providers, redact
    from lib.storage.service import get_storage_client

    created = _create()
    assert created["id"].startswith("prov_")
    assert created["owner_user_id"] == ALICE
    assert created["api_key"] == "sk-secret-AAAA"

    listed = list_providers(ALICE)
    assert len(listed) == 1
    assert "api_key" not in listed[0]
    assert "api_key_ciphertext" not in listed[0]
    assert "owner_user_id" not in listed[0]
    assert listed[0]["key_hint"] == "sk-s…AAAA"

    stored = get_storage_client().query(
        "provider.get",
        {"provider_id": created["id"], "owner_user_id": ALICE,
         "tenant_id": ""},
    )
    assert "sk-secret-AAAA" not in str(stored)
    assert stored["api_key_ciphertext"]
    assert get_provider(created["id"], ALICE)["api_key"] == "sk-secret-AAAA"
    assert redact(listed[0]) == listed[0]


def test_repository_owner_not_bearer_key_controls_visibility():
    from lib.byo_providers import get_provider, list_providers

    alice = _create(ALICE, name="A", api_key="", models=[])
    _create(BOB, name="B", api_key="", models=[])
    assert [row["name"] for row in list_providers(ALICE)] == ["A"]
    assert get_provider(alice["id"], BOB) is None
    assert get_provider(alice["id"], ALICE) is not None


def test_tenant_boundary_is_part_of_every_lookup():
    from lib.byo_providers import get_provider, list_providers

    created = _create(ALICE, tenant_id="tenant-a")
    assert list_providers(ALICE) == []
    assert get_provider(created["id"], ALICE, tenant_id="tenant-b") is None
    assert get_provider(
        created["id"], ALICE, tenant_id="tenant-a") is not None


def test_update_delete_and_wrong_owner_are_atomic():
    from lib.byo_providers import delete_provider, get_provider, update_provider

    created = _create(api_key="", models=[])
    assert update_provider(
        created["id"], ALICE, name="renamed", disabled=True)
    updated = get_provider(created["id"], ALICE)
    assert updated["name"] == "renamed"
    assert updated["disabled"] is True
    assert not update_provider(created["id"], BOB, name="stolen")
    assert not delete_provider(created["id"], BOB)
    assert delete_provider(created["id"], ALICE)
    assert get_provider(created["id"], ALICE) is None
    assert not delete_provider(created["id"], ALICE)


def test_update_reencrypts_secret_and_refreshes_hint():
    from lib.byo_providers import get_provider, get_public, update_provider
    from lib.storage.service import get_storage_client

    created = _create()
    before = get_storage_client().query(
        "provider.get",
        {"provider_id": created["id"], "owner_user_id": ALICE,
         "tenant_id": ""},
    )["api_key_ciphertext"]
    assert update_provider(created["id"], ALICE, api_key="new-secret-9999")
    after = get_storage_client().query(
        "provider.get",
        {"provider_id": created["id"], "owner_user_id": ALICE,
         "tenant_id": ""},
    )["api_key_ciphertext"]
    assert after != before
    assert get_provider(created["id"], ALICE)["api_key"] == "new-secret-9999"
    assert get_public(created["id"], ALICE)["key_hint"] == "new-…9999"


def test_secret_ciphertext_is_bound_to_owner_and_record():
    from lib.secret_envelope import SecretEnvelopeError, open_secret, seal_secret

    ciphertext = seal_secret(
        "secret", purpose="byo-provider-api-key",
        owner_user_id=ALICE, record_id="prov_one")
    with pytest.raises(SecretEnvelopeError):
        open_secret(
            ciphertext, purpose="byo-provider-api-key",
            owner_user_id=BOB, record_id="prov_one")
    with pytest.raises(SecretEnvelopeError):
        open_secret(
            ciphertext, purpose="byo-provider-api-key",
            owner_user_id=ALICE, record_id="prov_two")


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"name": ""}, "name is required"),
        ({"base_url": "ftp://bad"}, "must start with"),
        ({"models": "not-a-list"}, "models must be a list"),
        ({"api_key": "x" * 9000}, "api_key exceeds"),
    ],
)
def test_create_rejects_invalid_input(fields, message):
    defaults = {
        "owner_user_id": ALICE,
        "name": "valid",
        "base_url": "http://10.0.0.5:8080/v1",
        "api_key": "",
        "models": [],
    }
    defaults.update(fields)
    from lib.byo_providers import create_provider

    with pytest.raises(ValueError, match=message):
        create_provider(**defaults)


def test_domain_validation_cannot_be_bypassed_by_direct_call():
    from lib.byo_providers import create_provider, update_provider

    with pytest.raises(ValueError, match="reserved"):
        create_provider(
            owner_user_id=ALICE,
            name="bad headers",
            base_url="http://10.0.0.5:8080/v1",
            api_key="",
            models=[],
            extra_headers={"Authorization": "Bearer stolen"},
        )
    created = _create(api_key="", models=[])
    with pytest.raises(ValueError, match="reserved"):
        update_provider(
            created["id"], ALICE,
            extra_headers={"X-API-Key": "stolen"})
    with pytest.raises(ValueError, match="unknown provider update fields"):
        update_provider(created["id"], ALICE, owner_key_id="credential")


def test_quota_is_enforced_transactionally_per_owner():
    for index in range(32):
        _create(
            QUOTA_OWNER, name=f"provider-{index}", api_key="", models=[])
    with pytest.raises(RuntimeError, match="provider quota reached"):
        _create(QUOTA_OWNER, name="overflow", api_key="", models=[])


def test_model_string_resolution_and_disabled_behavior():
    from lib.byo_providers import resolve_model_string, update_provider

    plain = resolve_model_string("deepseek-v4-pro", ALICE)
    assert plain.model_id == "deepseek-v4-pro"
    assert plain.provider is None

    created = _create(api_key="", models=[])
    resolved = resolve_model_string(
        f"deepseek-v4-pro@{created['id']}", ALICE)
    assert resolved.model_id == "deepseek-v4-pro"
    assert resolved.provider["id"] == created["id"]
    assert resolve_model_string(f"foo@{created['id']}", BOB) is None

    assert update_provider(created["id"], ALICE, disabled=True)
    assert resolve_model_string(f"foo@{created['id']}", ALICE) is None

    version_tag = resolve_model_string("foo@1.0", ALICE)
    assert version_tag.model_id == "foo@1.0"
    assert version_tag.provider is None


def test_thinking_format_round_trips_and_rejects_unknown_dialects():
    from lib.byo_providers import get_provider, update_provider

    created = _create(
        api_key="", models=[], thinking_format="chat_template_kwargs")
    assert created["thinking_format"] == "chat_template_kwargs"
    assert get_provider(
        created["id"], ALICE)["thinking_format"] == "chat_template_kwargs"
    assert update_provider(created["id"], ALICE, thinking_format="none")
    assert get_provider(created["id"], ALICE)["thinking_format"] == "none"
    with pytest.raises(ValueError):
        update_provider(created["id"], ALICE, thinking_format="glm-style")
