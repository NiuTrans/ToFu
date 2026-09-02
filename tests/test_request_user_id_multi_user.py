"""tests/test_request_user_id_multi_user.py — multi-user fail-closed gate.

docs/ENTERPRISE_READINESS_AUDIT.md (I1): in multi-user auth mode an
authenticated context WITHOUT a bound user_id must not silently fall into
the shared user-1 pool — canonical ``request_user_id`` raises PermissionError.
open/private personal installs map a resolved local principal to owner 1 at
composition. Context-less callers and multi-user principals without an owner
fail closed; background work must construct an explicit system/user principal.
"""

import pytest

from lib.identity import PERSONAL_USER_ID
import routes.api_v1.auth as auth

pytestmark = pytest.mark.unit


class _Ctx:
    def __init__(self, owner_user_id=''):
        self.owner_user_id = owner_user_id


def _patch_auth(monkeypatch, ctx, *, multi_user):
    import routes.api_v1.auth as auth_mod
    monkeypatch.setattr(auth_mod, 'current_auth', lambda: ctx)
    monkeypatch.setattr(auth_mod, 'is_multi_user', lambda: multi_user)


def test_multi_user_mode_rejects_unbound_principal(monkeypatch):
    _patch_auth(monkeypatch, _Ctx(owner_user_id=''), multi_user=True)
    with pytest.raises(PermissionError, match='owner_user_id'):
        auth.request_user_id()


def test_multi_user_mode_accepts_bound_principal(monkeypatch):
    _patch_auth(monkeypatch, _Ctx(owner_user_id='7'), multi_user=True)
    assert auth.request_user_id() == 7


def test_private_mode_keeps_default_fallback(monkeypatch):
    _patch_auth(monkeypatch, _Ctx(owner_user_id=''), multi_user=False)
    assert auth.request_user_id() == PERSONAL_USER_ID


@pytest.mark.parametrize('multi_user', [False, True])
def test_no_context_fails_closed(monkeypatch, multi_user):
    _patch_auth(monkeypatch, None, multi_user=multi_user)
    with pytest.raises(PermissionError, match='no authenticated principal'):
        auth.request_user_id()


def test_route_modules_import_the_canonical_identity_boundary():
    import routes.chat_queue as chat_queue
    import routes.conversation_sync_v3 as conversation_sync
    import routes.conversations as conversations
    import routes.conversations_compaction as compaction
    import routes.conversations_search as conversation_search
    import routes.api_v1.project as project
    import routes.api_v1.scheduler as scheduler

    for module in (
            chat_queue, conversation_sync, conversations, compaction,
            conversation_search, project, scheduler):
        assert module._request_user_id is auth.request_user_id
